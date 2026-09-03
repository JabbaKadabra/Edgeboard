from xdash.collectors.claude_transcripts import (
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
    from xdash.collectors.claude_transcripts import read_head, read_tail, read_transcript

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
