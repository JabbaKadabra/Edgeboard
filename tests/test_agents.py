"""The agent-agnostic merge layer: ranking, summaries and per-agent error isolation."""

from datetime import datetime, timezone

from edgeboard.collectors.claude_sessions import ATTENTION, DONE, IDLE, WORKING, Session
from edgeboard.collectors.sessions import collect_agents, merge_sessions
from edgeboard.config import Settings


def _session(sid: str, status: str, activity: str, agent: str = "claude") -> Session:
    return Session(
        id=sid,
        name=sid,
        project="proj",
        cwd="/home/me/proj",
        branch="main",
        model="m",
        status=status,
        detail="",
        context_tokens=0,
        started_at=None,
        last_activity=activity,
        messages=0,
        agent=agent,
    )


def test_merge_ranks_attention_first_then_recency():
    parts = [
        ([_session("done", DONE, "2026-09-19T12:00:00+00:00"), _session("working", WORKING, "2026-09-19T09:00:00+00:00")], {"today": 2, "done": 1, "working": 1, "idle": 0, "attention": 0}),
        ([_session("att", ATTENTION, "2026-09-19T08:00:00+00:00", "codex"), _session("idle", IDLE, "2026-09-19T07:00:00+00:00", "opencode")], {"today": 2, "done": 0, "working": 0, "idle": 1, "attention": 1}),
    ]
    sessions, summary = merge_sessions(parts, shown=3)
    assert [s.id for s in sessions] == ["att", "working", "idle"]
    assert summary == {"today": 4, "done": 1, "working": 1, "idle": 1, "attention": 1}


def test_merge_ranks_by_recency_inside_a_status():
    parts = [([_session("older", WORKING, "2026-09-19T08:00:00+00:00"), _session("newer", WORKING, "2026-09-19T10:00:00+00:00")], {})]
    sessions, _ = merge_sessions(parts, shown=5)
    assert [s.id for s in sessions] == ["newer", "older"]


def test_collect_agents_runs_only_enabled_agents_and_isolates_errors():
    def claude(settings, now, hooks):
        return [_session("c", WORKING, "2026-09-19T10:00:00+00:00")], {"today": 1, "done": 0, "working": 1, "idle": 0, "attention": 0}

    def codex(settings, now, hooks):
        raise RuntimeError("no state db")

    def opencode(settings, now, hooks):
        return [_session("o", IDLE, "2026-09-19T09:00:00+00:00", "opencode")], {"today": 1, "done": 0, "working": 0, "idle": 1, "attention": 0}

    adapters = {"claude": claude, "codex": codex, "opencode": opencode}
    settings = Settings(agents=("claude", "codex", "opencode"), sessions_shown=5)
    sessions, summary, errors = collect_agents(settings, datetime(2026, 9, 19, 12, tzinfo=timezone.utc), {}, adapters)
    assert [s.id for s in sessions] == ["c", "o"]
    assert errors == ["codex: RuntimeError: no state db"]
    assert summary["today"] == 2

    sessions, summary, errors = collect_agents(Settings(agents=("claude",)), datetime(2026, 9, 19, 12, tzinfo=timezone.utc), {}, adapters)
    assert [s.id for s in sessions] == ["c"] and errors == [] and summary["today"] == 1
