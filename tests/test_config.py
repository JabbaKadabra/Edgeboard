from pathlib import Path

from xdash.config import Settings


def test_defaults():
    s = Settings.from_env({})
    assert s.claude_dir == Path.home() / ".claude"
    assert s.host == "127.0.0.1"
    assert s.port == 8765
    assert s.spotify_player == "spotify"
    assert s.demo is False


def test_overrides():
    s = Settings.from_env({"XDASH_PORT": "9000", "XDASH_CLAUDE_DIR": "/x", "XDASH_DEMO": "1", "XDASH_USAGE_INTERVAL": "5"})
    assert s.port == 9000
    assert s.claude_dir == Path("/x")
    assert s.demo is True
    assert s.usage_interval == 5.0
