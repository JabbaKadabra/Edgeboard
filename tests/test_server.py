from fastapi.testclient import TestClient

from edgeboard.config import Settings
from edgeboard.server import create_app
from edgeboard.state import State


def make_client(**kw):
    calls = []

    def runner(args):
        calls.append(args)
        return 0, ""

    app = create_app(Settings(claude_dir=kw.pop("claude_dir", None) or Settings().claude_dir, **kw), State(), spotify_runner=runner, start_collectors=False)
    return TestClient(app), calls


def test_state_shape():
    client, _ = make_client()
    data = client.get("/api/state").json()
    for key in ("now", "usage", "sessions", "sessions_summary", "spotify", "spotify_queue", "system", "errors"):
        assert key in data
    assert data["usage"]["windows"] == []
    assert data["spotify_queue"] == {"configured": False, "tracks": []}


def test_index_and_static():
    client, _ = make_client()
    r = client.get("/")
    assert r.status_code == 200 and "<html" in r.text.lower()
    assert client.get("/static/app.js").status_code == 200


def test_spotify_control():
    client, calls = make_client()
    r = client.post("/api/spotify/next")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert calls[0] == ["playerctl", "-p", "spotify", "next"]
    assert client.post("/api/spotify/explode").status_code == 404


def test_demo_mode_serves_canned_data():
    client, _ = make_client(demo=True)
    data = client.get("/api/state").json()
    assert data["usage"]["source"] == "demo"
    assert len(data["sessions"]) == 4
    assert data["sessions_summary"]["today"] > 4
    assert [w["key"] for w in data["usage"]["windows"]] == ["five_hour", "seven_day"]
    assert data["spotify_queue"]["configured"] and len(data["spotify_queue"]["tracks"]) == 6
    assert data["system"]["cpu"]["percent"] == 6.0
    r = client.post("/api/spotify/play_pause").json()
    assert r["spotify"]["status"] == "Paused"


def test_sse_stream_events():
    import asyncio
    import json

    from edgeboard.server import sse_stream

    async def run():
        gen = sse_stream(State(), interval=0)
        first = await gen.__anext__()
        rest = [await gen.__anext__() for _ in range(15)]
        await gen.aclose()
        return first, rest

    first, rest = asyncio.run(run())
    assert first.startswith("event: state\ndata: {")
    payload = json.loads(first.split("data: ", 1)[1])
    assert "sessions" in payload
    assert ": keep-alive\n\n" in rest


def test_demo_mode_never_runs_playerctl():
    calls = []

    def runner(args):
        calls.append(args)
        return 0, ""

    client = TestClient(create_app(Settings(demo=True), State(), spotify_runner=runner, start_collectors=False))
    assert client.post("/api/spotify/next").json()["ok"] is True
    assert calls == []


def test_note_error_keeps_usage_error_while_either_loop_fails():
    from edgeboard.server import Collectors

    state = State()
    c = Collectors(Settings(), state, lambda a: (0, ""))
    c._note_error("usage", "boom")
    c._note_error("timeline", None)
    assert state.errors["usage"] == "boom"
    c._note_error("usage", None)
    assert state.errors["usage"] is None
    c._note_error("timeline", "tl broke")
    c._note_error("usage", None)
    assert state.errors["usage"] == "tl broke"
    c._note_error("timeline", None)
    assert state.errors["usage"] is None
    c._note_error("spotify", "gone")
    assert state.errors["spotify"] == "gone"


def test_note_error_merges_queue_into_spotify_panel():
    from edgeboard.server import Collectors

    state = State()
    c = Collectors(Settings(), state, lambda a: (0, ""))
    c._note_error("queue", "Premium needed")
    c._note_error("spotify", None)
    assert state.errors["spotify"] == "Premium needed"
    c._note_error("queue", None)
    assert state.errors["spotify"] is None
    c._note_error("system", "no sensors")
    assert state.errors["system"] == "no sensors"


def test_spotify_seek_maps_fraction_to_seconds_of_current_track():
    client, calls = make_client()
    client.app.state.dashboard.spotify = {"running": True, "length_s": 200.0}
    r = client.post("/api/spotify/seek", json={"fraction": 0.25})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert calls[0] == ["playerctl", "-p", "spotify", "position", "50"]
    assert client.post("/api/spotify/seek", json={"fraction": 1.5}).status_code == 422
    assert client.post("/api/spotify/seek", json={"fraction": -0.1}).status_code == 422
    assert client.post("/api/spotify/seek", json={}).status_code == 422


def test_spotify_volume_route():
    client, calls = make_client()
    r = client.post("/api/spotify/volume", json={"volume": 0.4})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert calls[0] == ["playerctl", "-p", "spotify", "volume", "0.4"]
    assert client.post("/api/spotify/volume", json={"volume": 2}).status_code == 422
    assert client.post("/api/spotify/volume", json={"volume": "loud"}).status_code == 422


def test_spotify_skip_route_advances_index_plus_one_tracks():
    client, calls = make_client()
    r = client.post("/api/spotify/skip", json={"index": 2})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert calls[:3] == [["playerctl", "-p", "spotify", "next"]] * 3
    assert client.post("/api/spotify/skip", json={"index": -1}).status_code == 422


def test_spotify_skip_trims_the_queue_immediately_and_returns_it():
    client, _ = make_client()
    state = client.app.state.dashboard
    tracks = [{"title": f"t{i}", "artist": "", "album": "", "art_url": "", "length_s": 1.0} for i in range(5)]
    state.spotify_queue = {"configured": True, "tracks": tracks}
    r = client.post("/api/spotify/skip", json={"index": 2}).json()
    assert r["spotify_queue"]["tracks"] == tracks[3:]
    assert client.get("/api/state").json()["spotify_queue"]["tracks"] == tracks[3:]
    assert client.post("/api/spotify/skip", json={"index": 50}).status_code == 422


def test_demo_mode_seek_volume_skip_touch_only_state():
    client, calls = make_client(demo=True)
    sp = client.post("/api/spotify/seek", json={"fraction": 0.5}).json()["spotify"]
    assert sp["position_s"] == 0.5 * sp["length_s"]
    assert client.post("/api/spotify/volume", json={"volume": 0.2}).json()["spotify"]["volume"] == 0.2
    before = client.get("/api/state").json()["spotify_queue"]["tracks"]
    after = client.post("/api/spotify/skip", json={"index": 1}).json()
    assert after["spotify"]["title"] == before[1]["title"]
    assert client.get("/api/state").json()["spotify_queue"]["tracks"] == before[2:]
    assert calls == []
