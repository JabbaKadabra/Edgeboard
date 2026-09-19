"""Merge the per-agent session adapters into the one ranked list the panel shows.

Each adapter (``claude_sessions``, ``codex``, ``opencode``) returns sessions in
the shared ``Session`` shape plus its own summary; enabled adapters run
independently so one tool being down or absent cannot hide another's sessions.
The merged list is ranked globally: attention first, then working, idle and
done, each by recency, and the ``sessions_shown`` best ones are returned.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from edgeboard.collectors import claude_sessions
from edgeboard.collectors.claude_sessions import Session
from edgeboard.config import Settings

STATUS_ORDER = {claude_sessions.ATTENTION: 0, claude_sessions.WORKING: 1, claude_sessions.IDLE: 2, claude_sessions.DONE: 3}
SUMMARY_KEYS = ("today", "done", "working", "idle", "attention")

Adapter = Callable[[Settings, datetime, dict[str, dict]], tuple[list[Session], dict]]


def empty_summary() -> dict:
    return {key: 0 for key in SUMMARY_KEYS}


def _epoch(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return 0.0


def merge_sessions(parts: list[tuple[list[Session], dict]], shown: int) -> tuple[list[Session], dict]:
    """Concatenate the adapters' pieces, rank them and keep the first ``shown``."""
    sessions: list[Session] = []
    summary = empty_summary()
    for sessions_part, summary_part in parts:
        sessions.extend(sessions_part)
        for key in SUMMARY_KEYS:
            summary[key] += int(summary_part.get(key) or 0)
    sessions.sort(key=lambda s: (STATUS_ORDER.get(s.status, 4), -_epoch(s.last_activity)))
    return sessions[:shown], summary


def default_adapters() -> dict[str, Adapter]:
    from edgeboard.collectors import codex, opencode

    return {"claude": _claude, "codex": codex.collect_sessions, "opencode": opencode.collect_sessions}


def _claude(settings: Settings, now: datetime, hooks: dict[str, dict]) -> tuple[list[Session], dict]:
    return claude_sessions.collect_sessions(settings, now, claude_sessions.os_pid_alive, hooks)


def collect_agents(
    settings: Settings,
    now: datetime | None = None,
    hooks: dict[str, dict] | None = None,
    adapters: dict[str, Adapter] | None = None,
) -> tuple[list[Session], dict, list[str]]:
    """Run every enabled adapter (``settings.agents``); return (sessions, summary, errors).

    An adapter that raises does not take the others down: its message is
    returned in ``errors`` and the server surfaces it under the sessions panel.
    """
    now = now or datetime.now(timezone.utc)
    hooks = hooks if hooks is not None else {}
    adapters = adapters or default_adapters()
    parts: list[tuple[list[Session], dict]] = []
    errors: list[str] = []
    for name in settings.agents:
        adapter = adapters.get(name)
        if adapter is None:
            continue
        try:
            parts.append(adapter(settings, now, hooks))
        except Exception as exc:  # noqa: BLE001 - one agent must not hide the others
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    sessions, summary = merge_sessions(parts, settings.sessions_shown)
    return sessions, summary, errors
