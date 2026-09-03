import json

import httpx

from edgeboard.collectors.spotify_queue import QUEUE_URL, TOKEN_URL, NotConfigured, QueueClient, load_credentials, parse_queue

PAYLOAD = {
    "currently_playing": {"name": "now"},
    "queue": [
        {
            "name": "Outro",
            "duration_ms": 243000,
            "artists": [{"name": "M83"}, {"name": "Someone"}],
            "album": {"name": "Hurry Up", "images": [{"url": "big"}, {"url": "small"}]},
        },
        {"name": "Bare", "artists": [], "album": None, "duration_ms": None},
        "garbage",
    ],
}


def test_parse_queue():
    tracks = parse_queue(PAYLOAD)
    assert [t.title for t in tracks] == ["Outro", "Bare"]
    assert tracks[0].artist == "M83, Someone"
    assert tracks[0].album == "Hurry Up" and tracks[0].art_url == "small"
    assert tracks[0].length_s == 243.0
    assert tracks[1].to_dict() == {"title": "Bare", "artist": "", "album": "", "art_url": "", "length_s": 0.0}
    assert parse_queue({"queue": [{"name": str(i)} for i in range(20)]}, limit=3) == parse_queue({"queue": [{"name": str(i)} for i in range(3)]})
    assert parse_queue({}) == []


def test_load_credentials(tmp_path):
    f = tmp_path / "t.json"
    assert load_credentials(f) is None
    f.write_text("{not json")
    assert load_credentials(f) is None
    f.write_text(json.dumps({"client_id": "c"}))
    assert load_credentials(f) is None
    f.write_text(json.dumps({"client_id": "c", "refresh_token": "r", "extra": 1}))
    assert load_credentials(f) == {"client_id": "c", "refresh_token": "r"}


def _client(tmp_path, handler):
    f = tmp_path / "t.json"
    f.write_text(json.dumps({"client_id": "cid", "refresh_token": "r1"}))
    return QueueClient(f, http=httpx.Client(transport=httpx.MockTransport(handler))), f


def test_fetch_refreshes_once_and_rotates_refresh_token(tmp_path):
    calls = []

    def handler(request: httpx.Request):
        calls.append(str(request.url))
        if str(request.url) == TOKEN_URL:
            assert b"grant_type=refresh_token" in request.content and b"client_id=cid" in request.content
            return httpx.Response(200, json={"access_token": "A", "expires_in": 3600, "refresh_token": "r2"})
        assert request.headers["Authorization"] == "Bearer A"
        return httpx.Response(200, json=PAYLOAD)

    client, f = _client(tmp_path, handler)
    assert [t.title for t in client.fetch()] == ["Outro", "Bare"]
    assert [t.title for t in client.fetch()] == ["Outro", "Bare"]
    assert calls == [TOKEN_URL, QUEUE_URL, QUEUE_URL]  # token cached across polls
    assert json.loads(f.read_text())["refresh_token"] == "r2"


def test_fetch_errors(tmp_path):
    client, _ = _client(tmp_path, lambda r: httpx.Response(200, json={"access_token": "A"}) if str(r.url) == TOKEN_URL else httpx.Response(403))
    try:
        client.fetch()
    except RuntimeError as exc:
        assert "Premium" in str(exc)
    else:
        raise AssertionError("403 should raise")

    client, _ = _client(tmp_path, lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
    try:
        client.fetch()
    except RuntimeError as exc:
        assert "spotify_auth" in str(exc)
    else:
        raise AssertionError("rejected refresh token should raise")


def test_fetch_without_token_file_is_not_configured(tmp_path):
    client = QueueClient(tmp_path / "missing.json", http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    try:
        client.fetch()
    except NotConfigured:
        pass
    else:
        raise AssertionError("missing file must raise NotConfigured")


def test_queue_limit_covers_the_scrollable_list():
    from edgeboard.collectors.spotify_queue import QUEUE_LIMIT

    assert QUEUE_LIMIT == 20
