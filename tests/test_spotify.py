import pytest

from xdash.collectors.spotify import FORMAT, SEP, control, parse_metadata, read_spotify

SAMPLE = SEP.join(["Playing", "Song A", "Band B", "Album C", "https://i.scdn.co/image/abc", "215000000", "63500000", "On"]) + "\n"


def test_parse_metadata():
    s = parse_metadata(SAMPLE)
    assert s.running and s.status == "Playing"
    assert s.title == "Song A" and s.artist == "Band B" and s.album == "Album C"
    assert s.art_url == "https://i.scdn.co/image/abc"
    assert s.length_s == 215.0 and s.position_s == 63.5
    assert s.shuffle is True


def test_parse_metadata_garbage():
    assert parse_metadata("No players found\n").running is False


def test_read_spotify_runner_paths():
    calls = []

    def ok(args):
        calls.append(args)
        return 0, SAMPLE

    s = read_spotify(ok, "spotify")
    assert s.title == "Song A"
    assert calls[0] == ["playerctl", "-p", "spotify", "metadata", "--format", FORMAT]
    assert read_spotify(lambda a: (1, ""), "spotify").running is False
    missing = read_spotify(lambda a: (127, ""), "spotify")
    assert missing.running is False and missing.available is False


def test_control():
    calls = []
    assert control(lambda a: (calls.append(a) or 0, ""), "spotify", "play_pause") is True
    assert calls == [["playerctl", "-p", "spotify", "play-pause"]]
    with pytest.raises(ValueError):
        control(lambda a: (0, ""), "spotify", "explode")
