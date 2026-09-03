"""Canned snapshot so the page can be previewed without Claude, Spotify or sensors."""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

from edgeboard.state import State


def fill_demo(state: State) -> None:
    now = datetime.now(timezone.utc)
    rnd = random.Random(7)
    timeline = []
    for i in range(24):
        start = (now - timedelta(hours=23 - i)).replace(minute=0, second=0, microsecond=0)
        tokens = int(abs(math.sin(i / 3.0)) * 180_000 * rnd.uniform(0.3, 1.0)) if 6 <= start.astimezone().hour <= 23 else rnd.randint(0, 4000)
        timeline.append({"hour_start": start.isoformat(), "tokens": tokens})
    state.usage = {
        "source": "demo",
        "stale": False,
        "windows": [
            # 5-hour: filling faster than it resets (warning line); weekly: comfortably safe
            {"key": "five_hour", "label": "5-hour", "utilization": 6, "resets_at": (now + timedelta(hours=4, minutes=10)).isoformat(), "seconds_to_reset": 4 * 3600 + 600, "tokens": None, "rate_per_hour": 30.0, "projected_full_at": (now + timedelta(hours=94 / 30)).isoformat()},
            {"key": "seven_day", "label": "Weekly", "utilization": 2, "resets_at": (now + timedelta(days=5, hours=22)).isoformat(), "seconds_to_reset": 5 * 86400 + 22 * 3600, "tokens": None, "rate_per_hour": 0.4, "projected_full_at": (now + timedelta(hours=98 / 0.4)).isoformat()},
        ],
        "today": {"output": 370_000, "input": 558, "cache_read": 113_300_000, "cache_write": 2_900_000, "messages": 279},
        "timeline": timeline,
        "peak": max(b["tokens"] for b in timeline),
        "updated_at": now.isoformat(),
    }

    def session(i, name, status, detail, model, ctx, minutes, project="it-system-of-record", branch="master", agents=0, active_agents=0):
        return {
            "id": f"demo-{i}",
            "name": name,
            "project": project,
            "cwd": f"/home/me/{project}",
            "branch": branch,
            "model": model,
            "status": status,
            "detail": detail,
            "context_tokens": ctx,
            "started_at": (now - timedelta(minutes=minutes + 40)).isoformat(),
            "last_activity": (now - timedelta(minutes=minutes)).isoformat(),
            "messages": 20 + i * 7,
            "agents": agents,
            "active_agents": active_agents,
            "last_prompt": f"Review the {name.lower()} once more and list what still blocks Monday's rollout, then start on the fixes in priority order.",
        }

    state.sessions = [
        session(1, "HR Dashboard Monday review", "attention", "needs permission", "opus-5", 197_000, 0),
        session(2, "UKG process repo organization", "working", "running pytest tests/ -q", "opus-5", 251_000, 2),
        session(3, "Hazelwood Frost findings memo", "working", "agents running", "fable-5-1", 420_000, 1, agents=3, active_agents=2),
        session(4, "ITOPS features gap analysis", "idle", "waiting for you", "opus-5", 304_000, 31, agents=1),
    ][:4]
    state.sessions_summary = {"today": 21, "done": 5, "working": 2, "idle": 2, "attention": 1}
    state.spotify = {
        "running": True,
        "available": True,
        "status": "Playing",
        "title": "Midnight City",
        "artist": "M83",
        "album": "Hurry Up, We're Dreaming",
        "art_url": "",
        "length_s": 243.0,
        "position_s": 97.0,
        "shuffle": False,
        "volume": 0.65,
    }
    state.spotify_queue = {
        "configured": True,
        "tracks": [
            {"title": "Reunion", "artist": "M83", "album": "Hurry Up, We're Dreaming", "art_url": "", "length_s": 236.0},
            {"title": "Wait", "artist": "M83", "album": "Hurry Up, We're Dreaming", "art_url": "", "length_s": 342.0},
            {"title": "Kim & Jessie", "artist": "M83", "album": "Saturdays = Youth", "art_url": "", "length_s": 312.0},
            {"title": "Outro", "artist": "M83", "album": "Hurry Up, We're Dreaming", "art_url": "", "length_s": 251.0},
            {"title": "Intro", "artist": "The xx", "album": "xx", "art_url": "", "length_s": 128.0},
            {"title": "Genesis", "artist": "Grimes", "album": "Visions", "art_url": "", "length_s": 255.0},
        ],
    }
    cpu_hist = [30 + 25 * abs(math.sin(i / 7)) + rnd.uniform(-5, 5) for i in range(120)]
    gpu_hist = [10 + 60 * abs(math.sin(i / 11)) for i in range(120)]
    state.system = {
        "cpu": {"percent": 6.0, "per_core": [rnd.uniform(0, 20) for _ in range(16)], "temp": 64.0, "freq_mhz": 4350},
        "mem": {"percent": 50.0, "used": 32 * 1024**3, "total": 64 * 1024**3},
        "gpu": {"name": "Radeon RX 7900 XT", "percent": 6.0, "temp": 55.0, "mem_used": 3 * 1024**3, "mem_total": 20 * 1024**3, "vendor": "amd"},
        "disks": [{"mount": "/", "percent": 61.0, "used": 600 * 1024**3, "total": 1000 * 1024**3}],
        "net": {"rx_bps": 1.8e6, "tx_bps": 2.4e5},
        "load": [1.2, 0.9, 0.8],
        "uptime_s": 5 * 86400 + 3600,
        "history": {"cpu": cpu_hist, "gpu": gpu_hist, "rx": [rnd.uniform(0, 3e6) for _ in range(120)], "tx": [rnd.uniform(0, 5e5) for _ in range(120)]},
    }
