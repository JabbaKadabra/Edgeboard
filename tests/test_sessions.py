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


def test_load_facts_caches_until_file_changes(tmp_path):
    from xdash.collectors.claude_sessions import load_facts

    path = tmp_path / "s.jsonl"
    path.write_text(user_line("First") + "\n")
    facts1, _ = load_facts(path)
    facts2, _ = load_facts(path)
    assert facts1 is facts2 and facts1.title == "First"
    with path.open("a") as fh:
        fh.write(assistant_line("m1", stop_reason="tool_use") + "\n")
    facts3, _ = load_facts(path)
    assert facts3.last_stop_reason == "tool_use" and facts3.assistant_messages == 1


def test_collect_sessions_prefers_newest_pid_file(tmp_path):
    settings = _write_claude_dir(tmp_path)
    (tmp_path / "sessions" / "100.json").write_text(
        json.dumps({"pid": 100, "sessionId": SESSION, "cwd": "/home/me/proj", "startedAt": 1})
    )
    sessions, _ = collect_sessions(settings, pid_alive=lambda pid: pid == 4242)
    live = [s for s in sessions if s.id == SESSION]
    assert len(live) == 1 and live[0].status == WORKING


def test_summary_today_counts_finished_sessions_beyond_display_limit(tmp_path):
    settings = _write_claude_dir(tmp_path)
    projects = tmp_path / "projects" / "-home-me-proj"
    for i in range(3):
        (projects / f"77777777-0000-0000-0000-00000000000{i}.jsonl").write_text("\n".join([user_line(f"Extra {i}"), assistant_line("m")]))
    settings = Settings(claude_dir=tmp_path, done_sessions_limit=1)
    sessions, summary = collect_sessions(settings, pid_alive=lambda pid: pid == 4242)
    assert len(sessions) == 2  # one live, one displayed done
    assert summary == {"today": 5, "done": 4, "working": 1, "idle": 0}


def test_os_pid_alive_requires_claude_cmdline(tmp_path):
    from xdash.collectors.claude_sessions import os_pid_alive

    proc = tmp_path / "proc"
    (proc / "1").mkdir(parents=True)
    (proc / "1" / "cmdline").write_bytes(b"claude\x00--resume\x00")
    (proc / "2").mkdir()
    (proc / "2" / "cmdline").write_bytes(b"/usr/bin/bash\x00")
    (proc / "3").mkdir()
    (proc / "3" / "cmdline").write_bytes(b"")
    (proc / "5").mkdir()
    (proc / "5" / "cmdline").write_bytes(b"node\x00/home/me/.local/share/claude/cli.js\x00")
    assert os_pid_alive(1, proc) is True
    assert os_pid_alive(2, proc) is False  # pid reused by another program
    assert os_pid_alive(3, proc) is False  # zombie / kernel thread
    assert os_pid_alive(4, proc) is False  # gone
    assert os_pid_alive(5, proc) is True


def test_headless_session_written_just_now_is_working(tmp_path):
    projects = tmp_path / "projects" / "-home-me-proj"
    projects.mkdir(parents=True)
    busy = "aaaaaaaa-0000-0000-0000-000000000000"
    (projects / f"{busy}.jsonl").write_text("\n".join([user_line("Busy headless"), assistant_line("m1", stop_reason="tool_use")]))
    finished = "bbbbbbbb-0000-0000-0000-000000000000"
    (projects / f"{finished}.jsonl").write_text("\n".join([user_line("Finished headless"), assistant_line("m2", stop_reason="end_turn")]))
    stale = "cccccccc-0000-0000-0000-000000000000"
    p = projects / f"{stale}.jsonl"
    p.write_text("\n".join([user_line("Stale headless"), assistant_line("m3", stop_reason="tool_use")]))
    old = time.time() - 300
    os.utime(p, (old, old))
    sessions, summary = collect_sessions(Settings(claude_dir=tmp_path), pid_alive=lambda pid: False)
    status = {s.name: (s.status, s.detail) for s in sessions}
    assert status["Busy headless"] == (WORKING, "running tool")
    assert status["Finished headless"] == (DONE, "finished")
    assert status["Stale headless"] == (DONE, "finished")
    assert summary["working"] == 1


def test_load_facts_reads_only_appended_bytes(tmp_path):
    from xdash.collectors.claude_sessions import load_facts

    path = tmp_path / "s.jsonl"
    first = user_line("First prompt") + "\n"
    path.write_text(first)
    facts, _ = load_facts(path)
    assert facts.title == "First prompt"
    # Overwrite the already-parsed prefix with junk of the same length and append a
    # new line: an incremental parser keeps the old facts and only sees the new line.
    path.write_text("#" * len(first) + assistant_line("m1", stop_reason="tool_use") + "\n")
    facts2, _ = load_facts(path)
    assert facts2.title == "First prompt"
    assert facts2.last_stop_reason == "tool_use" and facts2.assistant_messages == 1


def test_load_facts_reparses_when_file_shrinks(tmp_path):
    from xdash.collectors.claude_sessions import load_facts

    path = tmp_path / "s.jsonl"
    path.write_text("\n".join([user_line("Long first prompt title here"), assistant_line("m1"), assistant_line("m2")]))
    assert load_facts(path)[0].assistant_messages == 2
    path.write_text(user_line("Short"))
    facts, _ = load_facts(path)
    assert facts.title == "Short" and facts.assistant_messages == 0


def test_load_facts_waits_for_a_complete_line(tmp_path):
    from xdash.collectors.claude_sessions import load_facts

    path = tmp_path / "s.jsonl"
    path.write_text(user_line("First") + "\n")
    load_facts(path)
    line = assistant_line("m1", stop_reason="tool_use")
    with path.open("a") as fh:
        fh.write(line[:40])  # writer is mid-line
    assert load_facts(path)[0].assistant_messages == 0
    with path.open("a") as fh:
        fh.write(line[40:] + "\n")
    facts, _ = load_facts(path)
    assert facts.assistant_messages == 1 and facts.last_stop_reason == "tool_use"
