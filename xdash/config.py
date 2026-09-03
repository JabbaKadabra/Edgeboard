"""Runtime settings, read from environment variables with sane defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class Settings:
    claude_dir: Path = Path.home() / ".claude"
    host: str = "127.0.0.1"
    port: int = 8765
    spotify_player: str = "spotify"
    system_interval: float = 1.0
    spotify_interval: float = 1.0
    sessions_interval: float = 2.0
    usage_interval: float = 60.0
    timeline_interval: float = 30.0
    done_sessions_limit: int = 12
    usage_url: str = "https://api.anthropic.com/api/oauth/usage"
    demo: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env

        def get(name: str, default):
            raw = env.get(f"XDASH_{name}")
            if raw is None or raw == "":
                return default
            if isinstance(default, bool):
                return raw.lower() in ("1", "true", "yes", "on")
            if isinstance(default, int):
                return int(raw)
            if isinstance(default, float):
                return float(raw)
            if isinstance(default, Path):
                return Path(raw).expanduser()
            return raw

        defaults = cls()
        return cls(
            claude_dir=get("CLAUDE_DIR", defaults.claude_dir),
            host=get("HOST", defaults.host),
            port=get("PORT", defaults.port),
            spotify_player=get("SPOTIFY_PLAYER", defaults.spotify_player),
            system_interval=get("SYSTEM_INTERVAL", defaults.system_interval),
            spotify_interval=get("SPOTIFY_INTERVAL", defaults.spotify_interval),
            sessions_interval=get("SESSIONS_INTERVAL", defaults.sessions_interval),
            usage_interval=get("USAGE_INTERVAL", defaults.usage_interval),
            timeline_interval=get("TIMELINE_INTERVAL", defaults.timeline_interval),
            done_sessions_limit=get("DONE_SESSIONS_LIMIT", defaults.done_sessions_limit),
            usage_url=get("USAGE_URL", defaults.usage_url),
            demo=get("DEMO", defaults.demo),
        )
