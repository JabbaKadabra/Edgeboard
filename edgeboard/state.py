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
    sessions_summary: dict = field(default_factory=lambda: {"today": 0, "done": 0, "working": 0, "idle": 0, "attention": 0})
    spotify: dict = field(default_factory=lambda: {"running": False, "available": True})
    # Upcoming tracks from the Spotify Web API; ``configured`` is False until
    # scripts/spotify_auth.py has written a token file.
    spotify_queue: dict = field(default_factory=lambda: {"configured": False, "tracks": []})
    system: dict | None = None
    # Today's commits in the sessions' repositories (see ``collectors.git.summarize``).
    git: dict = field(default_factory=lambda: {"commits": [], "count": 0, "added": 0, "deleted": 0})
    # Latest Claude Code hook event per session id (POST /api/hook), each with a
    # ``ts`` receipt time. Merged into the session cards; not part of the snapshot.
    hooks: dict[str, dict] = field(default_factory=dict)
    errors: dict[str, str | None] = field(default_factory=lambda: {"usage": None, "sessions": None, "spotify": None, "system": None, "git": None})
    # The few settings the page needs to know (set by create_app).
    settings: dict = field(default_factory=lambda: {"alert_sound": False, "presets": []})
    # ``<version>+<hash of the page files>`` (see ``server.build_id``); the page reloads when it changes.
    build: str = __version__

    def snapshot(self) -> dict:
        return {
            "now": datetime.now(timezone.utc).isoformat(),
            "version": self.build,
            "usage": self.usage,
            "sessions": self.sessions,
            "sessions_summary": self.sessions_summary,
            "spotify": self.spotify,
            "spotify_queue": self.spotify_queue,
            "system": self.system,
            "git": self.git,
            "errors": self.errors,
            "settings": self.settings,
        }
