"""Codex rollout parsing, status classification and hook overrides (pure; no real ~/.codex)."""

import json
import os
import time
from datetime import datetime, timedelta, timezone

from edgeboard.collectors.codex import (
    DONE,
    IDLE,
    WORKING,
    CodexParser,
    apply_codex_hook,
    classify,
    codex_hook_override,
    codex_question,
    collect_sessions,
    thread_rows,
)
from edgeboard.config import Settings

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
THREAD = "01a0b864-e4b3-76c0-96d9-35853e476d60"


def _stamp() -> str:
    """Rollout lines carry the real clock; the hook freshness window compares against it."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _line(kind: str, payload: dict, when: str | None = None) -> dict:
    return {"timestamp": when or _stamp(), "type": kind, "payload": payload}


def _meta(**extra) -> dict:
    return _line("session_meta", {"session_id": THREAD, "id": THREAD, "cwd": "/home/me/proj", "originator": "codex-tui", "cli_version": "0.155.1", **extra})


def _rollout(*entries: dict) -> list[dict]:
    return list(entries)


def test_parser_tracks_prompt_reply_and_context():
    parser = CodexParser()
    parser.feed(
        _rollout(
            _meta(),
            _line("event_msg", {"type": "task_started", "turn_id": "t1", "model_context_window": 258_400}),
            _line("response_item", {"type": "message", "id": "m1", "role": "user", "content": [{"type": "input_text", "text": "Fix the touch panel"}]}),
            _line("event_msg", {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 100, "cached_input_tokens": 40, "cache_write_input_tokens": 10}}}),
            _line("response_item", {"type": "message", "id": "m2", "role": "assistant", "content": [{"type": "output_text", "text": "I'll look at the input routing."}]}),
            _line("event_msg", {"type": "task_complete", "turn_id": "t1", "last_agent_message": "Done: the touch panel routes again."}),
        )
    )
    facts = parser.facts
    assert facts.session_id == THREAD and facts.cwd == "/home/me/proj"
    assert facts.in_turn is False and facts.last_kind == "assistant"
    assert facts.last_prompt == "Fix the touch panel"
    assert facts.last_reply == "Done: the touch panel routes again."
    assert facts.context_tokens == 150 and facts.context_window == 258_400
    assert facts.messages == 1  # only the assistant message carries an id we count


def test_parser_follows_a_running_tool_and_apply_patch():
    parser = CodexParser()
    parser.feed(
        _rollout(
            _meta(),
            _line("event_msg", {"type": "task_started", "turn_id": "t1"}),
            _line("response_item", {"type": "custom_tool_call", "name": "exec", "call_id": "c1", "input": 'const r = await tools.exec_command({cmd:"dotnet test tests/ -q"});'}),
        )
    )
    assert parser.facts.open_tool == "exec" and parser.facts.open_tool_hint == "dotnet test tests/ -q"
    parser.feed(_rollout(_line("response_item", {"type": "custom_tool_call_output", "call_id": "c1", "output": [{"type": "input_text", "text": "ok"}]})))
    assert parser.facts.open_tool == "" and parser.facts.last_kind == "tool"
    parser.feed(_rollout(_line("event_msg", {"type": "item_completed", "item": {"type": "FileChange", "id": "f1"}})))
    assert (parser.facts.last_tool, parser.facts.last_kind) == ("apply_patch", "tool")


def test_parser_flattens_request_user_input_and_clears_it_on_the_answer():
    parser = CodexParser()
    parser.feed(
        _rollout(
            _meta(),
            _line("event_msg", {"type": "task_started", "turn_id": "t1"}),
            _line(
                "response_item",
                {
                    "type": "function_call",
                    "name": "request_user_input",
                    "call_id": "q1",
                    "arguments": json.dumps({"questions": [{"header": "Target", "question": "Deploy where?", "options": [{"label": "staging"}, {"label": "prod"}]}]}),
                },
            ),
        )
    )
    question = parser.facts.question
    assert question is not None and question["tool_use_id"] == "q1"
    assert question["questions"] == [{"question": "Deploy where?", "header": "Target", "options": ["staging", "prod"], "multi": False}]
    parser.feed(_rollout(_line("response_item", {"type": "function_call_output", "call_id": "q1", "output": json.dumps({"answers": {"Target": {"answers": ["prod"]}}})})))
    assert parser.facts.question is None


def test_classify_follows_the_turn_and_the_idle_window():
    now = datetime.now(timezone.utc)
    parser = CodexParser()
    parser.feed(_rollout(_meta(), _line("event_msg", {"type": "task_started", "turn_id": "t1"})))
    mtime = time.time()
    assert classify(parser.facts, mtime, now, alive=False) == (WORKING, "thinking")  # fresh rollout, mid-turn
    parser.feed(_rollout(_line("event_msg", {"type": "task_complete", "turn_id": "t1"})))
    assert classify(parser.facts, mtime, now, alive=False) == (IDLE, "waiting for you")
    old = time.time() - 3600
    assert classify(parser.facts, old, now, alive=False) == (DONE, "finished")
    parser.feed(_rollout(_line("event_msg", {"type": "task_started", "turn_id": "t2"})))
    assert classify(parser.facts, old, now, alive=False) == (DONE, "finished")  # process gone mid-turn


def test_hook_overrides_cover_the_codex_events():
    assert codex_hook_override({"hook_event_name": "PermissionRequest"}) == ("attention", "needs permission")
    assert codex_hook_override({"hook_event_name": "PermissionRequest", "question_state": "answered"}) == (WORKING, "thinking")
    assert codex_hook_override({"hook_event_name": "UserPromptSubmit"}) == (WORKING, "working on your prompt")
    assert codex_hook_override({"hook_event_name": "Stop"}) == (IDLE, "waiting for you")
    assert codex_hook_override({"hook_event_name": "Interrupt"}) == (IDLE, "waiting for you")
    assert codex_hook_override({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls -la"}}) == (WORKING, "running ls -la")
    assert codex_hook_override({"hook_event_name": "PostToolUse"}) == (WORKING, "thinking")
    assert codex_hook_override({"hook_event_name": "SessionStart", "source": "compact"}) is None
    assert codex_hook_override({"hook_event_name": "Nope"}) is None


def test_hook_applies_only_when_fresh_and_alive():
    parser = CodexParser()
    parser.feed(_rollout(_meta(), _line("event_msg", {"type": "task_complete", "turn_id": "t1"})))
    hook = {"hook_event_name": "PermissionRequest", "ts": time.time()}
    assert apply_codex_hook((IDLE, "waiting for you"), parser.facts, hook, time.time(), alive=True) == ("attention", "needs permission")
    assert apply_codex_hook((IDLE, "waiting for you"), parser.facts, hook, time.time(), alive=False) == (IDLE, "waiting for you")
    stale = {**hook, "ts": time.time() - 3600}
    assert apply_codex_hook((IDLE, "waiting for you"), parser.facts, stale, time.time(), alive=True) == (IDLE, "waiting for you")


def test_permission_hook_becomes_an_answerable_question():
    parser = CodexParser()
    parser.feed(_rollout(_meta()))
    hook = {"hook_event_name": "PermissionRequest", "tool_use_id": "perm-1", "tool_name": "Bash", "tool_input": {"command": "git push"}, "ts": time.time()}
    question = codex_question(hook, parser.facts, time.time(), alive=True)
    assert question is not None and question["answerable"] is True
    assert question["questions"][0]["options"] == ["Allow", "Deny"]
    assert "git push" in question["questions"][0]["question"]
    assert codex_question({**hook, "question_state": "answered"}, parser.facts, time.time(), alive=True) is None


def _write_codex_dir(tmp_path, entries: list[dict], mtime: float | None = None, day: datetime | None = None, name: str = THREAD) -> Settings:
    day = day or datetime.now(timezone.utc)
    directory = tmp_path / "sessions" / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-{day.strftime('%Y-%m-%dT%H-%M-%S')}-{name}.jsonl"
    path.write_text("\n".join(json.dumps(entry) for entry in entries))
    if mtime:
        os.utime(path, (mtime, mtime))
    return Settings(codex_dir=tmp_path, agents=("codex",))


def test_collect_sessions_reads_todays_rollouts(tmp_path):
    settings = _write_codex_dir(
        tmp_path,
        [
            _meta(),
            _line("event_msg", {"type": "task_started", "turn_id": "t1"}),
            _line("response_item", {"type": "message", "id": "m1", "role": "user", "content": [{"type": "input_text", "text": "Fix the touch panel"}]}),
            _line("event_msg", {"type": "task_complete", "turn_id": "t1", "last_agent_message": "It routes again."}),
        ],
    )
    sessions, summary = collect_sessions(settings, datetime.now(timezone.utc), {})
    assert len(sessions) == 1
    session = sessions[0]
    assert session.agent == "codex" and session.id == THREAD
    assert session.status == IDLE and session.name == "Fix the touch panel"
    assert session.project == "proj" and session.last_reply == "It routes again."
    assert session.can_send is True  # a fresh rollout tail, no process needed
    assert summary["today"] == 1 and summary["idle"] == 1


def test_collect_sessions_skips_subagents(tmp_path):
    subagent = "01a0b554-fbe4-7753-9a6a-61dc18ca10c2"
    settings = _write_codex_dir(
        tmp_path,
        [
            _meta(),
            _line("event_msg", {"type": "task_started", "turn_id": "t1"}),
            _line("response_item", {"type": "message", "id": "m1", "role": "user", "content": [{"type": "input_text", "text": "Guard this"}]}),
        ],
    )
    # a subagent gets its own rollout file; its parent thread counts it as an agent
    _write_codex_dir(
        tmp_path,
        [_line("session_meta", {"session_id": subagent, "parent_thread_id": THREAD, "source": {"subagent": {"other": "guardian"}}, "thread_source": "guardian_review", "cwd": "/home/me/proj"})],
        name=subagent,
    )
    sessions, summary = collect_sessions(settings, datetime.now(timezone.utc), {})
    assert [s.id for s in sessions] == [THREAD] and sessions[0].agents == 1


def test_thread_rows_are_read_from_the_state_database(tmp_path):
    import sqlite3

    db = tmp_path / "state_5.sqlite"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, name TEXT, git_branch TEXT, model TEXT, archived INTEGER, created_at INTEGER)")
    connection.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)", (THREAD, "Touch panel fix", "touchy", "fix/touch", "gpt-6-astra", 0, 1789800015000))
    connection.commit()
    connection.close()
    rows = thread_rows(tmp_path, [THREAD])
    assert rows[THREAD]["title"] == "Touch panel fix" and rows[THREAD]["git_branch"] == "fix/touch"
    assert thread_rows(tmp_path, ["nope"]) == {}


def test_collect_sessions_uses_thread_metadata_and_hooks(tmp_path):
    settings = _write_codex_dir(
        tmp_path,
        [
            _meta(),
            _line("event_msg", {"type": "task_started", "turn_id": "t1"}),
            _line("response_item", {"type": "message", "id": "m1", "role": "user", "content": [{"type": "input_text", "text": "Deploy the fix"}]}),
        ],
    )
    hook = {"hook_event_name": "PermissionRequest", "tool_use_id": "perm-9", "tool_name": "Bash", "tool_input": {"command": "git push"}, "ts": time.time()}
    sessions, _ = collect_sessions(settings, datetime.now(timezone.utc), {THREAD: hook})
    session = sessions[0]
    assert session.status == "attention" and session.detail == "needs permission"
    assert session.question["answerable"] is True
    assert session.waiting_since is not None


def test_collect_sessions_ignores_yesterdays_quiet_rollout(tmp_path):
    old = time.time() - 2 * 86400
    day = datetime.now(timezone.utc) - timedelta(days=2)
    settings = _write_codex_dir(tmp_path, [_meta()], mtime=old, day=day)
    sessions, summary = collect_sessions(settings, datetime.now(timezone.utc), {})
    assert sessions == [] and summary["today"] == 0
