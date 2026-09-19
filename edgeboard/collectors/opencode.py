"""OpenCode sessions, read from and driven through the local OpenCode service.

OpenCode (V2) runs a background service; ``~/.local/state/opencode/service.json``
holds its URL, bearer password, pid and version. Unlike Claude Code it exposes a
real HTTP API: sessions and their messages, pending permission requests and
forms can be read, prompts sent, and permissions/forms answered, so the panel
gets full parity without hooks.

The API is versioned with the OpenCode release and every request is wrapped: an
unreachable service simply reports no sessions, and anything unexpected is
raised so the server shows it under the sessions panel instead of taking the
dashboard down.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx

from edgeboard.collectors.claude_sessions import ATTENTION, DONE, IDLE, WORKING, Session
from edgeboard.collectors.claude_transcripts import HISTORY_MAX, clean_text, tool_hint
from edgeboard.collectors.sessions import empty_summary
from edgeboard.config import Settings

PROMPT_MAX = 300
MESSAGE_SAMPLE = 14  # messages read per session, newest first (enough for the HISTORY_MAX transcript)
CANDIDATE_LIMIT = 8  # sessions whose detail is read each poll
IDLE_WINDOW = 30 * 60.0  # a finished session stays "idle" (and sendable) this long
MODEL_TTL = 10 * 60.0
TOOL_VERBS = {
    "shell": "running",
    "read": "reading",
    "edit": "editing",
    "write": "writing",
    "patch": "editing",
    "grep": "searching",
    "glob": "searching",
    "list": "listing",
    "webfetch": "fetching",
    "websearch": "searching",
    "task": "agent:",
    "todowrite": "planning",
    "todoread": "planning",
}

# ``(method, path, body) -> decoded JSON``; injectable, like the Spotify runner.
Request = Callable[[str, str, dict | None], dict]


@dataclass(frozen=True)
class Service:
    url: str
    password: str


def read_service(path: Path) -> Service | None:
    """The URL and password of the running OpenCode service, or None without one."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    url, password = data.get("url"), data.get("password")
    if not isinstance(url, str) or not url.startswith("http") or not isinstance(password, str) or not password:
        return None
    return Service(url.rstrip("/"), password)


def default_request(service: Service, timeout: float = 5.0) -> Request:
    """An httpx-backed request runner for the OpenCode service (HTTP basic auth)."""

    def request(method: str, path: str, body: dict | None = None) -> dict:
        with httpx.Client(base_url=service.url, auth=("opencode", service.password), timeout=timeout) as client:
            response = client.request(method, path, json=body)
            response.raise_for_status()
            return response.json()

    return request


def _iso(ms) -> str | None:
    if not isinstance(ms, (int, float)) or ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _epoch(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return 0.0


def _message_time(message: dict) -> float:
    time_info = message.get("time") if isinstance(message.get("time"), dict) else {}
    values = [v for v in time_info.values() if isinstance(v, (int, float))]
    return max(values) / 1000 if values else 0.0


def clean_tool_hint(name: str, tool_input) -> str:
    """``shell``/``edit``/``read`` use camelCase inputs, Claude's ``tool_hint`` uses PascalCase keys."""
    if not isinstance(tool_input, dict):
        return ""
    if name == "shell":
        command = tool_input.get("command")
        if isinstance(command, str) and command:
            return tool_hint("Bash", {"command": command})
        return ""
    if name in ("edit", "write", "read", "patch"):
        path = tool_input.get("filePath") or tool_input.get("path") or ""
        return Path(str(path)).name if path else ""
    return tool_hint(name.capitalize(), tool_input)


def tool_line(name: str, tool_input) -> str:
    """``running dotnet test``, ``editing server.py``: one phrase for the card's "now" line."""
    verb = TOOL_VERBS.get(name, "running")
    hint = clean_tool_hint(name, tool_input)
    if not hint:
        return f"{verb} {name}".strip()
    return f"{verb} {hint}"


def _running_tool(message: dict | None) -> dict | None:
    """The tool part of an assistant message when it is still running (or streaming)."""
    if not isinstance(message, dict) or message.get("type") != "assistant":
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    for part in reversed(content):
        if not isinstance(part, dict) or part.get("type") != "tool":
            continue
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        if state.get("status") in ("running", "streaming", "pending"):
            return part
    return None


def _assistant_text(message: dict) -> str:
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str) and part["text"].strip():
            return clean_text(part["text"], PROMPT_MAX)
    return ""


def _context_tokens(message: dict | None) -> int:
    """The newest assistant step's prompt size: input plus cache read/write."""
    if not isinstance(message, dict):
        return 0
    tokens = message.get("tokens") if isinstance(message.get("tokens"), dict) else None
    if not tokens:
        return 0
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    try:
        return int(tokens.get("input") or 0) + int(cache.get("read") or 0) + int(cache.get("write") or 0)
    except (TypeError, ValueError):
        return 0


def _session_messages(messages: list[dict]) -> tuple[str, str, int]:
    """(last prompt, last reply, context tokens) from a newest-first message list."""
    prompt = reply = ""
    context = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        kind = message.get("type")
        if kind == "user" and not prompt:
            prompt = clean_text(str(message.get("text") or ""), PROMPT_MAX)
        elif kind == "assistant" and not reply:
            reply = _assistant_text(message)
        if kind == "assistant" and not context:
            context = _context_tokens(message)
    return prompt, reply, context


def _session_history(messages: list[dict]) -> list[dict]:
    """The conversation tail for the detail overlay: user/assistant texts, oldest first.

    The API returns messages newest first; tool-only assistant steps carry no
    text and are skipped, so the transcript shows the conversation, not the
    tool churn.
    """
    history: list[dict] = []
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        kind = message.get("type")
        if kind == "user":
            text = clean_text(str(message.get("text") or ""), PROMPT_MAX)
        elif kind == "assistant":
            text = _assistant_text(message)
        else:
            continue
        if text:
            history.append({"role": kind, "text": text})
    return history[-HISTORY_MAX:]


def classify(
    session: dict,
    active: bool,
    permission: dict | None,
    form: dict | None,
    messages: list[dict],
    now: datetime,
) -> tuple[str, str]:
    """(status, detail) for one OpenCode session, mirroring ``claude_sessions.classify``."""
    if permission is not None:
        return ATTENTION, "needs permission"
    if form is not None:
        return ATTENTION, "asking you a question"
    newest = next((m for m in messages if isinstance(m, dict)), None)
    if active:
        tool = _running_tool(newest)
        if tool is not None:
            return WORKING, tool_line(str(tool.get("name") or ""), tool.get("state", {}).get("input"))
        if newest is not None and newest.get("type") == "idle":
            return IDLE, "waiting for you"
        if newest is not None and newest.get("type") == "user":
            return WORKING, "working on your prompt"
        return WORKING, "thinking"
    idle_since = session.get("time", {}).get("idle") or session.get("time", {}).get("updated")
    if idle_since and now.timestamp() - idle_since / 1000 < IDLE_WINDOW:
        return IDLE, "waiting for you"
    return DONE, "finished"


def _question_id(permission: dict) -> str:
    return str(permission.get("id") or "")


def permission_question(permission: dict) -> dict | None:
    """A pending permission request as the page's question shape (single choice)."""
    question_id = _question_id(permission)
    if not question_id:
        return None
    action = str(permission.get("action") or "this action")
    resources = permission.get("resources") if isinstance(permission.get("resources"), list) else []
    detail = ", ".join(str(r) for r in resources[:3] if r)
    message = permission.get("message") if isinstance(permission.get("message"), str) else ""
    text = message or f"Allow {action}" + (f" on {detail}" if detail else "?")
    return {
        "tool_use_id": question_id,
        "title": "Permission",
        "questions": [{"question": text, "header": action, "options": ["Allow once", "Always allow", "Deny"], "multi": False}],
    }


def form_questions(form: dict) -> dict | None:
    """A pending form as the page's question shape; option labels carry the form's values."""
    form_id = str(form.get("id") or "")
    questions = []
    fields = form.get("fields") if isinstance(form.get("fields"), list) else []
    for spec in fields:
        if not isinstance(spec, dict) or not spec.get("key"):
            continue
        title = str(spec.get("title") or spec.get("key"))
        question = str(spec.get("description") or title)
        kind = str(spec.get("type") or "string")
        options = spec.get("options") if isinstance(spec.get("options"), list) else []
        labels = [str(o.get("label") or o.get("value")) for o in options if isinstance(o, dict)]
        if kind == "boolean":
            labels = ["Yes", "No"]
        questions.append({"question": question, "header": title, "options": labels, "multi": kind == "multiselect"})
    if not form_id or not questions:
        return None
    return {"tool_use_id": form_id, "title": str(form.get("title") or "Question"), "questions": questions}


# Pending permission/form questions on the OpenCode side, keyed by their id, so
# an answer from the panel can be translated into the right API call. The
# collector refreshes it every round and drops entries older than HOOK_TTL.
@dataclass
class Pending:
    session_id: str
    kind: str  # "permission" | "form"
    request_id: str
    fields: dict[str, dict] = field(default_factory=dict)  # question text -> {key, type, options: {label: value}}
    seen_at: float = 0.0


class PendingStore:
    def __init__(self) -> None:
        self._items: dict[str, Pending] = {}

    def get(self, question_id: str) -> Pending | None:
        return self._items.get(question_id)

    def expire(self, now: float | None = None, ttl: float = 600.0) -> None:
        now = time.time() if now is None else now
        for question_id, entry in list(self._items.items()):
            if now - entry.seen_at > ttl:
                del self._items[question_id]

    def remember(self, question_id: str, entry: Pending) -> None:
        entry.seen_at = time.time()
        self._items[question_id] = entry

    def forget_session(self, session_id: str) -> None:
        for question_id, entry in list(self._items.items()):
            if entry.session_id == session_id:
                del self._items[question_id]


PENDING = PendingStore()

_model_cache: tuple[float, dict[str, int]] = (0.0, {})


def context_windows(request: Request) -> dict[str, int]:
    """``{model id: context window}`` from the model catalog, cached for a while."""
    global _model_cache
    now = time.time()
    cached_at, cached = _model_cache
    if cached and now - cached_at < MODEL_TTL:
        return cached
    try:
        data = request("GET", "/api/model", None)
    except Exception:  # noqa: BLE001 - the gauge is nice-to-have, never fail the collector for it
        return cached
    windows: dict[str, int] = {}
    for model in data.get("data", []) if isinstance(data, dict) else []:
        if not isinstance(model, dict):
            continue
        limit = model.get("limit") if isinstance(model.get("limit"), dict) else {}
        context = limit.get("context")
        identifier = str(model.get("id") or "")
        if identifier and isinstance(context, (int, float)) and context > 0:
            windows[identifier] = int(context)
    _model_cache = (now, windows)
    return windows


def _short_model(session: dict) -> str:
    model = session.get("model") if isinstance(session.get("model"), dict) else {}
    name = str(model.get("id") or "")
    return name


def _fetch_messages(request: Request, session_id: str) -> list[dict]:
    data = request("GET", f"/api/session/{session_id}/message?limit={MESSAGE_SAMPLE}&order=desc", None)
    messages = data.get("data") if isinstance(data, dict) else None
    return [m for m in messages if isinstance(m, dict)] if isinstance(messages, list) else []


def _fetch_pending(request: Request, session_id: str) -> tuple[dict | None, dict | None]:
    permissions: list = []
    forms: list = []
    try:
        data = request("GET", f"/api/session/{session_id}/permission", None)
        permissions = data.get("data") or [] if isinstance(data, dict) else []
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
    try:
        data = request("GET", f"/api/session/{session_id}/form", None)
        forms = data.get("data") or [] if isinstance(data, dict) else []
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
    permission = permissions[0] if permissions and isinstance(permissions[0], dict) else None
    form = forms[0] if forms and isinstance(forms[0], dict) else None
    return permission, form


def _pending_entry(session_id: str, permission: dict | None, form: dict | None) -> tuple[dict | None, Pending | None]:
    """The question shown on the card and the data needed to answer it."""
    if permission is not None:
        question = permission_question(permission)
        if question is not None:
            text = question["questions"][0]["question"]
            options = {"Allow once": "once", "Always allow": "always", "Deny": "reject"}
            return question, Pending(session_id, "permission", _question_id(permission), {text: {"key": "", "type": "choice", "options": options}})
    if form is not None:
        question = form_questions(form)
        if question is not None:
            fields: dict[str, dict] = {}
            for spec, item in zip(form.get("fields") or [], question["questions"], strict=False):
                if not isinstance(spec, dict):
                    continue
                options = {str(o.get("label") or o.get("value")): str(o.get("value") or o.get("label")) for o in spec.get("options") or [] if isinstance(o, dict)}
                if spec.get("type") == "boolean":
                    options = {"Yes": True, "No": False}
                fields[item["question"]] = {"key": str(spec.get("key") or ""), "type": str(spec.get("type") or "string"), "options": options}
            return question, Pending(session_id, "form", str(form.get("id") or ""), fields)
    return None, None


def _candidates(sessions: list[dict], active: dict, now: datetime) -> list[dict]:
    midnight = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000
    picked = []
    for session in sessions:
        if not isinstance(session, dict) or session.get("parentID"):
            continue  # subagent sessions are counted on their parent's card
        time_info = session.get("time") if isinstance(session.get("time"), dict) else {}
        updated = time_info.get("updated") or time_info.get("created") or 0
        if session.get("id") in active or updated >= midnight:
            picked.append(session)
    picked.sort(key=lambda s: (s.get("time") or {}).get("updated") or 0, reverse=True)
    return picked


def collect_sessions(settings: Settings, now: datetime, hooks: dict[str, dict]) -> tuple[list[Session], dict]:
    """Read the OpenCode service; ``([], summary)`` when it is not running."""
    service = read_service(settings.opencode_state_file)
    if service is None:
        return [], empty_summary()
    try:
        request = default_request(service)
        data = request("GET", "/api/session", None)
        sessions = data.get("data") if isinstance(data, dict) else None
        if not isinstance(sessions, list):
            return [], empty_summary()
        active_data = request("GET", "/api/session/active", None)
        active = active_data.get("data") if isinstance(active_data, dict) and isinstance(active_data.get("data"), dict) else {}
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
        return [], empty_summary()  # service not up right now: nothing to show, nothing to complain about
    sessions = [s for s in sessions if isinstance(s, dict)]
    parents = [s for s in sessions if not s.get("parentID")]
    PENDING.expire()

    children: dict[str, list[dict]] = {}
    for child in sessions:
        parent = child.get("parentID")
        if isinstance(parent, str) and parent:
            children.setdefault(parent, []).append(child)

    windows = context_windows(request)
    midnight = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    candidates = _candidates(sessions, active, now)[:CANDIDATE_LIMIT]
    result: list[Session] = []
    for info in candidates:
        session_id = str(info.get("id") or "")
        if not session_id:
            continue
        messages = _fetch_messages(request, session_id)
        permission, form = _fetch_pending(request, session_id)
        question, pending = _pending_entry(session_id, permission, form)
        if question is not None:
            question = {**question, "answerable": True}
        if pending is not None:
            PENDING.remember(question["tool_use_id"], pending)
        running = active.get(session_id) if isinstance(active.get(session_id), dict) else None
        is_active = bool(running and running.get("type") == "running")
        status, detail = classify(info, is_active, permission, form, messages, now)
        prompt, reply, context = _session_messages(messages)
        history = _session_history(messages)
        time_info = info.get("time") if isinstance(info.get("time"), dict) else {}
        created = time_info.get("created") or 0
        updated = max([time_info.get("updated") or 0, int(_message_time(messages[0]) * 1000) if messages else 0])
        model = _short_model(info)
        window = windows.get(model) or settings.context_window
        kids = children.get(session_id) or []
        active_kids = sum(1 for kid in kids if kid.get("id") in active)
        cwd = ""
        location = info.get("location") if isinstance(info.get("location"), dict) else {}
        if isinstance(location.get("directory"), str):
            cwd = location["directory"]
        agents_label = str(info.get("agent") or "")
        waiting = status in (IDLE, ATTENTION)
        result.append(
            Session(
                id=session_id,
                name=str(info.get("title") or Path(cwd).name or "session"),
                project=Path(cwd).name if cwd else "",
                cwd=cwd,
                branch="",
                model=model,
                status=status,
                detail=detail,
                context_tokens=context,
                started_at=_iso(created),
                last_activity=_iso(updated) or _iso(created),
                messages=0,
                agents=len(kids),
                active_agents=active_kids,
                last_prompt=prompt,
                last_reply=reply,
                history=history,
                permission_mode="",
                session_name=agents_label,
                agent="opencode",
                agent_detail=agents_label,
                can_send=True,  # the service can resume a session with a prompt
                waiting_since=_iso(time_info.get("idle") or updated) if waiting else None,
                question=question,
                context_window=window,
                context_pct=round(100 * context / window) if window else 0,
                tasks=None,
            )
        )
    summary = empty_summary()
    today = [s for s in parents if (s.get("time") or {}).get("updated", 0) >= midnight.timestamp() * 1000]
    summary["today"] = len(today)
    summary["done"] = sum(1 for s in today if s.get("id") not in active and now.timestamp() * 1000 - ((s.get("time") or {}).get("updated") or 0) >= IDLE_WINDOW * 1000)
    for session in result:
        if session.status in summary and session.status != DONE:
            summary[session.status] += 1
    return result, summary


# --- write paths (used by the server's /api routes) -------------------------


def service_or_raise(settings: Settings) -> Service:
    service = read_service(settings.opencode_state_file)
    if service is None:
        raise RuntimeError("the OpenCode service is not running")
    return service


def send_prompt(request: Request, session_id: str, text: str, resume: bool) -> dict:
    body: dict = {"text": text}
    if resume:
        body["resume"] = True
    return request("POST", f"/api/session/{session_id}/prompt", body)


def reply_permission(request: Request, session_id: str, request_id: str, decision: str) -> dict:
    return request("POST", f"/api/session/{session_id}/permission/{request_id}/reply", {"decision": decision})


def reply_form(request: Request, session_id: str, form_id: str, answer: dict) -> dict:
    return request("POST", f"/api/session/{session_id}/form/{form_id}/reply", {"answer": answer})


def form_answer(entry: Pending, answers: dict[str, str]) -> dict | None:
    """Translate the page's ``{question: label or text}`` into a ``Form.Reply`` answer map."""
    result: dict = {}
    for question, spec in entry.fields.items():
        value = answers.get(question)
        if value is None:
            return None
        kind, key, options = spec.get("type"), spec.get("key"), spec.get("options") or {}
        if not key:
            return None
        if key in result:
            continue
        if options:
            if kind == "multiselect":
                result[key] = [options.get(part.strip(), part.strip()) for part in str(value).split(",")]
            else:
                result[key] = options.get(str(value).strip(), value)
        elif kind in ("integer", "number"):
            try:
                result[key] = int(value) if kind == "integer" else float(value)
            except (TypeError, ValueError):
                return None
        elif kind == "boolean":
            result[key] = str(value).strip().lower() in ("yes", "true", "1", "on")
        else:
            result[key] = str(value)
    return result
