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

    def session(i, name, status, detail, model, ctx, minutes, project="it-system-of-record", branch="master", agents=0, active_agents=0, question=None, can_send=False, reply="", window=200_000, compactions=0, tasks=None, commits=0, prompt="", mode="default"):
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
            "last_prompt": prompt or f"Review the {name.lower()} once more and list what still blocks Monday's rollout, then start on the fixes in priority order.",
            "last_reply": reply,
            "permission_mode": mode,
            "session_name": f"{project[:6]}-{i}",
            "can_send": can_send,
            "waiting_since": (now - timedelta(minutes=minutes)).isoformat() if status in ("idle", "attention") else None,
            "question": question,
            "context_window": window,
            "context_pct": round(100 * ctx / window),
            "compactions": compactions,
            "last_compact_at": (now - timedelta(minutes=minutes + 12)).isoformat() if compactions else None,
            "last_compact_trigger": "auto" if compactions else "",
            "tasks": tasks,
            "commits": commits,
        }

    question = {
        "tool_use_id": "demo-toolu-1",
        "answerable": True,  # as if the hook script were waiting on /api/answer
        "title": "Rollout",
        "questions": [
            {"question": "Which environment should the Monday rollout target first?", "header": "Target", "options": ["staging", "prod", "both"], "multi": False},
            {"question": "Who gets the change notice?", "header": "Notify", "options": ["ops", "hr-leads", "everyone"], "multi": True},
        ],
    }

    # replies and prompts at the length the collector keeps (300 chars), so the cards show the clamp at work
    state.sessions = [
        session(1, "HR Dashboard Monday review", "attention", "asking you a question", "opus-5", 197_000, 0, question=question, can_send=True, mode="plan",
                prompt="Go through the HR dashboard once more and list what still blocks Monday's rollout, then plan the fixes in priority order.",
                reply="Two options remain for the rollout order; I need your call before I touch the deploy scripts.",
                tasks={"total": 7, "done": 3, "current": "Reviewing the HR dashboard access rules"}),
        session(2, "UKG process repo organization", "working", "running pytest tests/ -q", "opus-5[1m]", 251_000, 2, can_send=True, window=1_000_000, commits=2, mode="acceptEdits",
                prompt="Reorganise the UKG process repo: exports, loaders and the cron entries each in their own package, tests green after every move.",
                reply="Moved the UKG exports into ukg/exports/ and the loaders into ukg/load/, with the old import paths kept as thin shims so the cron entries keep working until I touch them. The suite passed after each move; running it once more end to end before I rewrite the cron entries and drop the shims.",
                tasks={"total": 5, "done": 5, "current": ""}),
        session(3, "Hazelwood Frost findings memo", "working", "agents running", "fable-5-1[1m]", 420_000, 1, agents=3, active_agents=2, can_send=True, window=1_000_000, compactions=1,
                prompt="Turn the Hazelwood Frost findings into a memo for the steering group: risks first, then the evidence, one page.",
                reply="Three reviewers are reading the findings in parallel: one checks the figures against the audit export, one reads the interview notes for anything the figures miss, one drafts the risk section. I will merge their notes into the memo's risk section and keep the evidence to one page as asked."),
        session(4, "ITOPS features gap analysis", "idle", "waiting for you", "opus-5[1m]", 304_000, 31, agents=1, can_send=True, window=1_000_000, commits=1,
                prompt="Compare the ITOPS feature list with what the vendor shipped and write up the gaps, blocking ones first.",
                reply="Gap analysis is drafted in docs/itops-gaps.md: 14 features, 5 blocking. The blocking ones are all on the ticketing side (SLA clocks, escalation rules, the on-call rota sync); the rest are reporting and can wait for the next release. Want me to open tickets for the blocking ones?",
                tasks={"total": 4, "done": 2, "current": "Open tickets for the blocking gaps"}),
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
    def commit(hash, repo, message, minutes_ago, added, deleted):
        return {"hash": hash, "repo": repo, "path": f"/home/me/{repo}", "message": message, "ts": (now - timedelta(minutes=minutes_ago)).isoformat(), "author": "me", "added": added, "deleted": deleted}

    commits = [
        commit("a3f91c2", "it-system-of-record", "fix: session card flash restarts", 12, 40, 8),
        commit("7be04d8", "it-system-of-record", "feat: pace projection on limits", 38, 122, 15),
        commit("d114f7a", "blog", "post: september notes draft", 65, 88, 0),
        commit("09cc31e", "api", "refactor: split auth middleware", 130, 96, 44),
        commit("f82ab55", "dotfiles", "chore: prune zsh aliases", 190, 6, 21),
        commit("31d9e04", "dotfiles", "feat: hyprland window rules", 200, 60, 0),
    ]
    state.git = {"commits": commits, "count": 9, "added": 412, "deleted": 88}
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
        "history": {"cpu": cpu_hist, "gpu": gpu_hist},
    }
