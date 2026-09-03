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

from xdash.collectors.claude_transcripts import UsageEvent, iter_entries, usage_events

LABELS = {
    "five_hour": "5-hour",
    "seven_day": "Weekly",
    "seven_day_opus": "Opus weekly",
    "seven_day_sonnet": "Sonnet weekly",
    "seven_day_oauth_apps": "Apps weekly",
}
WINDOW_LENGTH = {"five_hour": timedelta(hours=5), "seven_day": timedelta(days=7)}
BETA_HEADER = "oauth-2025-04-20"


@dataclass
class UsageWindow:
    key: str
    label: str
    utilization: float | None
    resets_at: str | None
    seconds_to_reset: int | None
    tokens: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


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

    Any top-level key whose value is a dict carrying ``utilization`` becomes a
    window, so new per-model windows appear without code changes.
    """
    windows: list[UsageWindow] = []
    for key, value in data.items():
        if not isinstance(value, dict) or "utilization" not in value:
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
    return [TimelineBucket(hour_start=s.isoformat(), tokens=t) for s, t in zip(starts, sums)]


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
            "User-Agent": "xdash/0.1",
        },
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("usage endpoint returned a non-object")
    return data


TRANSCRIPT_GLOBS = ("*/*.jsonl", "*/*/subagents/*.jsonl")


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
    for path in paths:
        try:
            if path.stat().st_mtime < since.timestamp():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        events.extend(e for e in usage_events(iter_entries(text)) if e.ts >= since)
    events.sort(key=lambda e: e.ts)
    return events
