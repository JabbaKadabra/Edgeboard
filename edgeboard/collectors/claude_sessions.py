"""Discover Claude Code sessions and classify what each one is doing."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from edgeboard.collectors.claude_transcripts import PROMPT_MAX, SessionFacts, SessionParser, clean_text, flatten_question, iter_entries, read_new_lines, read_transcript_bytes, short_model, tool_hint
from edgeboard.config import Settings

ATTENTION = "attention"  # Claude is blocked on the user: a permission prompt or a question
WORKING = "working"
IDLE = "idle"
DONE = "done"
# A transcript without a pid file (``claude -p``, remote sessions) counts as
# running while it was written this recently and its tail says Claude is busy.
# The same window decides whether a subagent transcript counts as active.
HEADLESS_ACTIVE_SECS = 60.0
# A hook event (POST /api/hook) older than this no longer overrides the
# transcript, so a missed Stop cannot pin a card.
HOOK_TTL = 10 * 60.0
_TOOL_VERBS = {
    "Bash": "running",
    "Read": "reading",
    "Edit": "editing",
    "Write": "writing",
    "NotebookEdit": "editing",
    "Grep": "searching",
    "Glob": "searching",
    "Agent": "agent:",
    "Task": "agent:",
}


@dataclass
class Session:
    id: str
    name: str
    project: str
    cwd: str
    branch: str
    model: str
    status: str
    detail: str
    context_tokens: int
    started_at: str | None
    last_activity: str | None
    messages: int
    agents: int = 0  # subagent transcripts under <project>/<session>/subagents/
    active_agents: int = 0  # of those, written in the last HEADLESS_ACTIVE_SECS
    last_prompt: str = ""
    last_reply: str = ""  # what Claude last said (Stop hook, else the transcript)
    permission_mode: str = ""
    session_name: str = ""  # Claude Code's own name for the session (pid file ``name``)
    can_send: bool = False  # alive with an inbox socket: POST /api/sessions/{id}/send works
    waiting_since: str | None = None  # since when it has been idle / needing you
    question: dict | None = None  # pending AskUserQuestion, see ``question_from_hook``

    def to_dict(self) -> dict:
        return asdict(self)


def tool_detail(name: str, hint: str) -> str:
    """``running ls -la``, ``editing server.py``, ``searching "foo"``, ``running WebFetch``."""
    if not name:
        return "running tool"
    if not hint:
        return f"running {name}"
    return f"{_TOOL_VERBS.get(name, 'running')} {hint}"


def classify(facts: SessionFacts, alive: bool, active_agents: int = 0) -> tuple[str, str]:
    """Return (status, detail) following the table in the design spec."""
    if not alive:
        return DONE, "finished"
    if facts.last_kind == "user_prompt":
        return WORKING, "working on your prompt"
    if facts.last_kind == "tool_result":
        return WORKING, "thinking"
    if facts.last_kind == "assistant" and facts.last_stop_reason == "tool_use":
        if facts.last_tool == "AskUserQuestion":
            return ATTENTION, "answer in the terminal"  # only a hook (see hook_override) lets the panel answer
        return WORKING, tool_detail(facts.last_tool, facts.last_tool_hint)
    if active_agents:
        return WORKING, "agents running"  # the main transcript waits on a subagent
    if facts.last_kind == "assistant":
        return IDLE, "waiting for you"
    return IDLE, "session started"


def hook_override(hook: dict) -> tuple[str, str] | None:
    """What a Claude Code hook event says the session is doing, or None when it says nothing."""
    event = hook.get("hook_event_name")
    if event == "Notification":
        kind = hook.get("notification_type")
        if kind == "permission_prompt":
            return ATTENTION, "needs permission"
        if kind == "elicitation_dialog":
            return ATTENTION, "needs your input"
        if kind == "idle_prompt":
            return IDLE, "waiting for you"
        return None
    if event == "PreToolUse":
        name = hook.get("tool_name") if isinstance(hook.get("tool_name"), str) else ""
        if name == "AskUserQuestion":
            state = hook.get("question_state")
            if state == "answered":
                return WORKING, "thinking"  # answered from the panel, Claude is on it
            if state == "abandoned":
                return ATTENTION, "answer in the terminal"  # the hook gave up waiting for the panel
            return ATTENTION, "asking you a question"
        return WORKING, tool_detail(name, tool_hint(name, hook.get("tool_input")))
    if event == "PostToolUse":
        return WORKING, "thinking"
    if event == "UserPromptSubmit":
        return WORKING, "working on your prompt"
    if event == "Stop":
        return IDLE, "waiting for you"
    if event == "SessionStart":
        return None if hook.get("source") == "compact" else (IDLE, "session started")
    return None


def hook_applies(facts: SessionFacts, hook: dict | None, now: float, alive: bool) -> bool:
    """Whether a hook event is fresher than everything else we know.

    Newest information wins: the hook only applies while the process is alive,
    within ``HOOK_TTL`` of its receipt, and when the transcript has not been
    written since (a tool_result after an approved permission prompt, say).
    """
    if not alive or not hook:
        return False
    try:
        ts = float(hook.get("ts") or 0)
    except (TypeError, ValueError):
        return False
    if now - ts > HOOK_TTL:
        return False
    return facts.last_ts is None or facts.last_ts.timestamp() <= ts


def apply_hook(current: tuple[str, str], facts: SessionFacts, hook: dict | None, now: float, alive: bool) -> tuple[str, str]:
    """Let a fresh hook event (see ``hook_applies``) override the transcript-derived status."""
    if not hook_applies(facts, hook, now, alive):
        return current
    return hook_override(hook) or current


def question_from_hook(hook: dict | None) -> dict | None:
    """The AskUserQuestion a PreToolUse hook is waiting on, flattened for the page.

    ``{tool_use_id, title, questions: [{question, header, options: [label, …], multi}]}``,
    or None when the hook is something else, lacks a ``tool_use_id`` or the
    question is already ``answered`` / ``abandoned`` (``question_state``).
    """
    if not hook or hook.get("hook_event_name") != "PreToolUse" or hook.get("tool_name") != "AskUserQuestion":
        return None
    if hook.get("question_state") in ("answered", "abandoned"):
        return None
    return flatten_question(hook.get("tool_use_id"), hook.get("tool_input"))


def attention_transitions(previous: dict[str, str], sessions: Iterable[dict]) -> list[dict]:
    """Sessions that just started needing the user, given the statuses of the previous round.

    Alerts on ``working -> idle`` (Claude finished its turn) and on anything
    ``-> attention`` (permission prompt or question). A session seen for the
    first time never alerts, nor does one that merely stays where it was.
    """
    alerts = []
    for s in sessions:
        before, now = previous.get(s["id"]), s["status"]
        if before is None or before == now:
            continue
        if now == ATTENTION or (now == IDLE and before == WORKING):
            alerts.append(s)
    return alerts


def prune_hooks(hooks: dict[str, dict], now: float) -> dict[str, dict]:
    """Drop hook state older than ``HOOK_TTL``."""
    return {sid: h for sid, h in hooks.items() if now - float(h.get("ts") or 0) <= HOOK_TTL}


def subagent_activity(transcript: Path, now: datetime, window: float = HEADLESS_ACTIVE_SECS) -> tuple[int, int]:
    """(total, active) subagent transcripts of ``transcript``.

    They live at ``<project>/<session-id>/subagents/*.jsonl`` and, for
    workflows, one directory deeper; ``.meta.json`` files are not agents.
    """
    root = transcript.parent / transcript.stem / "subagents"
    if not root.is_dir():
        return 0, 0
    total = active = 0
    for path in root.rglob("*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        total += 1
        if now.timestamp() - mtime < window:
            active += 1
    return total, active


def os_pid_alive(pid: int, proc: Path = Path("/proc")) -> bool:
    """True when ``pid`` exists and runs Claude Code (pids get reused after exit)."""
    try:
        cmdline = (proc / str(pid) / "cmdline").read_bytes()
    except OSError:
        return False
    return b"claude" in cmdline.lower()


def find_transcript(claude_dir: Path, session_id: str) -> Path | None:
    projects = claude_dir / "projects"
    if not projects.is_dir():
        return None
    for path in projects.glob(f"*/{session_id}.jsonl"):
        return path
    return None


def _live_sessions(claude_dir: Path, pid_alive: Callable[[int], bool]) -> list[dict]:
    result = []
    sessions_dir = claude_dir / "sessions"
    if not sessions_dir.is_dir():
        return result
    for path in sessions_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text())
            pid = int(data.get("pid") or path.stem)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not data.get("sessionId"):
            continue
        data["_alive"] = pid_alive(pid)
        for key in ("name", "messagingSocketPath"):
            if not isinstance(data.get(key), str):
                data[key] = ""
        result.append(data)
    return result


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# Parser state per transcript. Transcripts are append-only, so when a file
# only grew since the last poll just the new bytes are parsed; an unchanged
# file yields the cached facts. Keyed by (mtime_ns, size); ``offset`` is how
# far the parser has consumed (always at a line boundary).
@dataclass
class _Cached:
    key: tuple[int, int]
    parser: SessionParser
    offset: int


_facts_cache: dict[Path, _Cached] = {}


def _consume(parser: SessionParser, path: Path, start: int, end: int) -> int:
    """Feed the complete lines in ``[start, end)`` to ``parser``; return the new offset."""
    text, offset = read_new_lines(path, start, end)
    if text:
        parser.feed(iter_entries(text))
    return offset


def load_facts(path: Path) -> tuple[SessionFacts, datetime]:
    st = path.stat()
    key = (st.st_mtime_ns, st.st_size)
    cached = _facts_cache.get(path)
    if cached is not None and cached.key == key:
        facts = cached.parser.facts
    elif cached is not None and st.st_size >= cached.offset:
        cached.offset = _consume(cached.parser, path, cached.offset, st.st_size)
        cached.key = key
        facts = cached.parser.facts
    else:
        parser = SessionParser()
        data = read_transcript_bytes(path)
        facts = parser.feed(iter_entries(data.decode("utf-8", errors="replace")))
        # An unterminated last line was fed for freshness but is re-read once complete.
        unterminated = len(data) - (data.rfind(b"\n") + 1)
        _facts_cache[path] = _Cached(key, parser, st.st_size - unterminated)
    return facts, datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)


def _build(
    session_id: str,
    path: Path | None,
    alive: bool,
    started_ms: int | None,
    fallback_cwd: str,
    now: datetime,
    hook: dict | None = None,
    headless: bool = False,
    session_name: str = "",
    socket_path: str = "",
) -> Session:
    facts = SessionFacts()
    mtime: datetime | None = None
    agents = active_agents = 0
    if path is not None:
        try:
            facts, mtime = load_facts(path)
        except OSError:
            pass
        agents, active_agents = subagent_activity(path, now)
    if headless and active_agents:
        alive = True  # a subagent still writing means the headless run is not over
    status, detail = classify(facts, alive, active_agents)
    if headless and status != WORKING:
        status, detail = DONE, "finished"  # no process to wait for input
    fresh = hook_applies(facts, hook, now.timestamp(), alive)
    if fresh:
        status, detail = hook_override(hook) or (status, detail)
    cwd = facts.cwd or fallback_cwd
    started = datetime.fromtimestamp(started_ms / 1000, tz=timezone.utc) if started_ms else facts.first_ts
    last_activity = _iso(mtime or facts.last_ts)
    last_reply = facts.last_reply
    if fresh and hook.get("hook_event_name") == "Stop" and isinstance(hook.get("last_assistant_message"), str):
        last_reply = clean_text(hook["last_assistant_message"], PROMPT_MAX) or last_reply
    waiting_since = None
    if status in (IDLE, ATTENTION):
        waiting_since = _iso(datetime.fromtimestamp(float(hook["ts"]), tz=timezone.utc)) if fresh else last_activity
    # A question is answerable from the panel only while the hook script waits for
    # it; the transcript's own copy is shown for reading (answer in the terminal).
    question = question_from_hook(hook) if fresh else None
    if question is not None:
        question = {**question, "answerable": True}
    elif status == ATTENTION and facts.question is not None:
        question = {**facts.question, "answerable": False}
    return Session(
        id=session_id,
        name=facts.title or (Path(cwd).name if cwd else "session"),
        project=Path(cwd).name if cwd else "",
        cwd=cwd,
        branch=facts.branch,
        model=short_model(facts.model),
        status=status,
        detail=detail,
        context_tokens=facts.context_tokens,
        started_at=_iso(started),
        last_activity=last_activity,
        messages=facts.assistant_messages,
        agents=agents,
        active_agents=active_agents,
        last_prompt=facts.last_prompt,
        last_reply=last_reply,
        permission_mode=facts.permission_mode,
        session_name=session_name,
        can_send=bool(alive and not headless and socket_path and os.path.exists(socket_path)),
        waiting_since=waiting_since,
        question=question,
    )


def collect_sessions(
    settings: Settings,
    now: datetime | None = None,
    pid_alive: Callable[[int], bool] = os_pid_alive,
    hooks: dict[str, dict] | None = None,
) -> tuple[list[Session], dict]:
    """Return (sessions to show, summary). ``hooks`` is per-session hook state keyed by session id."""
    now = now or datetime.now(timezone.utc)
    hooks = hooks or {}
    local_midnight = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    live = _live_sessions(settings.claude_dir, pid_alive)
    # Newest process wins when a resumed session left a stale pid file behind.
    live.sort(key=lambda info: (info.get("_alive", False), info.get("startedAt") or 0), reverse=True)
    sessions: list[Session] = []
    seen: set[str] = set()
    for info in live:
        sid = info["sessionId"]
        if sid in seen:
            continue
        seen.add(sid)
        path = find_transcript(settings.claude_dir, sid)
        sessions.append(
            _build(sid, path, info["_alive"], info.get("startedAt"), info.get("cwd", ""), now, hooks.get(sid), session_name=info["name"], socket_path=info["messagingSocketPath"])
        )

    # Transcripts touched today whose process is gone: finished sessions.
    projects = settings.claude_dir / "projects"
    finished: list[tuple[float, Path]] = []
    if projects.is_dir():
        for path in projects.glob("*/*.jsonl"):
            if path.stem in seen:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime >= local_midnight.timestamp():
                finished.append((mtime, path))
    finished.sort(reverse=True)
    hidden_done = max(0, len(finished) - settings.done_sessions_limit)
    for mtime, path in finished[: settings.done_sessions_limit]:
        seen.add(path.stem)
        recent = now.timestamp() - mtime < HEADLESS_ACTIVE_SECS
        sessions.append(_build(path.stem, path, recent, None, "", now, hooks.get(path.stem), headless=True))
    for stale in [p for p in _facts_cache if p.stem not in seen]:
        _facts_cache.pop(stale, None)

    order = {ATTENTION: 0, WORKING: 1, IDLE: 2, DONE: 3}
    sessions.sort(key=lambda s: (order.get(s.status, 4), -(_epoch(s.last_activity))))
    # The summary counts everything; the page only gets the first few cards.
    summary = {
        "today": len(sessions) + hidden_done,
        "done": sum(1 for s in sessions if s.status == DONE) + hidden_done,
        "working": sum(1 for s in sessions if s.status == WORKING),
        "idle": sum(1 for s in sessions if s.status == IDLE),
        "attention": sum(1 for s in sessions if s.status == ATTENTION),
    }
    return sessions[: settings.sessions_shown], summary


def _epoch(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return 0.0
