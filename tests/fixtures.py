"""Helpers that build Claude Code transcript lines for tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone

SESSION = "11111111-2222-3333-4444-555555555555"


def ts(hours_ago: float = 0.0, now: datetime | None = None) -> str:
    now = now or datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    from datetime import timedelta

    return (now - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def user_line(text: str = "hello", when: str | None = None, tool_result: bool = False, **extra) -> str:
    content = [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}] if tool_result else [{"type": "text", "text": text}]
    entry = {
        "type": "user",
        "uuid": extra.pop("uuid", "u-" + text[:8]),
        "parentUuid": None,
        "sessionId": SESSION,
        "cwd": "/home/me/proj",
        "gitBranch": "main",
        "timestamp": when or ts(),
        "message": {"role": "user", "content": content},
    }
    entry.update(extra)
    return json.dumps(entry)


def assistant_line(
    msg_id: str = "msg_1",
    stop_reason: str = "end_turn",
    model: str = "claude-fable-5-1",
    when: str | None = None,
    input_tokens: int = 10,
    output_tokens: int = 100,
    cache_read: int = 1000,
    cache_write: int = 200,
    tool: tuple[str, dict] | None = None,
    **extra,
) -> str:
    content = [{"type": "text", "text": "hi"}]
    if tool is not None:
        content.append({"type": "tool_use", "id": "toolu_1", "name": tool[0], "input": tool[1]})
    entry = {
        "type": "assistant",
        "uuid": extra.pop("uuid", "a-" + msg_id),
        "parentUuid": "x",
        "sessionId": SESSION,
        "cwd": "/home/me/proj",
        "gitBranch": "main",
        "timestamp": when or ts(),
        "requestId": "req_" + msg_id,
        "message": {
            "id": msg_id,
            "role": "assistant",
            "model": model,
            "stop_reason": stop_reason,
            "content": content,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_write,
            },
        },
    }
    entry.update(extra)
    return json.dumps(entry)


def summary_line(text: str) -> str:
    return json.dumps({"type": "summary", "summary": text, "leafUuid": "leaf"})


def noise_lines() -> list[str]:
    return [
        json.dumps({"type": "queue-operation", "operation": "enqueue", "sessionId": SESSION}),
        json.dumps({"type": "attachment", "attachment": {"type": "hook_success"}, "sessionId": SESSION}),
        "not json at all {",
        "",
    ]
