from pathlib import Path

from edgeboard.config import Settings


NO_FILE = Path("/nonexistent/.env")


def test_defaults():
    s = Settings.from_env({}, env_file=NO_FILE)
    assert s.claude_dir == Path.home() / ".claude"
    assert s.host == "127.0.0.1"
    assert s.port == 8765
    assert s.spotify_player == "spotify"
    assert s.demo is False


def test_overrides():
    s = Settings.from_env({"EDGEBOARD_PORT": "9000", "EDGEBOARD_CLAUDE_DIR": "/x", "EDGEBOARD_DEMO": "1", "EDGEBOARD_USAGE_INTERVAL": "5"}, env_file=NO_FILE)
    assert s.port == 9000
    assert s.claude_dir == Path("/x")
    assert s.demo is True
    assert s.usage_interval == 5.0


def test_parse_env_file(tmp_path):
    from edgeboard.config import parse_env_file

    f = tmp_path / ".env"
    f.write_text(
        "# comment\n"
        "\n"
        "EDGEBOARD_PORT=9001\n"
        "export EDGEBOARD_HOST=0.0.0.0\n"
        'EDGEBOARD_SPOTIFY_PLAYER="vlc"\n'
        "EDGEBOARD_USAGE_URL='http://x/'  \n"
        "  EDGEBOARD_DEMO = yes\n"
        "not a pair\n"
    )
    assert parse_env_file(f) == {
        "EDGEBOARD_PORT": "9001",
        "EDGEBOARD_HOST": "0.0.0.0",
        "EDGEBOARD_SPOTIFY_PLAYER": "vlc",
        "EDGEBOARD_USAGE_URL": "http://x/",
        "EDGEBOARD_DEMO": "yes",
    }


def test_parse_env_file_missing(tmp_path):
    from edgeboard.config import parse_env_file

    assert parse_env_file(tmp_path / "nope") == {}


def test_from_env_reads_env_file_but_environment_wins(tmp_path):
    f = tmp_path / ".env"
    f.write_text("EDGEBOARD_PORT=9001\nEDGEBOARD_SPOTIFY_PLAYER=vlc\n")
    s = Settings.from_env({"EDGEBOARD_PORT": "9002"}, env_file=f)
    assert s.port == 9002
    assert s.spotify_player == "vlc"


def test_from_env_file_path_from_environment(tmp_path):
    f = tmp_path / "custom.env"
    f.write_text("EDGEBOARD_PORT=9003\n")
    s = Settings.from_env({"EDGEBOARD_ENV_FILE": str(f)})
    assert s.port == 9003


def test_alert_flags_default_off():
    s = Settings.from_env({}, env_file=NO_FILE)
    assert s.alert_sound is False and s.alert_notify is False
    s = Settings.from_env({"EDGEBOARD_ALERT_SOUND": "1", "EDGEBOARD_ALERT_NOTIFY": "yes"}, env_file=NO_FILE)
    assert s.alert_sound is True and s.alert_notify is True
