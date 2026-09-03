from fastapi.testclient import TestClient

from xdash.config import Settings
from xdash.server import create_app
from xdash.state import State


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
    for key in ("now", "usage", "sessions", "sessions_summary", "spotify", "system", "errors"):
        assert key in data
    assert data["usage"]["windows"] == []


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
    assert len(data["sessions"]) == 6
    assert data["system"]["cpu"]["percent"] == 6.0
    r = client.post("/api/spotify/play_pause").json()
    assert r["spotify"]["status"] == "Paused"


def test_sse_stream_events():
    import asyncio
    import json

    from xdash.server import sse_stream

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
