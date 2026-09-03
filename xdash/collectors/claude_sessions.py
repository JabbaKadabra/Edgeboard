"""Discover Claude Code sessions and classify what each one is doing."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from xdash.collectors.claude_transcripts import SessionFacts, iter_entries, read_transcript, session_facts, short_model
from xdash.config import Settings

WORKING = "working"
IDLE = "idle"
DONE = "done"


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

    def to_dict(self) -> dict:
        return asdict(self)


def classify(facts: SessionFacts, alive: bool) -> tuple[str, str]:
    """Return (status, detail) following the table in the design spec."""
    if not alive:
        return DONE, "finished"
    if facts.last_kind == "user_prompt":
        return WORKING, "working on your prompt"
    if facts.last_kind == "tool_result":
        return WORKING, "thinking"
    if facts.last_kind == "assistant":
        if facts.last_stop_reason == "tool_use":
            return WORKING, "running tool"
        return IDLE, "waiting for you"
    return IDLE, "session started"


def os_pid_alive(pid: int) -> bool:
    return Path("/proc", str(pid)).exists()


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
        result.append(data)
    return result


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# Parsed facts per transcript, keyed by (mtime_ns, size). Transcripts are
# append-only, so an unchanged file yields the same facts; this keeps the 2 s
# poll from re-parsing megabytes of idle sessions.
_facts_cache: dict[Path, tuple[tuple[int, int], SessionFacts]] = {}


def load_facts(path: Path) -> tuple[SessionFacts, datetime]:
    st = path.stat()
    key = (st.st_mtime_ns, st.st_size)
    cached = _facts_cache.get(path)
    if cached is not None and cached[0] == key:
        facts = cached[1]
    else:
        facts = session_facts(iter_entries(read_transcript(path)))
        _facts_cache[path] = (key, facts)
    return facts, datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)


def _build(session_id: str, path: Path | None, alive: bool, started_ms: int | None, fallback_cwd: str) -> Session:
    facts = SessionFacts()
    mtime: datetime | None = None
    if path is not None:
        try:
            facts, mtime = load_facts(path)
        except OSError:
            pass
    status, detail = classify(facts, alive)
    cwd = facts.cwd or fallback_cwd
    started = datetime.fromtimestamp(started_ms / 1000, tz=timezone.utc) if started_ms else facts.first_ts
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
        last_activity=_iso(mtime or facts.last_ts),
        messages=facts.assistant_messages,
    )


def collect_sessions(
    settings: Settings,
    now: datetime | None = None,
    pid_alive: Callable[[int], bool] = os_pid_alive,
) -> tuple[list[Session], dict]:
    now = now or datetime.now(timezone.utc)
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
        sessions.append(_build(sid, path, info["_alive"], info.get("startedAt"), info.get("cwd", "")))

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
    for _, path in finished[: settings.done_sessions_limit]:
        seen.add(path.stem)
        sessions.append(_build(path.stem, path, False, None, ""))
    for stale in [p for p in _facts_cache if p.stem not in seen]:
        _facts_cache.pop(stale, None)

    order = {WORKING: 0, IDLE: 1, DONE: 2}
    sessions.sort(key=lambda s: (order.get(s.status, 3), -(_epoch(s.last_activity))))
    summary = {
        "today": len(sessions),
        "done": sum(1 for s in sessions if s.status == DONE),
        "working": sum(1 for s in sessions if s.status == WORKING),
        "idle": sum(1 for s in sessions if s.status == IDLE),
    }
    return sessions, summary


def _epoch(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return 0.0
