import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from edgeboard.collectors.claude_sessions import DONE, IDLE, WORKING, classify, collect_sessions, find_transcript
from edgeboard.collectors.claude_transcripts import SessionFacts
from edgeboard.config import Settings
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
    assert summary == {"today": 2, "done": 1, "working": 1, "idle": 0, "attention": 0}
    assert sessions[0].name == "Live one"  # working sorts first


def test_collect_sessions_dead_pid_marks_done(tmp_path):
    settings = _write_claude_dir(tmp_path)
    sessions, _ = collect_sessions(settings, pid_alive=lambda pid: False)
    assert all(s.status == DONE for s in sessions)


def test_collect_sessions_missing_dir(tmp_path):
    sessions, summary = collect_sessions(Settings(claude_dir=tmp_path / "missing"))
    assert sessions == [] and summary["today"] == 0


def test_load_facts_caches_until_file_changes(tmp_path):
    from edgeboard.collectors.claude_sessions import load_facts

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
    assert summary == {"today": 5, "done": 4, "working": 1, "idle": 0, "attention": 0}


def test_sessions_shown_caps_cards_but_not_summary(tmp_path):
    settings = _write_claude_dir(tmp_path)
    projects = tmp_path / "projects" / "-home-me-proj"
    for i in range(4):
        (projects / f"77777777-0000-0000-0000-00000000000{i}.jsonl").write_text("\n".join([user_line(f"Extra {i}"), assistant_line("m")]))
    settings = Settings(claude_dir=tmp_path, sessions_shown=2)
    sessions, summary = collect_sessions(settings, pid_alive=lambda pid: pid == 4242)
    assert [s.status for s in sessions] == [WORKING, DONE]  # working sorts first, then the cap applies
    assert summary == {"today": 6, "done": 5, "working": 1, "idle": 0, "attention": 0}


def test_os_pid_alive_requires_claude_cmdline(tmp_path):
    from edgeboard.collectors.claude_sessions import os_pid_alive

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
    from edgeboard.collectors.claude_sessions import load_facts

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
    from edgeboard.collectors.claude_sessions import load_facts

    path = tmp_path / "s.jsonl"
    path.write_text("\n".join([user_line("Long first prompt title here"), assistant_line("m1"), assistant_line("m2")]))
    assert load_facts(path)[0].assistant_messages == 2
    path.write_text(user_line("Short"))
    facts, _ = load_facts(path)
    assert facts.title == "Short" and facts.assistant_messages == 0


def test_load_facts_waits_for_a_complete_line(tmp_path):
    from edgeboard.collectors.claude_sessions import load_facts

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


def _tool(name, hint=""):
    return SessionFacts(last_kind="assistant", last_stop_reason="tool_use", last_tool=name, last_tool_hint=hint)


def test_classify_names_the_running_tool():
    assert classify(_tool("Bash", "ls -la"), True) == (WORKING, "running ls -la")
    assert classify(_tool("Read", "README.md"), True) == (WORKING, "reading README.md")
    assert classify(_tool("Edit", "server.py"), True) == (WORKING, "editing server.py")
    assert classify(_tool("Write", "notes.md"), True) == (WORKING, "writing notes.md")
    assert classify(_tool("Grep", '"foo"'), True) == (WORKING, 'searching "foo"')
    assert classify(_tool("Agent", "Review the diff"), True) == (WORKING, "agent: Review the diff")
    assert classify(_tool("WebFetch"), True) == (WORKING, "running WebFetch")
    assert classify(_tool("Bash"), True) == (WORKING, "running Bash")
    assert classify(_tool(""), True) == (WORKING, "running tool")


def test_classify_idle_transcript_with_active_subagent_is_working():
    idle = SessionFacts(last_kind="assistant", last_stop_reason="end_turn")
    assert classify(idle, True, active_agents=2) == (WORKING, "agents running")
    assert classify(idle, True, active_agents=0) == (IDLE, "waiting for you")
    assert classify(idle, False, active_agents=2) == (DONE, "finished")
    assert classify(_tool("Bash", "ls"), True, active_agents=1) == (WORKING, "running ls")


def test_subagent_activity_counts_total_and_recent_files(tmp_path):
    from edgeboard.collectors.claude_sessions import subagent_activity

    transcript = tmp_path / "projects" / "-home-me-proj" / f"{SESSION}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(user_line("x"))
    now = datetime.now(timezone.utc)
    assert subagent_activity(transcript, now) == (0, 0)  # no subagent dir
    agents = transcript.parent / SESSION / "subagents"
    agents.mkdir(parents=True)
    old = agents / "agent-a1.jsonl"
    old.write_text(assistant_line("m1"))
    stamp = time.time() - 300
    os.utime(old, (stamp, stamp))
    (agents / "agent-a1.meta.json").write_text("{}")  # meta files are not agents
    assert subagent_activity(transcript, now) == (1, 0)
    (agents / "agent-a2.jsonl").write_text(assistant_line("m2"))
    nested = agents / "workflows" / "wf_1"
    nested.mkdir(parents=True)
    (nested / "agent-a3.jsonl").write_text(assistant_line("m3"))
    assert subagent_activity(transcript, now) == (3, 2)


def test_collect_sessions_reports_agents_and_marks_idle_session_working(tmp_path):
    settings = _write_claude_dir(tmp_path)
    projects = tmp_path / "projects" / "-home-me-proj"
    (projects / f"{SESSION}.jsonl").write_text("\n".join([user_line("Live one"), assistant_line("m1", stop_reason="end_turn")]))
    agents = projects / SESSION / "subagents"
    agents.mkdir(parents=True)
    (agents / "agent-a1.jsonl").write_text(assistant_line("m2"))
    sessions, summary = collect_sessions(settings, pid_alive=lambda pid: pid == 4242)
    live = next(s for s in sessions if s.id == SESSION)
    assert (live.agents, live.active_agents) == (1, 1)
    assert (live.status, live.detail) == (WORKING, "agents running")
    assert summary["working"] == 1
    done = next(s for s in sessions if s.id != SESSION)
    assert (done.agents, done.active_agents) == (0, 0)


def test_headless_session_with_a_fresh_subagent_is_working(tmp_path):
    projects = tmp_path / "projects" / "-home-me-proj"
    projects.mkdir(parents=True)
    sid = "aaaaaaaa-0000-0000-0000-000000000000"
    p = projects / f"{sid}.jsonl"
    p.write_text("\n".join([user_line("Fan out"), assistant_line("m1", stop_reason="tool_use", tool=("Agent", {"description": "Review"}))]))
    old = time.time() - 300
    os.utime(p, (old, old))
    agents = projects / sid / "subagents"
    agents.mkdir(parents=True)
    (agents / "agent-a1.jsonl").write_text(assistant_line("m2"))
    sessions, _ = collect_sessions(Settings(claude_dir=tmp_path), pid_alive=lambda pid: False)
    assert (sessions[0].status, sessions[0].detail) == (WORKING, "agent: Review")  # the Agent tool_use names it


def test_session_dict_carries_last_prompt(tmp_path):
    settings = _write_claude_dir(tmp_path)
    sessions, _ = collect_sessions(settings, pid_alive=lambda pid: pid == 4242)
    live = next(s for s in sessions if s.id == SESSION)
    assert live.to_dict()["last_prompt"] == "Live one"


# ---------- hook state (POST /api/hook) ----------


def _hook(event, ts, **fields):
    return {"hook_event_name": event, "ts": ts, **fields}


def test_apply_hook_permission_prompt_needs_attention():
    from edgeboard.collectors.claude_sessions import ATTENTION, apply_hook

    facts = SessionFacts(last_kind="assistant", last_stop_reason="tool_use", last_tool="Bash", last_tool_hint="rm -rf build")
    hook = _hook("Notification", 1000.0, notification_type="permission_prompt", message="Claude needs your permission to use Bash")
    assert apply_hook((WORKING, "running rm -rf build"), facts, hook, now=1010.0, alive=True) == (ATTENTION, "needs permission")


def test_apply_hook_event_table():
    from edgeboard.collectors.claude_sessions import ATTENTION, apply_hook

    facts = SessionFacts(last_kind="assistant", last_stop_reason="end_turn")
    base = (IDLE, "waiting for you")

    def run(event, **fields):
        return apply_hook(base, facts, _hook(event, 1000.0, **fields), now=1001.0, alive=True)

    assert run("Notification", notification_type="elicitation_dialog") == (ATTENTION, "needs your input")
    assert run("Notification", notification_type="idle_prompt") == (IDLE, "waiting for you")
    assert run("Notification", notification_type="auth_success") == base
    assert run("PreToolUse", tool_name="AskUserQuestion", tool_input={}) == (ATTENTION, "asking you a question")
    assert run("PreToolUse", tool_name="Bash", tool_input={"command": "pytest -q"}) == (WORKING, "running pytest -q")
    assert run("PreToolUse", tool_name="Read", tool_input={"file_path": "/a/b.py"}) == (WORKING, "reading b.py")
    assert run("PostToolUse", tool_name="Bash") == (WORKING, "thinking")
    assert run("UserPromptSubmit", prompt="hi") == (WORKING, "working on your prompt")
    assert run("Stop") == (IDLE, "waiting for you")
    assert run("SessionStart", source="startup") == (IDLE, "session started")
    assert run("SessionStart", source="compact") == base
    assert run("SomethingNew") == base


def test_apply_hook_ignores_expired_or_dead_or_older_than_transcript():
    from edgeboard.collectors.claude_sessions import HOOK_TTL, apply_hook

    hook = _hook("Notification", 1000.0, notification_type="permission_prompt")
    facts = SessionFacts(last_kind="assistant", last_stop_reason="tool_use", last_ts=datetime.fromtimestamp(990.0, tz=timezone.utc))
    base = (WORKING, "running tool")
    assert apply_hook(base, facts, hook, now=1000.0 + HOOK_TTL + 1, alive=True) == base  # expired
    assert apply_hook((DONE, "finished"), facts, hook, now=1001.0, alive=False) == (DONE, "finished")  # gone: the pid check wins
    newer = SessionFacts(last_kind="tool_result", last_ts=datetime.fromtimestamp(1005.0, tz=timezone.utc))
    assert apply_hook((WORKING, "thinking"), newer, hook, now=1010.0, alive=True) == (WORKING, "thinking")  # the transcript moved on
    assert apply_hook(base, facts, None, now=1001.0, alive=True) == base


def test_collect_sessions_merges_hooks_and_sorts_attention_first(tmp_path):
    from edgeboard.collectors.claude_sessions import ATTENTION

    settings = _write_claude_dir(tmp_path)
    now = datetime.now(timezone.utc)
    hooks = {SESSION: _hook("Notification", now.timestamp(), notification_type="permission_prompt")}
    sessions, summary = collect_sessions(settings, now, pid_alive=lambda pid: pid == 4242, hooks=hooks)
    assert (sessions[0].id, sessions[0].status, sessions[0].detail) == (SESSION, ATTENTION, "needs permission")
    assert summary == {"today": 2, "done": 1, "working": 0, "idle": 0, "attention": 1}
    # an unknown session id in the hook map is ignored
    sessions, _ = collect_sessions(settings, now, pid_alive=lambda pid: pid == 4242, hooks={"nope": hooks[SESSION]})
    assert sessions[0].status == WORKING


# ---------- attention alerts ----------


def test_attention_transitions_only_when_a_session_starts_needing_you():
    from edgeboard.collectors.claude_sessions import attention_transitions

    def s(i, status):
        return {"id": f"s{i}", "status": status, "name": f"n{i}", "detail": ""}

    # first sighting never alerts, whatever the status
    assert attention_transitions({}, [s(1, "idle"), s(2, "attention")]) == []
    prev = {"s1": "working", "s2": "working", "s3": "idle", "s4": "attention", "s5": "working"}
    got = attention_transitions(prev, [s(1, "idle"), s(2, "attention"), s(3, "idle"), s(4, "attention"), s(5, "done"), s(6, "idle")])
    # working -> idle and anything -> attention alert; staying put, finishing and new sessions do not
    assert [x["id"] for x in got] == ["s1", "s2"]
    # idle -> attention alerts too (a question after Claude was already waiting)
    assert [x["id"] for x in attention_transitions({"s3": "idle"}, [s(3, "attention")])] == ["s3"]
    # a stopped session that comes back to idle from done does not
    assert attention_transitions({"s5": "done"}, [s(5, "idle")]) == []


# ---------- questions, replies and what the pid file knows ----------


_ASK = {
    "title": "Deploy",
    "questions": [
        {"question": "Deploy where?", "header": "Target", "options": [{"label": "staging", "description": "safe"}, {"label": "prod", "description": "live"}], "multiSelect": False},
        {"question": "Notify?", "options": [{"label": "slack"}, {"label": "mail"}], "multiSelect": True},
        "not a question",
    ],
}


def test_question_from_hook_flattens_the_tool_input():
    from edgeboard.collectors.claude_sessions import question_from_hook

    hook = _hook("PreToolUse", 1000.0, tool_name="AskUserQuestion", tool_use_id="toolu_9", tool_input=_ASK)
    assert question_from_hook(hook) == {
        "tool_use_id": "toolu_9",
        "title": "Deploy",
        "questions": [
            {"question": "Deploy where?", "header": "Target", "options": ["staging", "prod"], "multi": False},
            {"question": "Notify?", "header": "", "options": ["slack", "mail"], "multi": True},
        ],
    }


def test_question_from_hook_is_none_unless_a_pending_ask():
    from edgeboard.collectors.claude_sessions import question_from_hook

    ask = dict(_hook("PreToolUse", 1000.0, tool_name="AskUserQuestion", tool_use_id="toolu_9", tool_input=_ASK))
    assert question_from_hook(_hook("PreToolUse", 1000.0, tool_name="Bash", tool_input={"command": "ls"})) is None
    assert question_from_hook(_hook("Stop", 1000.0)) is None
    assert question_from_hook({**ask, "tool_use_id": None}) is None
    assert question_from_hook({**ask, "tool_input": {"questions": "nope"}}) is None
    assert question_from_hook({**ask, "question_state": "answered"}) is None
    assert question_from_hook({**ask, "question_state": "abandoned"}) is None


def test_hook_override_follows_the_question_state():
    from edgeboard.collectors.claude_sessions import ATTENTION, hook_override

    ask = _hook("PreToolUse", 1000.0, tool_name="AskUserQuestion", tool_use_id="toolu_9", tool_input=_ASK)
    assert hook_override(ask) == (ATTENTION, "asking you a question")
    assert hook_override({**ask, "question_state": "answered"}) == (WORKING, "thinking")
    assert hook_override({**ask, "question_state": "abandoned"}) == (ATTENTION, "answer in the terminal")


def test_hook_applies_shares_the_freshness_rule():
    from edgeboard.collectors.claude_sessions import HOOK_TTL, hook_applies

    hook = _hook("Stop", 1000.0)
    facts = SessionFacts(last_ts=datetime.fromtimestamp(990.0, tz=timezone.utc))
    assert hook_applies(facts, hook, now=1001.0, alive=True)
    assert not hook_applies(facts, hook, now=1000.0 + HOOK_TTL + 1, alive=True)
    assert not hook_applies(facts, hook, now=1001.0, alive=False)
    assert not hook_applies(SessionFacts(last_ts=datetime.fromtimestamp(1005.0, tz=timezone.utc)), hook, now=1010.0, alive=True)
    assert not hook_applies(facts, None, now=1001.0, alive=True)
    assert not hook_applies(facts, {"hook_event_name": "Stop", "ts": "soon"}, now=1001.0, alive=True)


def _live_dir(tmp_path: Path, **pid_extra) -> Settings:
    tmp_path.mkdir(exist_ok=True)
    settings = _write_claude_dir(tmp_path)
    info = {"pid": 4242, "sessionId": SESSION, "cwd": "/home/me/proj", "startedAt": 1788420828212, **pid_extra}
    (tmp_path / "sessions" / "4242.json").write_text(json.dumps(info))
    return settings


def test_session_carries_question_reply_mode_and_name(tmp_path):
    from edgeboard.collectors.claude_sessions import ATTENTION

    sock = tmp_path / "4242.sock"
    sock.touch()
    settings = _live_dir(tmp_path, name="proj-3", messagingSocketPath=str(sock))
    now = datetime.now(timezone.utc)
    hooks = {SESSION: _hook("PreToolUse", now.timestamp(), tool_name="AskUserQuestion", tool_use_id="toolu_9", tool_input=_ASK)}
    live = collect_sessions(settings, now, pid_alive=lambda pid: pid == 4242, hooks=hooks)[0][0]
    assert (live.status, live.session_name, live.can_send) == (ATTENTION, "proj-3", True)
    assert live.question["tool_use_id"] == "toolu_9" and len(live.question["questions"]) == 2
    assert live.waiting_since == datetime.fromtimestamp(hooks[SESSION]["ts"], tz=timezone.utc).isoformat()
    assert live.last_reply == "hi"  # from the transcript fixture
    assert live.permission_mode == ""
    d = live.to_dict()
    assert {"question", "last_reply", "permission_mode", "session_name", "can_send", "waiting_since"} <= set(d)


def test_stop_hook_reply_wins_while_it_applies(tmp_path):
    settings = _live_dir(tmp_path)
    now = datetime.now(timezone.utc)
    hooks = {SESSION: _hook("Stop", now.timestamp(), last_assistant_message="All done.\n\nTests pass.")}
    live = collect_sessions(settings, now, pid_alive=lambda pid: pid == 4242, hooks=hooks)[0][0]
    assert (live.status, live.last_reply) == (IDLE, "All done. Tests pass.")
    assert live.waiting_since == datetime.fromtimestamp(hooks[SESSION]["ts"], tz=timezone.utc).isoformat()
    assert live.question is None


def test_can_send_needs_a_live_process_and_an_existing_socket(tmp_path):
    settings = _live_dir(tmp_path, messagingSocketPath=str(tmp_path / "missing.sock"))
    sessions, _ = collect_sessions(settings, pid_alive=lambda pid: pid == 4242)
    by_id = {s.id: s for s in sessions}
    assert by_id[SESSION].can_send is False  # socket path does not exist
    assert all(not s.can_send for s in sessions if s.id != SESSION)  # finished sessions have no inbox
    sock = tmp_path / "4242.sock"
    sock.touch()
    settings = _live_dir(tmp_path / "with-socket", messagingSocketPath=str(sock))
    assert collect_sessions(settings, pid_alive=lambda pid: pid == 4242)[0][0].can_send is True
    assert all(not s.can_send for s in collect_sessions(settings, pid_alive=lambda pid: False)[0])


def test_waiting_since_is_only_set_while_idle_or_attention(tmp_path):
    settings = _live_dir(tmp_path)
    working = collect_sessions(settings, pid_alive=lambda pid: pid == 4242)[0][0]
    assert working.status == WORKING and working.waiting_since is None
    settings = _live_dir(tmp_path / "idle")  # a fresh dir: transcripts are parsed incrementally, not rewritten
    projects = tmp_path / "idle" / "projects" / "-home-me-proj"
    (projects / f"{SESSION}.jsonl").write_text("\n".join([user_line("Live one", permissionMode="acceptEdits"), assistant_line("m1")]))
    idle = collect_sessions(settings, pid_alive=lambda pid: pid == 4242)[0][0]
    assert idle.status == IDLE and idle.waiting_since == idle.last_activity
    assert idle.permission_mode == "acceptEdits"
