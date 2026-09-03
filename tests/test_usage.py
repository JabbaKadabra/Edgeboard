import json
from datetime import datetime, timedelta, timezone

from edgeboard.collectors.claude_transcripts import UsageEvent
from edgeboard.collectors.claude_usage import (
    label_for,
    load_all_events,
    load_token,
    local_windows,
    parse_usage_response,
    timeline,
    today_totals,
)
from tests.fixtures import assistant_line, ts, user_line

NOW = datetime(2026, 9, 3, 12, 30, tzinfo=timezone.utc)


def ev(hours_ago: float, output=100, input=10, cache_read=1000, cache_write=200) -> UsageEvent:
    return UsageEvent(NOW - timedelta(hours=hours_ago), "claude-fable-5-1", input, output, cache_read, cache_write)


def test_parse_usage_response_keeps_plan_windows_only():
    data = {
        "five_hour": {"utilization": 6, "resets_at": "2026-09-03T16:40:00Z"},
        "seven_day": {"utilization": 2.5, "resets_at": "2026-09-08T10:00:00+00:00"},
        "seven_day_fable": {"utilization": 2, "resets_at": "2026-09-08T10:00:00Z"},
        "seven_day_opus": None,
        "extra_usage": {"utilization": 0, "resets_at": None},
        "extra": "ignored",
    }
    windows = parse_usage_response(data, NOW)
    # per-model and extra-usage windows are dropped: only the plan-wide two remain
    assert [w.label for w in windows] == ["5-hour", "Weekly"]
    assert windows[0].utilization == 6.0
    assert windows[0].seconds_to_reset == 4 * 3600 + 10 * 60
    assert windows[1].utilization == 2.5
    assert windows[0].to_dict()["key"] == "five_hour"


def test_label_for():
    assert label_for("seven_day_opus") == "Opus weekly"
    assert label_for("seven_day_sonnet_4_5") == "Sonnet 4 5 weekly"
    assert label_for("monthly") == "Monthly"


def test_local_windows_reset_from_first_event():
    windows = local_windows([ev(4), ev(1), ev(30)], NOW)
    five, week = windows
    assert five.key == "five_hour" and five.utilization is None
    assert five.tokens == 2 * (100 + 10 + 200)
    assert five.resets_at == (NOW - timedelta(hours=4) + timedelta(hours=5)).isoformat()
    assert week.tokens == 3 * 310
    assert local_windows([], NOW)[0].resets_at is None


def test_today_totals_excludes_yesterday():
    totals = today_totals([ev(1), ev(2), ev(20)], NOW, timezone.utc)
    assert totals.messages == 2
    assert totals.output == 200 and totals.input == 20 and totals.cache_read == 2000 and totals.cache_write == 400


def test_timeline_buckets():
    buckets = timeline([ev(0), ev(0.2), ev(3), ev(30)], NOW)
    assert len(buckets) == 24
    assert buckets[-1].tokens == 620
    assert buckets[-4].tokens == 310
    assert sum(b.tokens for b in buckets) == 930
    assert buckets[-1].hour_start == "2026-09-03T12:00:00+00:00"


def test_load_token(tmp_path):
    assert load_token(tmp_path) is None
    (tmp_path / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok"}}))
    assert load_token(tmp_path) == "tok"


def test_load_all_events(tmp_path):
    proj = tmp_path / "projects" / "-x"
    proj.mkdir(parents=True)
    (proj / "a.jsonl").write_text("\n".join([user_line("q"), assistant_line("m1", when=ts(1, NOW)), assistant_line("m1", when=ts(1, NOW), output_tokens=9)]))
    (proj / "b.jsonl").write_text(assistant_line("m2", when=ts(50, NOW)))
    events = load_all_events(tmp_path, NOW - timedelta(hours=24))
    assert [e.output for e in events] == [9]


def test_load_all_events_includes_subagents(tmp_path):
    proj = tmp_path / "projects" / "-x"
    sub = proj / "sess" / "subagents"
    sub.mkdir(parents=True)
    (proj / "sess.jsonl").write_text(assistant_line("main", when=ts(1, NOW), output_tokens=5))
    (sub / "agent-1.jsonl").write_text(assistant_line("agent", when=ts(1, NOW), output_tokens=7))
    events = load_all_events(tmp_path, NOW - timedelta(hours=24))
    assert sorted(e.output for e in events) == [5, 7]


def test_load_all_events_caches_unchanged_files(tmp_path):
    import os

    proj = tmp_path / "projects" / "-x"
    proj.mkdir(parents=True)
    path = proj / "a.jsonl"
    path.write_text(assistant_line("m1", when=ts(1, NOW), output_tokens=100))
    since = NOW - timedelta(hours=24)
    assert [e.output for e in load_all_events(tmp_path, since)] == [100]
    st = path.stat()
    path.write_text(assistant_line("m1", when=ts(1, NOW), output_tokens=900))  # same size
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert [e.output for e in load_all_events(tmp_path, since)] == [100]  # unchanged mtime+size: cached
    with path.open("a") as fh:
        fh.write("\n" + assistant_line("m2", when=ts(0.5, NOW), output_tokens=7))
    assert [e.output for e in load_all_events(tmp_path, since)] == [900, 7]


def test_load_all_events_cache_respects_since(tmp_path):
    proj = tmp_path / "projects" / "-x"
    proj.mkdir(parents=True)
    (proj / "a.jsonl").write_text("\n".join([assistant_line("m1", when=ts(30, NOW), output_tokens=1), assistant_line("m2", when=ts(1, NOW), output_tokens=2)]))
    assert [e.output for e in load_all_events(tmp_path, NOW - timedelta(hours=48))] == [1, 2]
    assert [e.output for e in load_all_events(tmp_path, NOW - timedelta(hours=24))] == [2]


# ---------- limit projection ----------


def samples(*points):
    """(minutes_ago, utilization) pairs -> Sample list, oldest first."""
    from edgeboard.collectors.claude_usage import Sample

    return [Sample(NOW - timedelta(minutes=m), u) for m, u in sorted(points, reverse=True)]


def test_project_window_rising_hits_full_before_reset():
    from edgeboard.collectors.claude_usage import project_window

    # 10 % per 30 min = 20 %/h, latest 40 % -> 100 % in 3 h
    proj = project_window(samples((60, 20), (30, 30), (0, 40)), NOW)
    assert proj.rate_per_hour == 20.0
    assert proj.projected_full_at == (NOW + timedelta(hours=3)).isoformat()


def test_project_window_two_samples_uses_delta():
    from edgeboard.collectors.claude_usage import project_window

    proj = project_window(samples((20, 10), (0, 15)), NOW)
    assert proj.rate_per_hour == 15.0
    assert proj.projected_full_at == (NOW + timedelta(hours=85 / 15)).isoformat()


def test_project_window_flat_or_too_few_samples_is_none():
    from edgeboard.collectors.claude_usage import project_window

    assert project_window(samples((60, 20), (30, 20), (0, 20)), NOW).rate_per_hour is None
    assert project_window(samples((0, 20)), NOW).rate_per_hour is None
    assert project_window([], NOW).projected_full_at is None
    # a tiny slope from noise (below 0.1 %/h) is treated as flat
    assert project_window(samples((60, 20.0), (0, 20.05)), NOW).rate_per_hour is None


def test_project_window_ignores_samples_before_a_reset():
    from edgeboard.collectors.claude_usage import project_window

    # the window reset between -30 and -20: utilization dropped from 90 to 5
    proj = project_window(samples((60, 70), (30, 90), (20, 5), (10, 10), (0, 15)), NOW)
    assert proj.rate_per_hour == 30.0
    assert proj.projected_full_at == (NOW + timedelta(hours=85 / 30)).isoformat()


def test_project_window_at_full_is_now():
    from edgeboard.collectors.claude_usage import project_window

    proj = project_window(samples((30, 90), (0, 100)), NOW)
    assert proj.rate_per_hour == 20.0
    assert proj.projected_full_at == NOW.isoformat()


def test_record_sample_trims_and_drops_old_windows():
    from edgeboard.collectors.claude_usage import Sample, record_sample

    store: dict[str, list[Sample]] = {}
    for i in range(40):
        record_sample(store, "five_hour", Sample(NOW + timedelta(minutes=i), float(i)), limit=30)
    assert len(store["five_hour"]) == 30
    assert store["five_hour"][0].utilization == 10.0 and store["five_hour"][-1].utilization == 39.0
    # a None utilization (local fallback) records nothing
    record_sample(store, "seven_day", Sample(NOW, None), limit=30)
    assert "seven_day" not in store
