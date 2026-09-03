import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from xdash.collectors.claude_sessions import DONE, IDLE, WORKING, classify, collect_sessions, find_transcript
from xdash.collectors.claude_transcripts import SessionFacts
from xdash.config import Settings
from tests.fixtures import SESSION, assistant_line, user_line


def test_classify_table():
    assert classify(SessionFacts(last_kind="user_prompt"), True) == (WORKING, "working on your prompt")
    assert classify(SessionFacts(last_kind="tool_result"), True) == (WORKING, "thinking")
    assert classify(SessionFacts(last_kind="assistant", last_stop_reason="tool_use"), True) == (WORKING, "running tool")
    assert classify(SessionFacts(last_kind="assistant", last_stop_reason="end_turn"), True) == (IDLE, "waiting for you")
    assert classify(SessionFacts(last_kind="assistant", last_stop_reason="end_turn"), False) == (DONE, "finished")
    assert classify(SessionFacts(), True) == (IDLE, "session started")


def _write_claude_dir(tmp_path: Path) -> Settings:
    projects = tmp_path / "projects" / "-home-me-proj"
    projects.mkdir(parents=True)
    (tmp_path / "sessions").mkdir()
    (projects / f"{SESSION}.jsonl").write_text("\n".join([user_line("Live one"), assistant_line("m1", stop_reason="tool_use")]))
    (tmp_path / "sessions" / "4242.json").write_text(
        json.dumps({"pid": 4242, "sessionId": SESSION, "cwd": "/home/me/proj", "startedAt": 1788420828212})
    )
    other = "99999999-0000-0000-0000-000000000000"
    (projects / f"{other}.jsonl").write_text("\n".join([user_line("Old one"), assistant_line("m2")]))
    old = "88888888-0000-0000-0000-000000000000"
    p = projects / f"{old}.jsonl"
    p.write_text(user_line("Yesterday"))
    two_days = time.time() - 2 * 86400
    os.utime(p, (two_days, two_days))
    return Settings(claude_dir=tmp_path)


def test_find_transcript(tmp_path):
    settings = _write_claude_dir(tmp_path)
    assert find_transcript(settings.claude_dir, SESSION).name == f"{SESSION}.jsonl"
    assert find_transcript(settings.claude_dir, "nope") is None


def test_collect_sessions(tmp_path):
    settings = _write_claude_dir(tmp_path)
    sessions, summary = collect_sessions(settings, datetime.now(timezone.utc), pid_alive=lambda pid: pid == 4242)
    by_name = {s.name: s for s in sessions}
    assert set(by_name) == {"Live one", "Old one"}  # yesterday's is excluded
    live = by_name["Live one"]
    assert live.status == WORKING and live.detail == "running tool"
    assert live.project == "proj" and live.branch == "main" and live.model == "fable-5-1"
    assert live.context_tokens == 1210
    assert live.started_at.startswith("2026-09-0")
    assert by_name["Old one"].status == DONE
    assert summary == {"today": 2, "done": 1, "working": 1, "idle": 0}
    assert sessions[0].name == "Live one"  # working sorts first


def test_collect_sessions_dead_pid_marks_done(tmp_path):
    settings = _write_claude_dir(tmp_path)
    sessions, _ = collect_sessions(settings, pid_alive=lambda pid: False)
    assert all(s.status == DONE for s in sessions)


def test_collect_sessions_missing_dir(tmp_path):
    sessions, summary = collect_sessions(Settings(claude_dir=tmp_path / "missing"))
    assert sessions == [] and summary["today"] == 0
