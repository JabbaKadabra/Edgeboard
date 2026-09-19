"""Codex CLI sessions, read from its rollout files, state database and hooks.

Codex writes one append-only rollout JSONL per thread under
``<codex_dir>/sessions/YYYY/MM/DD/rollout-*.jsonl`` and keeps title, branch and
archive state in ``<codex_dir>/state_*.sqlite`` (table ``threads``). The
lifecycle hooks documented at developers.openai.com/codex/hooks carry almost
the same JSON as Claude Code's (``session_id``, ``hook_event_name``, ``cwd``,
``model``, ``permission_mode``, ``Stop.last_assistant_message``), so a fresh
hook overrides the rollout-derived status the same way it does for Claude; a
``PermissionRequest`` can even be answered from the panel (the hook script
long-polls, see scripts/edgeboard-hook.py).

Prompts are queued with ``codex queue --thread <id> --message <text>``.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

from edgeboard.collectors.claude_sessions import (
    ATTENTION,
    DONE,
    HEADLESS_ACTIVE_SECS,
    HOOK_TTL,
    IDLE,
    WORKING,
    Session,
    hook_applies,
    tool_detail,
)
from edgeboard.collectors.claude_transcripts import (
    PROMPT_MAX,
    clean_prompt,
    clean_text,
    iter_entries,
    parse_ts,
    read_new_lines,
    read_transcript_bytes,
)
from edgeboard.collectors.sessions import empty_summary
from edgeboard.config import Settings

# A finished turn stays "idle" (and therefore sendable) this long, then counts as done.
IDLE_WINDOW = 30 * 60.0
_CMD_RE = re.compile(r"tools\.exec_command\(\{cmd:\s*\"((?:[^\"\\]|\\.)*)\"", re.S)


@dataclass
class CodexFacts:
    session_id: str = ""
    parent_thread_id: str = ""
    originator: str = ""
    thread_source: str = ""
    cwd: str = ""
    model: str = ""
    title: str = ""
    context_window: int = 0
    context_tokens: int = 0
    in_turn: bool = False
    last_kind: str = ""  # "user_prompt" | "assistant" | "tool" | ""
    last_tool: str = ""
    last_tool_hint: str = ""
    open_tool: str = ""  # a tool call whose output has not been written yet
    open_tool_hint: str = ""
    last_prompt: str = ""
    last_reply: str = ""
    messages: int = 0
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    compactions: int = 0
    last_compact_ts: datetime | None = None
    question: dict | None = None  # a pending request_user_input, answerable only through the app server

    @property
    def is_subagent(self) -> bool:
        return bool(self.parent_thread_id) or self.thread_source in ("guardian_review", "subagent")


def _content_text(message: dict) -> str:
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(str(b.get("text")) for b in content if isinstance(b, dict) and b.get("type") in ("input_text", "output_text", "text") and b.get("text"))


def _tool_hint(name: str, raw) -> str:
    """A short description of a Codex tool call (an ``exec`` JS snippet, a patch, …)."""
    if isinstance(raw, str):
        match = _CMD_RE.search(raw)
        if match:
            return clean_text(match.group(1).replace('\\"', '"').replace("\\n", " "), 40)
        text = re.sub(r"\s+", " ", raw).strip()
        return text[:39].rstrip() + "…" if len(text) > 40 else text
    if isinstance(raw, dict):
        for key in ("command", "cmd", "path", "file_path"):
            value = raw.get(key)
            if isinstance(value, str) and value:
                return Path(value).name if key in ("path", "file_path") else clean_text(value, 40)
    return ""


def _flatten_request_user_input(tool_use_id: str, arguments) -> dict | None:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return None
    if not isinstance(arguments, dict):
        return None
    questions = []
    for item in arguments.get("questions") or []:
        if not isinstance(item, dict):
            continue
        question = item.get("question") or item.get("header")
        if not isinstance(question, str) or not question:
            continue
        options = [o.get("label") for o in item.get("options") or [] if isinstance(o, dict) and isinstance(o.get("label"), str)]
        questions.append({"question": question, "header": str(item.get("header") or ""), "options": options, "multi": bool(item.get("multiSelect"))})
    if not questions:
        return None
    return {"tool_use_id": tool_use_id, "title": "Question", "questions": questions}


def _answer_from_output(entry: dict) -> str | None:
    """The answers a ``request_user_input`` output carries, if any."""
    if entry.get("type") == "function_call_output":
        output = entry.get("output")
    elif entry.get("type") == "custom_tool_call_output":
        blocks = entry.get("output")
        output = " ".join(str(b.get("text")) for b in blocks if isinstance(b, dict)) if isinstance(blocks, list) else None
    else:
        return None
    if isinstance(output, str) and '"answers"' in output:
        return clean_text(output, PROMPT_MAX)
    return None


class CodexParser:
    """Incremental ``CodexFacts`` builder: feed rollout records as the file grows."""

    def __init__(self) -> None:
        self.facts = CodexFacts()
        self._first_prompt = ""
        self._seen_messages: set[str] = set()
        self._open_calls: dict[str, tuple[str, str]] = {}  # call id -> (name, hint)
        self._open_call_id = ""  # the call whose tool is still running (drives the "now" line)

    def feed(self, entries: Iterable[dict]) -> CodexFacts:
        facts = self.facts
        for entry in entries:
            try:
                self._entry(entry)
            except (AttributeError, TypeError, ValueError):
                continue  # one mis-shaped record must not take the file down
        facts.messages = len(self._seen_messages)
        return facts

    def _entry(self, entry: dict) -> None:
        facts = self.facts
        kind = entry.get("type")
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        ts = parse_ts(entry.get("timestamp"))
        if ts is not None:
            facts.first_ts = facts.first_ts or ts
            facts.last_ts = ts
        if kind == "session_meta":
            facts.session_id = str(payload.get("session_id") or payload.get("id") or "")
            facts.parent_thread_id = str(payload.get("parent_thread_id") or "")
            facts.originator = str(payload.get("originator") or "")
            source = payload.get("source")
            if isinstance(source, dict) and "subagent" in source:
                facts.thread_source = "subagent"
            facts.thread_source = str(payload.get("thread_source") or facts.thread_source)
            facts.cwd = str(payload.get("cwd") or facts.cwd)
            window = payload.get("context_window")
            if isinstance(window, (int, float)) and window > 0:
                facts.context_window = int(window)
            return
        if kind == "turn_context":
            facts.cwd = str(payload.get("cwd") or facts.cwd)
            return
        if kind == "world_state":
            state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
            mode = state.get("collaboration_mode") if isinstance(state.get("collaboration_mode"), dict) else {}
            model = mode.get("model") if isinstance(mode, dict) else None
            if isinstance(model, str) and model:
                facts.model = model
            return
        if kind == "event_msg":
            self._event(payload)
            return
        if kind == "response_item":
            self._item(payload)
            return
        if kind == "token_usage_record":
            usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
            self._context(usage)
            return
        if kind == "compacted":
            facts.compactions += 1
            facts.last_compact_ts = parse_ts(entry.get("timestamp")) or facts.last_compact_ts

    def _event(self, payload: dict) -> None:
        facts = self.facts
        event = payload.get("type")
        if event == "task_started":
            facts.in_turn = True
            facts.last_kind = "user_prompt"
            window = payload.get("model_context_window")
            if isinstance(window, (int, float)) and window > 0:
                facts.context_window = int(window)
        elif event == "task_complete":
            facts.in_turn = False
            facts.last_kind = "assistant"
            facts.open_tool = facts.open_tool_hint = ""
            reply = payload.get("last_agent_message")
            if isinstance(reply, str) and reply:
                facts.last_reply = clean_text(reply, PROMPT_MAX)
        elif event == "item_completed":
            item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
            self._completed_item(item)
        elif event == "token_count":
            info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
            usage = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else {}
            self._context(usage)
        elif event == "user_message":
            message = payload.get("message")
            if isinstance(message, str) and message:
                self._user_prompt(message)
        elif event == "agent_message":
            message = payload.get("message")
            if isinstance(message, str) and message:
                reply = clean_text(message, PROMPT_MAX)
                if reply:
                    facts.last_reply = reply

    def _completed_item(self, item: dict) -> None:
        facts = self.facts
        item_type = item.get("type")
        if item_type == "UserMessage":
            self._user_prompt(_plain_content(item))
        elif item_type == "AgentMessage":
            reply = clean_text(_plain_content(item), PROMPT_MAX)
            if reply:
                facts.last_kind = "assistant"
                facts.last_reply = reply
        elif item_type == "CommandExecution":
            command = item.get("command")
            if isinstance(command, list) and command:
                hint = clean_text(str(command[-1]), 40)
                if hint:
                    facts.last_tool, facts.last_tool_hint = "Bash", hint
                    facts.last_kind = "tool"
        elif item_type == "FileChange":
            facts.last_tool, facts.last_tool_hint = "apply_patch", "files"
            facts.last_kind = "tool"
        elif item_type == "ContextCompaction":
            facts.compactions += 1
            facts.last_compact_ts = datetime.now(timezone.utc)

    def _context(self, usage: dict) -> None:
        if not isinstance(usage, dict) or not usage:
            return
        try:
            self.facts.context_tokens = int(usage.get("input_tokens") or 0) + int(usage.get("cached_input_tokens") or 0) + int(usage.get("cache_write_input_tokens") or 0)
        except (TypeError, ValueError):
            pass

    def _user_prompt(self, text: str) -> None:
        facts = self.facts
        prompt = clean_text(text, PROMPT_MAX)
        if not prompt:
            return
        facts.last_kind = "user_prompt"
        facts.last_prompt = prompt
        if not self._first_prompt:
            self._first_prompt = clean_prompt(text)
            facts.title = self._first_prompt

    def _item(self, payload: dict) -> None:
        facts = self.facts
        item_type = payload.get("type")
        if item_type == "message":
            role = payload.get("role")
            text = _content_text(payload)
            if role == "user":
                self._user_prompt(text)
            elif role == "assistant" and text.strip():
                message_id = str(payload.get("id") or "")
                if message_id:
                    self._seen_messages.add(message_id)
                reply = clean_text(text, PROMPT_MAX)
                if reply:
                    facts.last_kind = "assistant"
                    facts.last_reply = reply
            return
        if item_type in ("custom_tool_call", "function_call"):
            name = str(payload.get("name") or "")
            call_id = str(payload.get("call_id") or payload.get("id") or "")
            raw = payload.get("input") if item_type == "custom_tool_call" else payload.get("arguments")
            if name == "request_user_input":
                question = _flatten_request_user_input(call_id, raw)
                if question is not None:
                    facts.question = question
                if call_id:
                    self._open_calls[call_id] = (name, "")
                facts.last_kind = "tool"
                facts.last_tool, facts.last_tool_hint = "request_user_input", ""
                return
            hint = _tool_hint(name, raw)
            if call_id:
                self._open_calls[call_id] = (name, hint)
                self._open_call_id = call_id
            facts.open_tool, facts.open_tool_hint = name, hint
            facts.last_kind = "tool"
            facts.last_tool, facts.last_tool_hint = name, hint
            return
        if item_type in ("custom_tool_call_output", "function_call_output"):
            call_id = str(payload.get("call_id") or "")
            answered = _answer_from_output(payload)
            if answered is not None and facts.question is not None and call_id in self._open_calls:
                facts.question = None
            self._open_calls.pop(call_id, None)
            if call_id and call_id == self._open_call_id:
                facts.open_tool = facts.open_tool_hint = ""
                self._open_call_id = ""
            facts.last_kind = "tool"
            return
        if item_type == "reasoning":
            return


def _plain_content(item: dict) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text")) for b in content if isinstance(b, dict) and b.get("type") == "Text" and b.get("text"))
    return ""


# --- discovery ---------------------------------------------------------------


@dataclass
class _Cached:
    key: tuple[int, int]
    parser: CodexParser
    offset: int


_facts_cache: dict[Path, _Cached] = {}


def load_facts(path: Path) -> tuple[CodexFacts, float]:
    """Parse ``path`` incrementally (append-only); returns (facts, mtime)."""
    st = path.stat()
    key = (st.st_mtime_ns, st.st_size)
    cached = _facts_cache.get(path)
    if cached is not None and cached.key == key:
        facts = cached.parser.facts
    elif cached is not None and st.st_size >= cached.offset:
        text, offset = read_new_lines(path, cached.offset, st.st_size)
        cached.offset = offset
        cached.key = key
        facts = cached.parser.feed(iter_entries(text))
    else:
        parser = CodexParser()
        data = read_transcript_bytes(path)
        facts = parser.feed(iter_entries(data.decode("utf-8", errors="replace")))
        unterminated = len(data) - (data.rfind(b"\n") + 1)
        _facts_cache[path] = _Cached(key, parser, st.st_size - unterminated)
    return facts, st.st_mtime


def _local_midnight(now: datetime) -> float:
    return now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _iter_todays_rollouts(codex_dir: Path, now: datetime) -> Iterator[Path]:
    """Rollout files under today's (and yesterday's) day directory, touched since midnight.

    A resumed thread keeps appending to its original file, which can live in an
    older day directory, so the two newest days are scanned.
    """
    root = codex_dir / "sessions"
    if not root.is_dir():
        return
    midnight = _local_midnight(now)
    local_now = now.astimezone()
    days = [local_now, local_now - timedelta(days=1)]
    for day in days:
        directory = root / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("rollout-*.jsonl")):
            try:
                if path.stat().st_mtime >= midnight:
                    yield path
            except OSError:
                continue


def thread_rows(codex_dir: Path, ids: list[str] | None = None) -> dict[str, dict]:
    """Title/branch/archive info from the newest ``state_*.sqlite``; {} when unreadable."""
    candidates = sorted(codex_dir.glob("state_*.sqlite"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    for path in candidates:
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
            connection.row_factory = sqlite3.Row
            try:
                if ids:
                    marks = ",".join("?" for _ in ids)
                    rows = connection.execute(f"SELECT id, title, name, git_branch, model, archived, created_at FROM threads WHERE id IN ({marks})", ids).fetchall()
                else:
                    rows = connection.execute("SELECT id, title, name, git_branch, model, archived, created_at FROM threads").fetchall()
            finally:
                connection.close()
            return {str(row["id"]): dict(row) for row in rows}
        except (sqlite3.Error, OSError):
            continue
    return {}


def apply_codex_hook(current: tuple[str, str], facts: CodexFacts, hook: dict | None, now: float, alive: bool) -> tuple[str, str]:
    """Let a fresh Codex hook (same freshness rule as Claude's) override the rollout status."""
    if not hook_applies(facts_to_shared(facts), hook, now, alive):
        return current
    return codex_hook_override(hook) or current


def facts_to_shared(facts: CodexFacts):
    """A ``SessionFacts``-shaped view so ``hook_applies`` can compare timestamps."""
    from edgeboard.collectors.claude_transcripts import SessionFacts

    return SessionFacts(last_ts=facts.last_ts)


def codex_hook_override(hook: dict) -> tuple[str, str] | None:
    event = hook.get("hook_event_name")
    if event == "PermissionRequest":
        state = hook.get("question_state")
        if state == "answered":
            return WORKING, "thinking"
        if state == "abandoned":
            return ATTENTION, "answer in the terminal"
        return ATTENTION, "needs permission"
    if event == "PreToolUse":
        name = str(hook.get("tool_name") or "")
        tool_input = hook.get("tool_input")
        if name == "request_user_input":
            return ATTENTION, "asking you a question"
        if name in ("apply_patch", "Edit", "Write"):
            hint = ""
            if isinstance(tool_input, dict):
                hint = _tool_hint("apply_patch", tool_input)
            return WORKING, f"editing {hint}".strip()
        return WORKING, tool_detail(name, _tool_hint(name, tool_input))
    if event == "PostToolUse":
        return WORKING, "thinking"
    if event == "UserPromptSubmit":
        return WORKING, "working on your prompt"
    if event in ("Stop", "Interrupt"):
        return IDLE, "waiting for you"
    if event == "SessionStart":
        return None if hook.get("source") == "compact" else (IDLE, "session started")
    return None


def codex_question(hook: dict | None, facts: CodexFacts, now: float, alive: bool) -> dict | None:
    """The pending question: a hook's permission/request_user_input, else the rollout's."""
    if hook_applies(facts_to_shared(facts), hook, now, alive):
        event = hook.get("hook_event_name")
        if event == "PermissionRequest" and hook.get("question_state") not in ("answered", "abandoned"):
            question = _flatten_permission(hook)
            if question is not None:
                return {**question, "answerable": True}
        if event == "PreToolUse" and hook.get("tool_name") == "request_user_input":
            question = _flatten_request_user_input(str(hook.get("tool_use_id") or ""), hook.get("tool_input"))
            if question is not None:
                return {**question, "answerable": False}
    if facts.question is not None:
        return {**facts.question, "answerable": False}
    return None


def _flatten_permission(hook: dict) -> dict | None:
    tool_use_id = hook.get("tool_use_id")
    if not isinstance(tool_use_id, str) or not tool_use_id:
        return None
    name = str(hook.get("tool_name") or "action")
    tool_input = hook.get("tool_input") if isinstance(hook.get("tool_input"), dict) else {}
    description = tool_input.get("description")
    hint = description if isinstance(description, str) and description else _tool_hint(name, tool_input)
    question = f"Allow {name}" + (f": {hint}" if hint else "?")
    return {"tool_use_id": tool_use_id, "title": "Permission", "questions": [{"question": question, "header": name, "options": ["Allow", "Deny"], "multi": False}]}


def classify(facts: CodexFacts, mtime: float, now: datetime, alive: bool) -> tuple[str, str]:
    """(status, detail) from the rollout tail, used when no fresh hook says otherwise."""
    active = alive or now.timestamp() - mtime < HEADLESS_ACTIVE_SECS
    if facts.in_turn and active:
        if facts.open_tool:
            return WORKING, tool_detail(facts.open_tool, facts.open_tool_hint)
        return WORKING, "thinking"
    if facts.in_turn:
        return DONE, "finished"  # the process went away mid-turn
    if not facts.in_turn and facts.last_kind:
        if now.timestamp() - mtime < IDLE_WINDOW:
            return IDLE, "waiting for you"
        return DONE, "finished"
    return (IDLE, "session started") if active else (DONE, "finished")


def collect_sessions(settings: Settings, now: datetime, hooks: dict[str, dict]) -> tuple[list[Session], dict]:
    """Codex threads touched today as panel sessions, ranked with the other agents'."""
    now_epoch = now.timestamp()
    parsed: list[tuple[Path, CodexFacts, float]] = []
    for path in _iter_todays_rollouts(settings.codex_dir, now):
        try:
            facts, mtime = load_facts(path)
        except OSError:
            continue
        if facts.session_id:
            parsed.append((path, facts, mtime))
    rows = thread_rows(settings.codex_dir, [facts.session_id for _, facts, _ in parsed])
    subagents: dict[str, int] = {}
    for _, facts, _ in parsed:
        if facts.is_subagent and facts.parent_thread_id:
            subagents[facts.parent_thread_id] = subagents.get(facts.parent_thread_id, 0) + 1

    sessions: list[Session] = []
    for _path, facts, mtime in parsed:
        if facts.is_subagent:
            continue
        thread_id = facts.session_id
        row = rows.get(thread_id) or {}
        if row.get("archived"):
            continue
        hook = hooks.get(thread_id)
        alive = bool(hook) and now_epoch - float(hook.get("ts") or 0) <= HOOK_TTL
        status, detail = classify(facts, mtime, now, alive)
        status, detail = apply_codex_hook((status, detail), facts, hook, now_epoch, alive)
        last_activity = datetime.fromtimestamp(mtime, tz=timezone.utc)
        waiting_since = None
        if status in (IDLE, ATTENTION):
            if alive:
                waiting_since = datetime.fromtimestamp(float(hook.get("ts") or mtime), tz=timezone.utc).isoformat()
            else:
                waiting_since = last_activity.isoformat()
        last_reply = facts.last_reply
        if hook and hook_applies(facts_to_shared(facts), hook, now_epoch, alive) and hook.get("hook_event_name") == "Stop" and isinstance(hook.get("last_assistant_message"), str):
            last_reply = clean_text(hook["last_assistant_message"], PROMPT_MAX) or last_reply
        model = str(row.get("model") or "") or str((hook or {}).get("model") or "") or facts.model
        window = facts.context_window or settings.context_window
        created = row.get("created_at")
        if isinstance(created, (int, float)) and created:
            started = datetime.fromtimestamp(created / 1000 if created > 1e12 else created, tz=timezone.utc)  # created_at is seconds, created_at_ms milliseconds
        else:
            started = facts.first_ts
        title = str(row.get("title") or "").strip() or facts.title or (Path(facts.cwd).name if facts.cwd else "codex session")
        can_send = status != DONE and bool(shutil.which("codex"))
        sessions.append(
            Session(
                id=thread_id,
                name=clean_prompt(title) or "codex session",
                project=Path(facts.cwd).name if facts.cwd else "",
                cwd=facts.cwd,
                branch=str(row.get("git_branch") or ""),
                model=model,
                status=status,
                detail=detail,
                context_tokens=facts.context_tokens,
                started_at=started.isoformat() if started else None,
                last_activity=last_activity.isoformat(),
                messages=facts.messages,
                agents=subagents.get(thread_id, 0),
                last_prompt=facts.last_prompt,
                last_reply=last_reply,
                session_name=str(row.get("name") or ""),
                agent="codex",
                agent_detail=facts.originator,
                can_send=can_send,
                waiting_since=waiting_since,
                question=codex_question(hook, facts, now_epoch, alive),
                context_window=window,
                context_pct=round(100 * facts.context_tokens / window) if window else 0,
                compactions=facts.compactions,
                last_compact_at=facts.last_compact_ts.isoformat() if facts.last_compact_ts else None,
            )
        )
    for stale in [p for p in _facts_cache if p not in {path for path, _, _ in parsed}]:
        _facts_cache.pop(stale, None)

    sessions.sort(key=lambda s: -(parse_ts(s.last_activity).timestamp() if parse_ts(s.last_activity) else 0))
    # Keep every session that is not done; cap only the done ones (the merged
    # ranking trims the rest and the summary still counts everything).
    kept: list[Session] = []
    done_kept = 0
    for session in sessions:
        if session.status != DONE or done_kept < settings.done_sessions_limit:
            kept.append(session)
            if session.status == DONE:
                done_kept += 1
    summary = empty_summary()
    summary["today"] = len(sessions)
    summary["done"] = sum(1 for s in sessions if s.status == DONE)
    summary["working"] = sum(1 for s in sessions if s.status == WORKING)
    summary["idle"] = sum(1 for s in sessions if s.status == IDLE)
    summary["attention"] = sum(1 for s in sessions if s.status == ATTENTION)
    return kept, summary


def queue_message(thread_id: str, text: str) -> None:
    """Hand a prompt to ``codex queue`` for an existing thread (raises on failure)."""
    import subprocess

    binary = shutil.which("codex")
    if not binary:
        raise RuntimeError("the codex CLI is not on PATH")
    result = subprocess.run([binary, "queue", "--thread", thread_id, "--message", text], capture_output=True, text=True, timeout=30)
    if result.returncode != 0 or "Error:" in result.stderr:
        message = (result.stderr or result.stdout).strip().splitlines()[-1] if (result.stderr or result.stdout) else "codex queue failed"
        raise RuntimeError(message)
