"""Claude usage limits, today's totals and the 24 h timeline.

Primary source for limits is the OAuth usage endpoint that Claude Code's
``/usage`` command reads. When no token is available the module falls back
to token counts computed from local transcripts (no percentages then).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Iterable

import httpx

from edgeboard.collectors.claude_transcripts import UsageEvent, UsageParser, iter_entries, read_new_lines

LABELS = {
    "five_hour": "5-hour",
    "seven_day": "Weekly",
    "seven_day_opus": "Opus weekly",
    "seven_day_sonnet": "Sonnet weekly",
    "seven_day_oauth_apps": "Apps weekly",
}
WINDOW_LENGTH = {"five_hour": timedelta(hours=5), "seven_day": timedelta(days=7)}
# Only the plan-wide windows are shown; per-model and extra-usage windows are dropped.
SHOWN_WINDOWS = ("five_hour", "seven_day")
BETA_HEADER = "oauth-2025-04-20"


@dataclass
class UsageWindow:
    key: str
    label: str
    utilization: float | None
    resets_at: str | None
    seconds_to_reset: int | None
    tokens: int | None = None
    # Pace over the recent samples (see ``project_window``): None while flat or unknown.
    rate_per_hour: float | None = None
    projected_full_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Sample:
    """One ``(when, utilization)`` reading of a usage window."""

    ts: datetime
    utilization: float | None


@dataclass
class Projection:
    rate_per_hour: float | None = None
    projected_full_at: str | None = None


# Samples kept per window for the projection: at the 60 s poll interval that is
# half an hour of history, enough to smooth a burst without lagging a real change.
PROJECTION_SAMPLES = 30
# Below this pace the line is noise (a 5-hour window would take days to fill).
MIN_RATE_PER_HOUR = 0.1


@dataclass
class TodayTotals:
    output: int = 0
    input: int = 0
    cache_read: int = 0
    cache_write: int = 0
    messages: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TimelineBucket:
    hour_start: str
    tokens: int

    def to_dict(self) -> dict:
        return asdict(self)


def label_for(key: str) -> str:
    if key in LABELS:
        return LABELS[key]
    words = key.replace("seven_day_", "").replace("_", " ").strip()
    if key.startswith("seven_day_"):
        return f"{words.capitalize()} weekly"
    return words.capitalize()


def _parse_dt(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _window(key: str, utilization: float | None, resets: datetime | None, now: datetime, tokens: int | None = None) -> UsageWindow:
    secs = None
    if resets is not None:
        secs = max(0, int((resets - now).total_seconds()))
    return UsageWindow(
        key=key,
        label=label_for(key),
        utilization=utilization,
        resets_at=resets.isoformat() if resets else None,
        seconds_to_reset=secs,
        tokens=tokens,
    )


def parse_usage_response(data: dict, now: datetime) -> list[UsageWindow]:
    """Turn the usage endpoint's JSON into ordered windows.

    Any top-level key whose value is a dict carrying ``utilization`` is a
    window, but only ``SHOWN_WINDOWS`` (5-hour and weekly) are kept: the
    per-model and extra-usage windows are noise on the panel.
    """
    windows: list[UsageWindow] = []
    for key, value in data.items():
        if key not in SHOWN_WINDOWS or not isinstance(value, dict) or "utilization" not in value:
            continue
        util = value.get("utilization")
        try:
            util = float(util) if util is not None else None
        except (TypeError, ValueError):
            util = None
        windows.append(_window(key, util, _parse_dt(value.get("resets_at")), now))
    priority = {"five_hour": 0, "seven_day": 1}
    windows.sort(key=lambda w: (priority.get(w.key, 2), w.key))
    return windows


def local_windows(events: Iterable[UsageEvent], now: datetime) -> list[UsageWindow]:
    events = list(events)
    windows = []
    for key, length in WINDOW_LENGTH.items():
        inside = [e for e in events if e.ts > now - length]
        tokens = sum(e.burn for e in inside)
        resets = min(e.ts for e in inside) + length if inside else None
        windows.append(_window(key, None, resets, now, tokens))
    return windows


def today_totals(events: Iterable[UsageEvent], now: datetime, tz: tzinfo | None = None) -> TodayTotals:
    local_now = now.astimezone(tz)
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    totals = TodayTotals()
    for e in events:
        if e.ts.astimezone(tz) < midnight:
            continue
        totals.output += e.output
        totals.input += e.input
        totals.cache_read += e.cache_read
        totals.cache_write += e.cache_write
        totals.messages += 1
    return totals


def timeline(events: Iterable[UsageEvent], now: datetime, hours: int = 24) -> list[TimelineBucket]:
    end = now.replace(minute=0, second=0, microsecond=0)
    starts = [end - timedelta(hours=hours - 1 - i) for i in range(hours)]
    sums = [0] * hours
    first = starts[0]
    for e in events:
        idx = int((e.ts - first).total_seconds() // 3600)
        if 0 <= idx < hours:
            sums[idx] += e.burn
    return [TimelineBucket(hour_start=s.isoformat(), tokens=t) for s, t in zip(starts, sums, strict=True)]


def _since_last_reset(samples: list[Sample]) -> list[Sample]:
    """The tail of ``samples`` after the last drop in utilization (a window reset)."""
    start = 0
    for i in range(1, len(samples)):
        if samples[i].utilization < samples[i - 1].utilization:
            start = i
    return samples[start:]


def project_window(samples: Iterable[Sample], now: datetime) -> Projection:
    """When the window hits 100 % at the current pace.

    Fits a least-squares slope through the samples taken since the last reset
    (three or more), or takes the delta between the oldest and newest when
    there are only two. A slope below ``MIN_RATE_PER_HOUR`` counts as flat and
    yields an empty projection, as does anything with fewer than two samples.
    """
    usable = _since_last_reset(sorted((s for s in samples if s.utilization is not None), key=lambda s: s.ts))
    if len(usable) < 2:
        return Projection()
    t0 = usable[0].ts
    xs = [(s.ts - t0).total_seconds() / 3600 for s in usable]
    ys = [s.utilization for s in usable]
    span = xs[-1] - xs[0]
    if span <= 0:
        return Projection()
    if len(usable) < 3:
        slope = (ys[-1] - ys[0]) / span
    else:
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        var = sum((x - mx) ** 2 for x in xs)
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / var
    rate = round(slope, 3)
    if rate < MIN_RATE_PER_HOUR:
        return Projection()
    hours_left = max(0.0, (100.0 - ys[-1]) / rate)
    return Projection(rate_per_hour=rate, projected_full_at=(now + timedelta(hours=hours_left)).isoformat())


def record_sample(store: dict[str, list[Sample]], key: str, sample: Sample, limit: int = PROJECTION_SAMPLES) -> None:
    """Append ``sample`` to the per-window history, keeping the newest ``limit`` readings."""
    if sample.utilization is None:
        return
    history = store.setdefault(key, [])
    history.append(sample)
    del history[:-limit]


def project_windows(windows: Iterable[UsageWindow], store: dict[str, list[Sample]], now: datetime) -> None:
    """Record ``now``'s reading of each window and fill in its projection in place."""
    for w in windows:
        record_sample(store, w.key, Sample(now, w.utilization))
        proj = project_window(store.get(w.key, []), now)
        w.rate_per_hour, w.projected_full_at = proj.rate_per_hour, proj.projected_full_at


def load_token(claude_dir: Path) -> str | None:
    path = claude_dir / ".credentials.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if isinstance(oauth, dict) and oauth.get("accessToken"):
        return str(oauth["accessToken"])
    return None


async def fetch_usage(client: httpx.AsyncClient, token: str, url: str) -> dict:
    response = await client.get(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": BETA_HEADER,
            "Accept": "application/json",
            "User-Agent": "edgeboard/0.1",
        },
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("usage endpoint returned a non-object")
    return data


TRANSCRIPT_GLOBS = ("*/*.jsonl", "*/*/subagents/*.jsonl")


# Parser state per transcript, keyed by (mtime_ns, size). Transcripts are
# append-only: a file that only grew since the last poll has just its new
# bytes parsed (a live session's transcript changes every few seconds and
# can be tens of MB), an unchanged one yields the cached events, and one
# that shrank or was replaced is parsed again. ``offset`` is how far the
# parser has consumed, always at a line boundary.
@dataclass
class _Cached:
    key: tuple[int, int]
    parser: UsageParser
    offset: int


_events_cache: dict[Path, _Cached] = {}


def _file_events(path: Path) -> list[UsageEvent] | None:
    try:
        st = path.stat()
        key = (st.st_mtime_ns, st.st_size)
        cached = _events_cache.get(path)
        if cached is not None and cached.key == key:
            return cached.parser.events
        if cached is not None and st.st_size >= cached.offset:
            text, cached.offset = read_new_lines(path, cached.offset, st.st_size)
            cached.key = key
            if text:
                cached.parser.feed(iter_entries(text))
            return cached.parser.events
        data = path.read_bytes()
    except OSError:
        return None
    parser = UsageParser()
    events = parser.feed(iter_entries(data.decode("utf-8", errors="replace")))
    # An unterminated last line was fed for freshness but is re-read once complete.
    unterminated = len(data) - (data.rfind(b"\n") + 1)
    _events_cache[path] = _Cached(key, parser, st.st_size - unterminated)
    return events


def load_all_events(claude_dir: Path, since: datetime) -> list[UsageEvent]:
    """Usage events from every transcript modified after ``since``.

    Subagent transcripts (``<project>/<session>/subagents/*.jsonl``) are
    included: their tokens count against the same limits.
    """
    projects = claude_dir / "projects"
    events: list[UsageEvent] = []
    if not projects.is_dir():
        return events
    paths = [p for pattern in TRANSCRIPT_GLOBS for p in projects.glob(pattern)]
    keep: set[Path] = set()
    for path in paths:
        try:
            if path.stat().st_mtime < since.timestamp():
                continue
        except OSError:
            continue
        keep.add(path)
        file_events = _file_events(path)
        if file_events:
            events.extend(e for e in file_events if e.ts >= since)
    for stale in [p for p in _events_cache if p not in keep]:
        _events_cache.pop(stale, None)
    events.sort(key=lambda e: e.ts)
    return events
