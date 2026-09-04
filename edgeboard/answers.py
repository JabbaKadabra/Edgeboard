"""Pending AskUserQuestion answers, shared by the hook script and the panel.

The PreToolUse hook (scripts/edgeboard-hook.py) posts the question to
``/api/hook`` and then long-polls ``GET /api/answer/{tool_use_id}``; a tap on
the panel resolves it through ``POST /api/sessions/{id}/answer``. Entries are
memory-only: after a restart the hook gets a 404 and the terminal dialog
takes over. When the script stops polling (its wait ran out) the question is
``abandoned`` and the card says so; either state is written into the
session's hook dict (``question_state``) where ``question_from_hook`` and
``hook_override`` read it.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from edgeboard.collectors.claude_sessions import HOOK_TTL

# The hook script polls every ~25 s; no poll for this long means it gave up.
ABANDON_AFTER = 35.0


@dataclass
class _Pending:
    session_id: str
    opened_at: float
    last_polled_at: float
    result: dict | None = None
    abandoned: bool = False
    waiters: list[asyncio.Future] = field(default_factory=list)


class Answers:
    def __init__(self, hooks: dict[str, dict]):
        self._hooks = hooks  # State.hooks, flagged in place
        self._pending: dict[str, _Pending] = {}

    def open(self, tool_use_id: str, session_id: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._pending[tool_use_id] = _Pending(session_id, now, now)

    def session_of(self, tool_use_id: str) -> str | None:
        entry = self._pending.get(tool_use_id)
        return entry.session_id if entry else None

    def last_polled_at(self, tool_use_id: str) -> float | None:
        entry = self._pending.get(tool_use_id)
        return entry.last_polled_at if entry else None

    def is_abandoned(self, tool_use_id: str) -> bool:
        entry = self._pending.get(tool_use_id)
        return bool(entry and entry.abandoned)

    async def wait(self, tool_use_id: str, timeout: float, now: float | None = None) -> dict | None:
        """Block until the panel answers ``tool_use_id`` or ``timeout`` passes (None)."""
        entry = self._pending.get(tool_use_id)
        if entry is None:
            return None
        entry.last_polled_at = time.time() if now is None else now
        if entry.result is not None:
            return entry.result
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        entry.waiters.append(fut)
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            if fut in entry.waiters:
                entry.waiters.remove(fut)

    def resolve(self, tool_use_id: str, session_id: str, result: dict) -> bool:
        entry = self._pending.get(tool_use_id)
        if entry is None or entry.session_id != session_id or entry.abandoned or entry.result is not None:
            return False
        entry.result = result
        for fut in entry.waiters:
            if not fut.done():
                fut.set_result(result)
        self._flag(entry, tool_use_id, "answered")
        return True

    def expire(self, now: float | None = None) -> None:
        """Drop entries older than ``HOOK_TTL``; mark the ones the hook script stopped polling as abandoned."""
        now = time.time() if now is None else now
        for tool_use_id, entry in list(self._pending.items()):
            if now - entry.opened_at > HOOK_TTL:
                del self._pending[tool_use_id]
            elif entry.result is None and not entry.abandoned and now - entry.last_polled_at > ABANDON_AFTER:
                entry.abandoned = True
                self._flag(entry, tool_use_id, "abandoned")

    def _flag(self, entry: _Pending, tool_use_id: str, state: str) -> None:
        hook = self._hooks.get(entry.session_id)
        if hook and hook.get("tool_use_id") == tool_use_id:
            hook["question_state"] = state
