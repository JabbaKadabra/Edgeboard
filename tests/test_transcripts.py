from edgeboard.collectors.claude_transcripts import (
    clean_prompt,
    iter_entries,
    session_facts,
    short_model,
    usage_events,
)
from tests.fixtures import assistant_line, noise_lines, summary_line, ts, user_line


def test_iter_entries_skips_malformed():
    text = "\n".join(noise_lines() + [user_line("hi")])
    entries = list(iter_entries(text))
    assert [e["type"] for e in entries] == ["queue-operation", "attachment", "user"]


def test_usage_dedups_streaming_duplicates():
    text = "\n".join(
        [
            user_line("q"),
            assistant_line("msg_1", output_tokens=5, when=ts(1)),
            assistant_line("msg_1", output_tokens=50, when=ts(0.9)),
            assistant_line("msg_2", output_tokens=7, when=ts(0.5)),
        ]
    )
    events = usage_events(iter_entries(text))
    assert [e.output for e in events] == [50, 7]
    assert events[0].burn == 10 + 50 + 200
    assert events[0].model == "claude-fable-5-1"


def test_session_facts_basic():
    text = "\n".join(noise_lines() + [user_line("Build me a dashboard\nmore text"), assistant_line("msg_1", stop_reason="tool_use")])
    facts = session_facts(iter_entries(text))
    assert facts.title == "Build me a dashboard"
    assert facts.cwd == "/home/me/proj"
    assert facts.branch == "main"
    assert facts.model == "claude-fable-5-1"
    assert facts.last_kind == "assistant"
    assert facts.last_stop_reason == "tool_use"
    assert facts.context_tokens == 10 + 1000 + 200
    assert facts.assistant_messages == 1
    assert facts.first_ts is not None and facts.last_ts is not None


def test_summary_wins_over_first_prompt():
    text = "\n".join([summary_line("Dashboard roadmap"), user_line("something else")])
    assert session_facts(iter_entries(text)).title == "Dashboard roadmap"


def test_last_kind_tracks_tool_results():
    text = "\n".join([user_line("q"), assistant_line("m1", stop_reason="tool_use"), user_line(tool_result=True, uuid="tr")])
    assert session_facts(iter_entries(text)).last_kind == "tool_result"
    text = "\n".join([user_line("q"), assistant_line("m1"), user_line("follow up", uuid="u2")])
    assert session_facts(iter_entries(text)).last_kind == "user_prompt"


def test_clean_prompt_strips_tags_and_truncates():
    raw = "<system-reminder>ignore me</system-reminder>\n  " + "x" * 100
    out = clean_prompt(raw)
    assert out.startswith("xxxx")
    assert len(out) == 60
    assert out.endswith("…")
    assert clean_prompt("<command-name>/foo</command-name>Run the thing") == "Run the thing"


def test_short_model():
    assert short_model("claude-fable-5-1") == "fable-5-1"
    assert short_model("claude-haiku-4-5-20251001") == "haiku-4-5"
    assert short_model("") == ""


def test_read_transcript_large_file_keeps_head_and_tail(tmp_path):
    from edgeboard.collectors.claude_transcripts import read_head, read_tail, read_transcript

    path = tmp_path / "s.jsonl"
    lines = [user_line("First prompt title")] + [assistant_line(f"m{i}", stop_reason="end_turn") for i in range(2000)] + [assistant_line("last", stop_reason="tool_use")]
    path.write_text("\n".join(lines))
    text = read_transcript(path, full_limit=1000, head_bytes=2000, tail_bytes=2000)
    facts = session_facts(iter_entries(text))
    assert facts.title == "First prompt title"
    assert facts.last_stop_reason == "tool_use"
    assert read_head(path, 500).endswith("\n")
    assert read_tail(path, 500).strip().endswith("}")
    small = read_transcript(path)
    assert session_facts(iter_entries(small)).assistant_messages == 2001


def test_mis_shaped_entries_are_skipped():
    import json

    bad_message = json.dumps({"type": "user", "message": ["oops"], "timestamp": ts(), "cwd": 5})
    bad_usage = json.dumps({"type": "assistant", "message": {"usage": "nope", "id": "z"}, "timestamp": ts()})
    no_message = json.dumps({"type": "assistant", "message": "garbage", "timestamp": ts()})
    text = "\n".join([bad_message, bad_usage, no_message, user_line("Real prompt"), assistant_line("m1")])
    facts = session_facts(iter_entries(text))
    assert facts.title == "Real prompt"
    assert facts.assistant_messages == 2  # "z" is a real assistant message with broken usage; "garbage" is not
    assert [e.output for e in usage_events(iter_entries(text))] == [100]


def test_clean_prompt_keeps_code_angle_brackets():
    assert clean_prompt("fix the bug where x < 5 and y > 3 causes a crash") == "fix the bug where x < 5 and y > 3 causes a crash"
    assert clean_prompt("Use generics like List<String> in java") == "Use generics like List<String> in java"
    assert clean_prompt("<system-reminder>\nnoise\n</system-reminder>\nReal") == "Real"


def _tool_facts(name, inp, stop_reason="tool_use"):
    text = "\n".join([user_line("q"), assistant_line("m1", stop_reason=stop_reason, tool=(name, inp))])
    return session_facts(iter_entries(text))


def test_last_tool_bash_hint_is_the_command_truncated():
    facts = _tool_facts("Bash", {"command": "ls -la   /very/long/path " + "x" * 60, "description": "List"})
    assert facts.last_tool == "Bash"
    assert facts.last_tool_hint.startswith("ls -la /very/long/path")
    assert len(facts.last_tool_hint) <= 40


def test_last_tool_file_tools_hint_is_the_basename():
    assert _tool_facts("Read", {"file_path": "/home/me/proj/README.md"}).last_tool_hint == "README.md"
    assert _tool_facts("Edit", {"file_path": "/home/me/proj/edgeboard/server.py", "old_string": "a"}).last_tool_hint == "server.py"
    assert _tool_facts("Write", {"file_path": "notes.txt", "content": "x"}).last_tool_hint == "notes.txt"


def test_last_tool_search_and_agent_hints():
    assert _tool_facts("Grep", {"pattern": "def main", "path": "."}).last_tool_hint == '"def main"'
    assert _tool_facts("Glob", {"pattern": "**/*.py"}).last_tool_hint == '"**/*.py"'
    assert _tool_facts("Agent", {"description": "Review the diff", "prompt": "..."}).last_tool_hint == "Review the diff"


def test_last_tool_without_recognisable_input_has_no_hint():
    facts = _tool_facts("WebFetch", {"url": "https://x"})
    assert facts.last_tool == "WebFetch" and facts.last_tool_hint == ""
    facts = _tool_facts("Bash", {})
    assert facts.last_tool == "Bash" and facts.last_tool_hint == ""


def test_last_tool_is_cleared_by_a_text_only_reply():
    text = "\n".join([user_line("q"), assistant_line("m1", stop_reason="tool_use", tool=("Bash", {"command": "ls"})), assistant_line("m2")])
    facts = session_facts(iter_entries(text))
    assert facts.last_tool == "" and facts.last_tool_hint == ""


def test_last_prompt_keeps_the_most_recent_user_prompt():
    text = "\n".join(
        [
            user_line("First prompt"),
            assistant_line("m1", stop_reason="tool_use"),
            user_line(tool_result=True, uuid="tr"),
            assistant_line("m2"),
            user_line("<system-reminder>noise</system-reminder>\nSecond prompt\nwith a second line", uuid="u2"),
        ]
    )
    facts = session_facts(iter_entries(text))
    assert facts.title == "First prompt"
    assert facts.last_prompt == "Second prompt with a second line"


def test_last_prompt_is_truncated_to_300_characters():
    facts = session_facts(iter_entries(user_line("y" * 400)))
    assert len(facts.last_prompt) == 300 and facts.last_prompt.endswith("…")


def test_last_reply_is_the_most_recent_assistant_text():
    text = "\n".join(
        [
            user_line("q1"),
            assistant_line("msg_1", text="First <system-reminder>x</system-reminder>  answer\nline two"),
            user_line("q2"),
            assistant_line("msg_2", text="Done, all green."),
            # streaming writes one content block per line: a tool_use-only line must not clear the reply
            assistant_line("msg_2", stop_reason="tool_use", text=None, tool=("Bash", {"command": "ls"})),
        ]
    )
    facts = session_facts(iter_entries(text))
    assert facts.last_reply == "Done, all green."
    facts = session_facts(iter_entries("\n".join([user_line("q"), assistant_line("m", text="First <system-reminder>x</system-reminder>  answer\nline two")])))
    assert facts.last_reply == "First answer line two"


def test_last_reply_is_truncated_to_300_characters():
    facts = session_facts(iter_entries("\n".join([user_line("q"), assistant_line("m", text="y" * 400)])))
    assert len(facts.last_reply) == 300 and facts.last_reply.endswith("…")


def test_permission_mode_comes_from_the_latest_user_prompt():
    text = "\n".join(
        [
            user_line("plan it", permissionMode="plan"),
            assistant_line("m1", stop_reason="tool_use", tool=("Bash", {"command": "ls"})),
            user_line("", tool_result=True),  # tool results carry no permissionMode
            assistant_line("m2"),
        ]
    )
    assert session_facts(iter_entries(text)).permission_mode == "plan"
    assert session_facts(iter_entries(user_line("hi"))).permission_mode == ""
