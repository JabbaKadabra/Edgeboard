"""Shared in-memory snapshot filled by collectors and read by the web layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from edgeboard import __version__


@dataclass
class State:
    usage: dict = field(
        default_factory=lambda: {
            "source": "none",
            "stale": False,
            "windows": [],
            "today": {"output": 0, "input": 0, "cache_read": 0, "cache_write": 0, "messages": 0},
            "timeline": [],
            "peak": 0,
            "updated_at": None,
        }
    )
    sessions: list[dict] = field(default_factory=list)
    sessions_summary: dict = field(default_factory=lambda: {"today": 0, "done": 0, "working": 0, "idle": 0})
    spotify: dict = field(default_factory=lambda: {"running": False, "available": True})
    # Upcoming tracks from the Spotify Web API; ``configured`` is False until
    # scripts/spotify_auth.py has written a token file.
    spotify_queue: dict = field(default_factory=lambda: {"configured": False, "tracks": []})
    system: dict | None = None
    errors: dict[str, str | None] = field(default_factory=lambda: {"usage": None, "sessions": None, "spotify": None, "system": None})

    def snapshot(self) -> dict:
        return {
            "now": datetime.now(timezone.utc).isoformat(),
            "version": __version__,
            "usage": self.usage,
            "sessions": self.sessions,
            "sessions_summary": self.sessions_summary,
            "spotify": self.spotify,
            "spotify_queue": self.spotify_queue,
            "system": self.system,
            "errors": self.errors,
        }
