"""Pending AskUserQuestion answers: the hook script waits, the panel resolves."""

import asyncio

from edgeboard.answers import ABANDON_AFTER, Answers
from edgeboard.collectors.claude_sessions import HOOK_TTL


def _hooks(tool_use_id="toolu_1"):
    return {"s1": {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion", "tool_use_id": tool_use_id, "ts": 1000.0}}


def test_resolve_before_wait_returns_at_once():
    hooks = _hooks()
    a = Answers(hooks)
    a.open("toolu_1", "s1", now=1000.0)
    assert a.resolve("toolu_1", "s1", {"answers": {"q": "yes"}}) is True
    assert asyncio.run(a.wait("toolu_1", timeout=0.0, now=1001.0)) == {"answers": {"q": "yes"}}
    assert hooks["s1"]["question_state"] == "answered"


def test_wait_wakes_up_when_the_panel_answers():
    a = Answers(_hooks())
    a.open("toolu_1", "s1", now=1000.0)

    async def run():
        waiter = asyncio.create_task(a.wait("toolu_1", timeout=5.0, now=1001.0))
        await asyncio.sleep(0.01)
        a.resolve("toolu_1", "s1", {"pass": True})
        return await waiter

    assert asyncio.run(run()) == {"pass": True}


def test_wait_times_out_with_none_and_records_the_poll():
    a = Answers(_hooks())
    a.open("toolu_1", "s1", now=1000.0)
    assert asyncio.run(a.wait("toolu_1", timeout=0.0, now=1030.0)) is None
    assert a.last_polled_at("toolu_1") == 1030.0
    assert asyncio.run(a.wait("unknown", timeout=0.0, now=1030.0)) is None
    assert a.session_of("toolu_1") == "s1" and a.session_of("unknown") is None


def test_resolve_rejects_unknown_wrong_session_or_abandoned():
    hooks = _hooks()
    a = Answers(hooks)
    a.open("toolu_1", "s1", now=1000.0)
    assert a.resolve("nope", "s1", {"pass": True}) is False
    assert a.resolve("toolu_1", "other", {"pass": True}) is False
    a.expire(now=1000.0 + ABANDON_AFTER + 1)  # the hook script stopped polling: it gave up
    assert a.is_abandoned("toolu_1") and hooks["s1"]["question_state"] == "abandoned"
    assert a.resolve("toolu_1", "s1", {"pass": True}) is False


def test_expire_drops_entries_after_the_hook_ttl_and_only_flags_the_matching_hook():
    hooks = _hooks(tool_use_id="toolu_2")  # the session already moved on to another question
    a = Answers(hooks)
    a.open("toolu_1", "s1", now=1000.0)
    a.expire(now=1000.0 + ABANDON_AFTER + 1)
    assert "question_state" not in hooks["s1"]
    a.expire(now=1000.0 + HOOK_TTL + 1)
    assert a.session_of("toolu_1") is None
