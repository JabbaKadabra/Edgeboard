"""Runtime settings, read from environment variables with sane defaults.

Values may also come from a ``.env`` file (``KEY=value`` lines, ``#`` comments,
optional ``export`` prefix and surrounding quotes). Real environment variables
always take precedence over the file. The file is ``EDGEBOARD_ENV_FILE`` if set,
else ``.env`` in the current working directory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

DEFAULT_ENV_FILE = Path(".env")

# Follow-ups the page offers on an idle session card. They travel to the
# session as plain text (slash commands would not run), hence the phrasing.
DEFAULT_PRESETS: tuple[tuple[str, str], ...] = (
    ("continue", "Continue with the next step."),
    ("commit", "Use the /commit skill to commit the current work with a clear message."),
    ("tests", "Run the test suite and fix whatever fails."),
    ("summary", "Summarize what you did, what is left and what you need from me, in a few lines."),
    ("yes", "Yes, go ahead."),
)


def parse_presets(raw: str) -> tuple[tuple[str, str], ...]:
    """``label=text|label=text`` -> ((label, text), …); ``DEFAULT_PRESETS`` when nothing usable is in there."""
    presets = []
    for item in raw.split("|"):
        label, sep, text = item.partition("=")
        label, text = label.strip(), text.strip()
        if sep and label and text:
            presets.append((label, text))
    return tuple(presets) or DEFAULT_PRESETS


def parse_env_file(path: Path) -> dict[str, str]:
    """Return KEY=value pairs from a dotenv-style file; {} if it does not exist."""
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


@dataclass(frozen=True)
class Settings:
    claude_dir: Path = Path.home() / ".claude"
    host: str = "127.0.0.1"
    port: int = 8765
    spotify_player: str = "spotify"
    system_interval: float = 1.0
    spotify_interval: float = 1.0
    spotify_queue_interval: float = 10.0
    spotify_token_file: Path = Path.home() / ".config" / "edgeboard" / "spotify-token.json"
    sessions_interval: float = 2.0
    usage_interval: float = 60.0
    timeline_interval: float = 30.0
    done_sessions_limit: int = 12
    sessions_shown: int = 4
    usage_url: str = "https://api.anthropic.com/api/oauth/usage"
    demo: bool = False
    # Attention alerts (see README): a chime on the panel and a desktop notification
    # when a session switches to waiting for you or needing permission.
    alert_sound: bool = False
    alert_notify: bool = False
    # Follow-up buttons on idle session cards (see ``parse_presets``) and how
    # long the hook script waits for an AskUserQuestion answer from the panel
    # before the terminal dialog takes over (scripts/edgeboard-hook.py).
    presets: tuple[tuple[str, str], ...] = DEFAULT_PRESETS
    answer_wait: float = 90.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, env_file: Path | None = None) -> "Settings":
        env = os.environ if env is None else env
        if env_file is None:
            env_file = Path(env.get("EDGEBOARD_ENV_FILE") or DEFAULT_ENV_FILE).expanduser()
        env = {**parse_env_file(env_file), **env}

        def get(name: str, default):
            raw = env.get(f"EDGEBOARD_{name}")
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
            spotify_queue_interval=get("SPOTIFY_QUEUE_INTERVAL", defaults.spotify_queue_interval),
            spotify_token_file=get("SPOTIFY_TOKEN_FILE", defaults.spotify_token_file),
            sessions_interval=get("SESSIONS_INTERVAL", defaults.sessions_interval),
            usage_interval=get("USAGE_INTERVAL", defaults.usage_interval),
            timeline_interval=get("TIMELINE_INTERVAL", defaults.timeline_interval),
            done_sessions_limit=get("DONE_SESSIONS_LIMIT", defaults.done_sessions_limit),
            sessions_shown=get("SESSIONS_SHOWN", defaults.sessions_shown),
            usage_url=get("USAGE_URL", defaults.usage_url),
            demo=get("DEMO", defaults.demo),
            alert_sound=get("ALERT_SOUND", defaults.alert_sound),
            alert_notify=get("ALERT_NOTIFY", defaults.alert_notify),
            presets=parse_presets(get("PRESETS", "")),
            answer_wait=get("ANSWER_WAIT", defaults.answer_wait),
        )
