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
    # the guard admits loopback hosts only; the default base URL of TestClient ("testserver") is not one
    return TestClient(app, base_url="http://127.0.0.1:8765"), calls


def test_state_shape():
    client, _ = make_client()
    data = client.get("/api/state").json()
    for key in ("now", "usage", "sessions", "sessions_summary", "spotify", "spotify_queue", "system", "git", "github", "errors"):
        assert key in data
    assert data["usage"]["windows"] == []
    assert data["git"] == {"commits": [], "count": 0, "added": 0, "deleted": 0}
    assert data["github"] == {"configured": False, "runs": [], "running": 0, "failed": 0}
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


def test_open_route_spawns_the_configured_browser_on_the_configured_monitor(monkeypatch):
    from edgeboard import server

    spawned = []
    focused = []

    def spawn(argv, **kw):
        spawned.append((argv, kw))

    def focus(name):
        focused.append(name)

    monkeypatch.setattr(server.shutil, "which", lambda name: "/usr/bin/" + name)
    settings = Settings(open_command="firefox --new-window", open_monitor="DP-2")
    state = State()
    state.github = {"configured": True, "runs": [{"id": 1, "url": "https://github.com/o/r/actions/runs/1"}], "running": 1, "failed": 0}
    opener = lambda url: server.open_external(url, settings, spawn=spawn, focus=focus)  # noqa: E731
    app = create_app(settings, state, start_collectors=False, opener=opener)
    client = TestClient(app, base_url="http://127.0.0.1:8765")

    r = client.post("/api/open", json={"url": "https://github.com/o/r/actions/runs/1"})
    assert r.status_code == 200 and r.json()["opened"] is True
    # the monitor is focused first, then the browser is spawned detached with the URL appended
    assert focused == ["DP-2"]
    argv, kw = spawned[0]
    assert argv == ["firefox", "--new-window", "https://github.com/o/r/actions/runs/1"]
    assert kw["start_new_session"] is True
    # only the runs the snapshot carries may be opened, and only over http(s)
    assert client.post("/api/open", json={"url": "https://github.com/other/repo/actions/runs/9"}).status_code == 404
    assert client.post("/api/open", json={"url": "file:///etc/passwd"}).status_code == 422
    assert client.post("/api/open", json={"url": "javascript:alert(1)"}).status_code == 422
    assert len(spawned) == 1 and len(focused) == 1


def test_focus_monitor_speaks_both_hyprland_config_styles(monkeypatch):
    from edgeboard import server

    calls = []
    monkeypatch.setattr(server.shutil, "which", lambda name: "/usr/bin/" + name)
    server._focus_monitor("DP-2", runner=lambda argv, **kw: calls.append(argv))
    # the legacy parser takes the dispatcher, the Lua config the eval form (the
    # legacy call fails there, so both are sent and failures ignored)
    assert calls == [
        ["hyprctl", "dispatch", "focusmonitor", "DP-2"],
        ["hyprctl", "eval", 'hl.dispatch(hl.dsp.focus({ monitor = "DP-2" }))'],
    ]
    # a missing hyprctl or a suspicious monitor name never runs anything
    monkeypatch.setattr(server.shutil, "which", lambda name: None)
    server._focus_monitor("DP-2", runner=lambda argv, **kw: calls.append(argv))
    monkeypatch.setattr(server.shutil, "which", lambda name: "/usr/bin/" + name)
    server._focus_monitor('DP-2" }); os.execute("boom', runner=lambda argv, **kw: calls.append(argv))
    assert len(calls) == 2


def test_open_route_reports_a_missing_browser_and_never_spawns_in_demo(monkeypatch):
    from edgeboard import server

    spawned = []
    monkeypatch.setattr(server.shutil, "which", lambda name: None)

    def opener(url):
        return server.open_external(url, Settings(open_command=""), spawn=lambda *a, **kw: spawned.append(a))

    state = State()
    state.github = {"configured": True, "runs": [{"id": 1, "url": "https://github.com/o/r/actions/runs/1"}], "running": 1, "failed": 0}
    client = TestClient(create_app(Settings(), state, start_collectors=False, opener=opener), base_url="http://127.0.0.1:8765")
    # an empty open_command spawns nothing and the page gets a 503 to show
    assert client.post("/api/open", json={"url": "https://github.com/o/r/actions/runs/1"}).status_code == 503
    assert spawned == []
    # demo mode never touches the desktop, even with a command configured
    demo_state = State()
    demo = create_app(Settings(demo=True), demo_state, start_collectors=False)
    run = demo_state.github["runs"][0]
    r = TestClient(demo, base_url="http://127.0.0.1:8765").post("/api/open", json={"url": run["url"]})
    assert r.status_code == 200 and r.json()["opened"] is False
    assert spawned == []


def test_demo_mode_serves_canned_data():
    client, _ = make_client(demo=True)
    data = client.get("/api/state").json()
    assert data["usage"]["source"] == "demo"
    assert len(data["sessions"]) == 4
    assert data["sessions_summary"]["today"] > 4
    assert [w["key"] for w in data["usage"]["windows"]] == ["five_hour", "seven_day"]
    assert all(w["projected_full_at"] and w["rate_per_hour"] for w in data["usage"]["windows"])
    assert data["spotify_queue"]["configured"] and len(data["spotify_queue"]["tracks"]) == 6
    assert data["system"]["cpu"]["percent"] == 6.0
    assert data["git"]["count"] >= len(data["git"]["commits"]) > 0 and data["git"]["added"] > 0
    assert data["github"]["configured"] and data["github"]["running"] == 1 and data["github"]["failed"] == 1
    assert all(run["repo"] and run["name"] and run["branch"] for run in data["github"]["runs"])
    assert sum(s["commits"] for s in data["sessions"]) > 0
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

    client = TestClient(create_app(Settings(demo=True), State(), spotify_runner=runner, start_collectors=False), base_url="http://127.0.0.1:8765")
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


def _rate_limited(retry_after: str | None = None):
    import httpx

    headers = {"retry-after": retry_after} if retry_after is not None else {}
    request = httpx.Request("GET", "https://api.anthropic.com/api/oauth/usage")
    response = httpx.Response(429, headers=headers, request=request)
    return httpx.HTTPStatusError("429", request=request, response=response)


def _usage_collector(monkeypatch, responses):
    import asyncio

    from edgeboard.collectors import claude_usage
    from edgeboard.server import Collectors

    state = State()
    c = Collectors(Settings(usage_interval=60), state, lambda a: (0, ""))
    monkeypatch.setattr(claude_usage, "load_token", lambda claude_dir: "tok")

    async def fake_fetch(client, token, url):
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(claude_usage, "fetch_usage", fake_fetch)

    async def poll():
        try:
            return await c._usage(), None
        except Exception as exc:  # noqa: BLE001 - the loop's contract
            return None, exc

    return c, state, lambda: asyncio.run(poll())


def test_usage_429_marks_stale_and_backs_off_without_an_error(monkeypatch):
    ok = {"five_hour": {"utilization": 10, "resets_at": None}}
    c, state, poll = _usage_collector(monkeypatch, [ok, _rate_limited(), _rate_limited(), _rate_limited(), ok])
    assert poll() == (None, None)
    assert state.usage["source"] == "api" and state.usage["stale"] is False
    delay, exc = poll()
    assert exc is None and state.usage["stale"] is True
    assert state.usage["windows"][0]["utilization"] == 10  # last good value kept
    assert delay == 120
    assert poll()[0] == 240
    assert poll()[0] == 480
    delay, exc = poll()
    assert (delay, exc) == (None, None) and state.usage["stale"] is False


def test_usage_429_backoff_is_capped_and_honours_retry_after(monkeypatch):
    responses = [_rate_limited("300")] + [_rate_limited()] * 6
    c, state, poll = _usage_collector(monkeypatch, responses)
    assert poll()[0] == 300
    delays = [poll()[0] for _ in range(6)]
    assert max(delays) == 600 and delays[-1] == 600


def test_usage_429_surfaces_an_error_once_stale_for_too_long(monkeypatch):

    from edgeboard.server import USAGE_STALE_AFTER

    ok = {"five_hour": {"utilization": 10, "resets_at": None}}
    c, state, poll = _usage_collector(monkeypatch, [ok, _rate_limited(), _rate_limited()])
    poll()
    assert poll()[1] is None
    c._usage_ok_at -= USAGE_STALE_AFTER + 1
    _, exc = poll()
    assert isinstance(exc, RuntimeError) and "rate limited" in str(exc)
    assert state.usage["stale"] is True


def test_usage_non_429_http_errors_still_raise(monkeypatch):
    import httpx

    request = httpx.Request("GET", "https://x")
    err = httpx.HTTPStatusError("500", request=request, response=httpx.Response(500, request=request))
    c, state, poll = _usage_collector(monkeypatch, [err])
    _, exc = poll()
    assert isinstance(exc, httpx.HTTPStatusError) and state.usage["stale"] is True


def test_loop_uses_delay_returned_by_collector(monkeypatch):
    import asyncio

    from edgeboard.server import Collectors

    c = Collectors(Settings(), State(), lambda a: (0, ""))
    sleeps = []
    results = iter([None, 7.0, None])

    async def fn():
        return next(results)

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    try:
        asyncio.run(c._loop("x", 3.0, fn))
    except asyncio.CancelledError:
        pass
    assert [round(s) for s in sleeps] == [3, 7, 3]


def test_hook_route_stores_payload_per_session():
    client, _ = make_client()
    body = {"session_id": "abc", "hook_event_name": "Notification", "cwd": "/home/me/proj", "notification_type": "permission_prompt", "message": "Claude needs your permission"}
    r = client.post("/api/hook", json=body)
    assert r.status_code == 200 and r.json() == {"ok": True}
    stored = client.app.state.dashboard.hooks["abc"]
    assert stored["hook_event_name"] == "Notification" and stored["notification_type"] == "permission_prompt"
    assert isinstance(stored["ts"], float) and stored["ts"] > 0
    # a newer event for the same session replaces the old one
    client.post("/api/hook", json={"session_id": "abc", "hook_event_name": "Stop"})
    assert client.app.state.dashboard.hooks["abc"]["hook_event_name"] == "Stop"


def test_hook_route_rejects_malformed_bodies():
    client, _ = make_client()
    assert client.post("/api/hook", content=b"not json", headers={"content-type": "application/json"}).status_code == 400
    assert client.post("/api/hook", json=["a", "list"]).status_code == 400
    assert client.post("/api/hook", json={"hook_event_name": "Stop"}).status_code == 400  # no session id
    assert client.post("/api/hook", json={"session_id": "abc"}).status_code == 400  # no event name
    assert client.post("/api/hook", json={"session_id": 5, "hook_event_name": "Stop"}).status_code == 400
    assert client.app.state.dashboard.hooks == {}


def test_sessions_collector_passes_hooks_and_prunes_expired(monkeypatch, tmp_path):
    import asyncio
    import time

    from edgeboard.collectors import claude_sessions
    from edgeboard.server import Collectors

    state = State()
    state.hooks = {
        "fresh": {"hook_event_name": "Stop", "ts": time.time()},
        "old": {"hook_event_name": "Stop", "ts": time.time() - claude_sessions.HOOK_TTL - 1},
    }
    seen = {}

    def fake_collect(settings, now=None, hooks=None, adapters=None):
        seen["hooks"] = hooks
        return [], {"today": 0, "done": 0, "working": 0, "idle": 0, "attention": 0}, []

    import edgeboard.server as server

    monkeypatch.setattr(server, "collect_agents", fake_collect)
    c = Collectors(Settings(claude_dir=tmp_path), state, lambda a: (0, ""))
    asyncio.run(c._sessions())
    assert set(seen["hooks"]) == {"fresh"} and set(state.hooks) == {"fresh"}


def test_state_exposes_alert_settings_and_presets():
    client, _ = make_client(alert_sound=True, presets=(("go", "Go on."),))
    assert client.get("/api/state").json()["settings"] == {"alert_sound": True, "presets": [{"label": "go", "text": "Go on."}], "context_warn": 80, "system_interval": 1.0}
    client, _ = make_client(context_warn_pct=70)
    settings = client.get("/api/state").json()["settings"]
    assert settings["alert_sound"] is False and len(settings["presets"]) >= 3 and settings["context_warn"] == 70


def test_sessions_loop_notifies_on_attention_transitions(monkeypatch):
    import asyncio

    from edgeboard import server
    from edgeboard.server import Collectors

    rounds = [
        ([{"id": "a", "status": "working", "name": "Fix tests", "detail": "running pytest"}], {}),
        ([{"id": "a", "status": "idle", "name": "Fix tests", "detail": "waiting for you"}], {}),
        ([{"id": "a", "status": "idle", "name": "Fix tests", "detail": "waiting for you"}], {}),
        ([{"id": "a", "status": "attention", "name": "Fix tests", "detail": "needs permission"}], {}),
    ]

    class FakeSession(dict):
        def to_dict(self):
            return dict(self)

    def fake_collect(settings, now, hooks, adapters=None):
        sessions, summary = rounds.pop(0)
        return [FakeSession(s) for s in sessions], summary, []

    monkeypatch.setattr(server, "collect_agents", fake_collect)
    sent = []
    collectors = Collectors(Settings(alert_notify=True), State(), spotify_runner=lambda args: (0, ""), notifier=lambda title, body: sent.append((title, body)))

    async def run():
        for _ in range(4):
            await collectors._sessions()

    asyncio.run(run())
    assert sent == [("Claude is waiting for you", "Fix tests: waiting for you"), ("Claude needs you", "Fix tests: needs permission")]

    # off by default: the same transitions send nothing
    rounds.extend([
        ([{"id": "b", "status": "working", "name": "x", "detail": ""}], {}),
        ([{"id": "b", "status": "attention", "name": "x", "detail": "needs permission"}], {}),
    ])
    quiet = []
    collectors = Collectors(Settings(), State(), spotify_runner=lambda args: (0, ""), notifier=lambda t, b: quiet.append(t))

    async def run_twice():
        await collectors._sessions()
        await collectors._sessions()

    asyncio.run(run_twice())
    assert quiet == []


# ---------- git ----------


def _commit(hash, path, ts):
    from edgeboard.collectors.git import Commit

    return Commit(hash, path.rsplit("/", 1)[-1], path, "m", ts, "me", 3, 1)


def test_git_collector_reads_session_repositories_and_configured_ones(monkeypatch):
    import asyncio

    import edgeboard.server as server
    from edgeboard.collectors import git
    from edgeboard.server import Collectors

    state = State()
    state.sessions = [{"id": "a", "cwd": "/home/me/Dashboard/edgeboard", "started_at": "2026-09-04T09:30:00+00:00"}, {"id": "b", "cwd": ""}]
    seen = {}
    found = [_commit("b", "/home/me/Dashboard", "2026-09-04T10:00:00+00:00"), _commit("a", "/home/me/Dashboard", "2026-09-04T09:00:00+00:00"), _commit("c", "/srv/blog", "2026-09-04T08:00:00+00:00")]

    def fake_collect(cwds, since, runner=None):
        seen["cwds"], seen["since"] = list(cwds), since
        return found

    monkeypatch.setattr(git, "collect_git", fake_collect)
    c = Collectors(Settings(git_repos=("/srv/blog",)), state, lambda a: (0, ""))
    assert asyncio.run(c._git()) is None
    assert seen["cwds"] == ["/srv/blog", "/home/me/Dashboard/edgeboard", ""]
    assert seen["since"].hour == 0 and seen["since"].tzinfo is not None
    assert state.git["count"] == 3 and state.git["added"] == 9 and [x["hash"] for x in state.git["commits"]] == ["b", "a", "c"]

    # the next sessions round counts each session's commits since it started
    class FakeSession(dict):
        def to_dict(self):
            return dict(self)

    rows = [FakeSession({"id": "a", "status": "idle", "cwd": "/home/me/Dashboard/edgeboard", "started_at": "2026-09-04T09:30:00+00:00"}), FakeSession({"id": "d", "status": "done", "cwd": "/srv/blog", "started_at": None})]
    monkeypatch.setattr(server, "collect_agents", lambda *a, **k: (rows, {}, []))
    asyncio.run(c._sessions())
    assert [s["commits"] for s in state.sessions] == [1, 1]


def test_git_collector_retries_soon_while_there_is_nothing_to_read(monkeypatch):
    import asyncio

    from edgeboard.collectors import git
    from edgeboard.server import Collectors

    monkeypatch.setattr(git, "collect_git", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run git without a path")))
    state = State()
    assert asyncio.run(Collectors(Settings(), state, lambda a: (0, ""))._git()) == 5.0
    assert state.git["count"] == 0


# ---------- github ----------


def _gh_run(id: int, repo: str, status: str = "completed", conclusion: str = "failure", branch: str = "main", minutes_ago: float = 10.0, name: str = "ci"):
    from datetime import datetime, timedelta, timezone

    from edgeboard.collectors.github import Run

    stamp = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    return Run(id=id, repo=repo, name=name, title="t", branch=branch, status=status, conclusion=conclusion, url="", number=1, started_at=stamp, updated_at=stamp)


def test_github_collector_writes_runs_and_alerts_once_per_new_failure():
    import asyncio

    from edgeboard.collectors.github import GitHubError
    from edgeboard.server import Collectors

    class Client:
        def __init__(self):
            self.by_repo = {}

        def runs(self, repo):
            return list(self.by_repo.get(repo, []))

    notified = []
    state = State()
    c = Collectors(Settings(github_repos=("me/ci",), alert_notify=True), state, lambda a: (0, ""), notifier=lambda title, body: notified.append((title, body)))
    client = Client()
    c._github_client = client  # a real one would need the network
    client.by_repo["me/ci"] = [
        _gh_run(1, "me/ci", status="in_progress", conclusion="", minutes_ago=3),
        _gh_run(2, "me/ci", minutes_ago=12, branch="other_agents"),
    ]
    assert asyncio.run(c._github()) is None
    assert state.github["configured"] is True and state.github["running"] == 1 and state.github["failed"] == 1
    assert [r["id"] for r in state.github["runs"]] == [1, 2]
    assert notified == []  # the poll that first sees the failures only records them

    # a failure the previous poll did not show alerts once, and not again on the next poll
    client.by_repo["me/ci"] = [*client.by_repo["me/ci"], _gh_run(3, "me/ci", minutes_ago=1, branch="dev")]
    asyncio.run(c._github())
    assert state.github["failed"] == 2
    assert notified == [("CI failed", "me/ci ci on dev")]
    asyncio.run(c._github())
    assert notified == [("CI failed", "me/ci ci on dev")]

    # a failure that leaves the list (the row cap) and comes back must not alert again
    client.by_repo["me/ci"] = client.by_repo["me/ci"][:2]  # drops id 3
    asyncio.run(c._github())
    client.by_repo["me/ci"] = [*client.by_repo["me/ci"], _gh_run(3, "me/ci", minutes_ago=1, branch="dev")]
    asyncio.run(c._github())
    assert notified == [("CI failed", "me/ci ci on dev")]

    # a repository that cannot be read raises after writing what the others gave
    class Broken(Client):
        def runs(self, repo):
            raise GitHubError("HTTP 403 (rate limited, or the token cannot read this repository)")

    c._github_client = Broken()
    try:
        asyncio.run(c._github())
    except RuntimeError as exc:
        assert "me/ci: HTTP 403" in str(exc)
    else:
        raise AssertionError("a failing repository must raise for the error line")


def test_github_collector_without_a_token_is_not_an_error(monkeypatch):
    import asyncio

    import edgeboard.server as server
    from edgeboard.server import Collectors

    monkeypatch.setattr(server.github, "load_token", lambda explicit="": "")
    state = State()
    c = Collectors(Settings(github_repos=("me/ci",)), state, lambda a: (0, ""))
    assert asyncio.run(c._github()) is None
    assert state.github == {"configured": False, "runs": [], "running": 0, "failed": 0}
    assert c._github_client is None


def test_github_collector_retries_soon_without_repositories():
    import asyncio

    from edgeboard.server import Collectors

    state = State()
    assert asyncio.run(Collectors(Settings(), state, lambda a: (0, ""))._github()) == 5.0
    assert state.github["configured"] is False


# ---------- answering questions and sending presets ----------

_ASK_HOOK = {
    "session_id": "abc",
    "hook_event_name": "PreToolUse",
    "tool_name": "AskUserQuestion",
    "tool_use_id": "toolu_1",
    "tool_input": {"questions": [{"question": "Deploy where?", "header": "Target", "options": [{"label": "staging"}, {"label": "prod"}], "multiSelect": False}]},
}


def test_ask_hook_opens_a_pending_answer_the_script_can_poll():
    client, _ = make_client()
    assert client.get("/api/answer/toolu_1?wait=0").status_code == 404
    assert client.post("/api/hook", json=_ASK_HOOK).json() == {"ok": True}
    assert client.get("/api/answer/toolu_1?wait=0").json() == {"status": "pending"}
    r = client.post("/api/sessions/abc/answer", json={"tool_use_id": "toolu_1", "answers": {"Deploy where?": "staging"}})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert client.get("/api/answer/toolu_1?wait=0").json() == {"status": "answered", "answers": {"Deploy where?": "staging"}}
    assert client.app.state.dashboard.hooks["abc"]["question_state"] == "answered"


def test_answer_pass_hands_the_question_back_to_the_terminal():
    client, _ = make_client()
    client.post("/api/hook", json=_ASK_HOOK)
    assert client.post("/api/sessions/abc/answer", json={"tool_use_id": "toolu_1", "pass": True}).status_code == 200
    assert client.get("/api/answer/toolu_1?wait=0").json() == {"status": "pass"}


def test_answer_route_rejects_unknown_foreign_abandoned_and_malformed():
    from edgeboard.answers import ABANDON_AFTER

    client, _ = make_client()
    body = {"tool_use_id": "toolu_1", "answers": {"Deploy where?": "prod"}}
    assert client.post("/api/sessions/abc/answer", json=body).status_code == 404  # nothing pending
    client.post("/api/hook", json=_ASK_HOOK)
    assert client.post("/api/sessions/other/answer", json=body).status_code == 404  # not that session's question
    assert client.post("/api/sessions/abc/answer", json={"tool_use_id": "toolu_1"}).status_code == 422  # neither answers nor pass
    assert client.post("/api/sessions/abc/answer", json={"tool_use_id": "toolu_1", "answers": {"q": 5}}).status_code == 422
    import time as _time

    client.app.state.answers.expire(now=_time.time() + ABANDON_AFTER + 1)  # the hook script stopped polling
    assert client.post("/api/sessions/abc/answer", json=body).status_code == 409
    assert client.app.state.dashboard.hooks["abc"]["question_state"] == "abandoned"


def test_send_route_posts_into_the_session_inbox(monkeypatch, tmp_path):
    import edgeboard.server as server
    from edgeboard.collectors.claude_inbox import Inbox

    sent = []
    monkeypatch.setattr(server, "find_inbox", lambda claude_dir, sid: Inbox("/tmp/x.sock", "tok") if sid == "abc" else None)
    monkeypatch.setattr(server, "send_message", lambda inbox, text: sent.append((inbox, text)))
    client, _ = make_client(claude_dir=tmp_path)
    r = client.post("/api/sessions/abc/send", json={"text": "  run the tests  "})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert sent == [(Inbox("/tmp/x.sock", "tok"), "run the tests")]
    # the card flips to "working on your prompt" until the transcript catches up
    hook = client.app.state.dashboard.hooks["abc"]
    assert hook["hook_event_name"] == "UserPromptSubmit" and hook["prompt"] == "run the tests"
    assert client.post("/api/sessions/nope/send", json={"text": "hi"}).status_code == 404
    assert client.post("/api/sessions/abc/send", json={"text": "   "}).status_code == 422
    assert client.post("/api/sessions/abc/send", json={"text": "x" * 4001}).status_code == 422

    def boom(inbox, text):
        raise ConnectionRefusedError("nobody home")

    monkeypatch.setattr(server, "send_message", boom)
    r = client.post("/api/sessions/abc/send", json={"text": "hi"})
    assert r.status_code == 502 and "nobody home" in r.json()["detail"]


def test_demo_mode_answers_and_sends_without_touching_anything(monkeypatch):
    import edgeboard.server as server

    monkeypatch.setattr(server, "find_inbox", lambda *a: (_ for _ in ()).throw(AssertionError("demo must not look for inboxes")))
    client, _ = make_client(demo=True)
    sessions = client.get("/api/state").json()["sessions"]
    asking = next(s for s in sessions if s["question"])
    q = asking["question"]
    r = client.post(f"/api/sessions/{asking['id']}/answer", json={"tool_use_id": q["tool_use_id"], "answers": {q["questions"][0]["question"]: q["questions"][0]["options"][0]}})
    assert r.status_code == 200
    after = next(s for s in client.get("/api/state").json()["sessions"] if s["id"] == asking["id"])
    assert after["question"] is None and after["status"] == "working"
    idle = next(s for s in sessions if s["can_send"])
    assert client.post(f"/api/sessions/{idle['id']}/send", json={"text": "carry on"}).status_code == 200
    after = next(s for s in client.get("/api/state").json()["sessions"] if s["id"] == idle["id"])
    assert after["status"] == "working" and after["last_prompt"] == "carry on"
    assert client.post("/api/sessions/demo-1/send", json={"text": "x"}).status_code in (200, 404)


def test_sessions_collector_expires_pending_answers(monkeypatch, tmp_path):
    import asyncio
    import time

    import edgeboard.server as server
    from edgeboard.answers import ABANDON_AFTER, Answers
    from edgeboard.server import Collectors

    state = State()
    state.hooks = {"abc": {**_ASK_HOOK, "ts": time.time()}}
    answers = Answers(state.hooks)
    answers.open("toolu_1", "abc", now=time.time() - ABANDON_AFTER - 1)
    monkeypatch.setattr(server, "collect_agents", lambda *a, **k: ([], {"today": 0, "done": 0, "working": 0, "idle": 0, "attention": 0}, []))
    c = Collectors(Settings(claude_dir=tmp_path), state, lambda a: (0, ""), answers=answers)
    asyncio.run(c._sessions())
    assert state.hooks["abc"]["question_state"] == "abandoned"


def test_codex_permission_hook_is_answerable_from_the_panel():
    client, _ = make_client()
    hook = {"session_id": "thr_1", "agent": "codex", "hook_event_name": "PermissionRequest", "tool_use_id": "perm_1", "tool_name": "Bash", "tool_input": {"command": "git push"}}
    assert client.post("/api/hook", json=hook).json() == {"ok": True}
    assert client.get("/api/answer/perm_1?wait=0").json() == {"status": "pending"}
    r = client.post("/api/sessions/thr_1/answer", json={"tool_use_id": "perm_1", "answers": {"Allow Bash?": "Allow"}})
    assert r.status_code == 200
    assert client.get("/api/answer/perm_1?wait=0").json() == {"status": "answered", "answers": {"Allow Bash?": "Allow"}}


def test_opencode_answers_and_prompts_go_through_the_service(monkeypatch, tmp_path):
    from edgeboard.collectors import opencode

    calls = []

    def fake_request(service, timeout=5.0):
        def request(method, path, body=None):
            calls.append((method, path, body))
            return {"data": {}}

        return request

    state_file = tmp_path / "service.json"
    state_file.write_text('{"url": "http://127.0.0.1:49374", "password": "pw"}')
    monkeypatch.setattr(opencode, "default_request", fake_request)
    opencode.PENDING._items.clear()
    opencode.PENDING.remember("per_1", opencode.Pending("ses_1", "permission", "per_1", {"Allow Bash?": {"key": "", "type": "choice", "options": {"Allow once": "once", "Deny": "reject"}}}))
    client, _ = make_client(opencode_state_file=state_file)

    # answering a permission from the panel reaches the service's reply route
    r = client.post("/api/sessions/ses_1/answer", json={"tool_use_id": "per_1", "answers": {"Allow Bash?": "Allow once"}})
    assert r.status_code == 200 and calls[-1] == ("POST", "/api/session/ses_1/permission/per_1/reply", {"decision": "once"})
    assert opencode.PENDING.get("per_1") is not None  # only the collector expires entries

    # a prompt goes to the prompt route; a done session resumes instead of steering
    r = client.post("/api/sessions/ses_1/send", json={"text": "carry on"})
    assert r.status_code == 200 and calls[-1] == ("POST", "/api/session/ses_1/prompt", {"text": "carry on"})
    client.app.state.dashboard.sessions = [{"id": "ses_9", "agent": "opencode", "status": "done"}]
    r = client.post("/api/sessions/ses_9/send", json={"text": "again"})
    assert r.status_code == 200 and calls[-1] == ("POST", "/api/session/ses_9/prompt", {"text": "again", "resume": True})

    # an unknown question, a missing option and a missing service all fail cleanly
    assert client.post("/api/sessions/ses_1/answer", json={"tool_use_id": "nope", "answers": {"q": "a"}}).status_code == 404
    opencode.PENDING.remember("per_2", opencode.Pending("ses_1", "form", "frm_1", {"Deploy where?": {"key": "env", "type": "string", "options": {}}}))
    assert client.post("/api/sessions/ses_1/answer", json={"tool_use_id": "per_2", "answers": {}}).status_code == 422
    state_file.unlink()
    assert client.post("/api/sessions/ses_1/send", json={"text": "x"}).status_code == 404
    opencode.PENDING._items.clear()


def test_codex_send_queues_a_message(monkeypatch):
    import edgeboard.server as server

    queued = []
    monkeypatch.setattr(server, "queue_message", lambda thread_id, text: queued.append((thread_id, text)))
    client, _ = make_client()
    client.app.state.dashboard.sessions = [{"id": "01a0b864-e4b3-76c0-96d9-35853e476d60", "agent": "codex", "status": "idle"}]
    r = client.post("/api/sessions/01a0b864-e4b3-76c0-96d9-35853e476d60/send", json={"text": "carry on"})
    assert r.status_code == 200 and queued == [("01a0b864-e4b3-76c0-96d9-35853e476d60", "carry on")]

    def boom(thread_id, text):
        raise RuntimeError("codex queue failed")

    monkeypatch.setattr(server, "queue_message", boom)
    r = client.post("/api/sessions/01a0b864-e4b3-76c0-96d9-35853e476d60/send", json={"text": "x"})
    assert r.status_code == 502 and "codex queue failed" in r.json()["detail"]


# ---------- origin guard ----------


def test_api_rejects_foreign_host_and_origin():
    # A browser on the same machine can reach loopback: a cross-site "simple request"
    # (text/plain body, no preflight) or DNS rebinding must not drive the API.
    client, calls = make_client(demo=True)
    evil = {"origin": "https://evil.example"}
    assert client.post("/api/spotify/play_pause", headers=evil).status_code == 403
    assert client.post("/api/hook", content=b'{"session_id":"demo-2","hook_event_name":"Stop"}', headers={**evil, "content-type": "text/plain"}).status_code == 403
    assert client.get("/api/state", headers={"host": "attacker.example:8765"}).status_code == 403
    assert client.get("/api/events", headers={"host": "attacker.example"}).status_code == 403
    assert client.app.state.dashboard.hooks == {} and calls == []
    assert client.get("/api/state").json()["spotify"]["status"] == "Playing"


def test_api_accepts_loopback_hosts_and_same_origin():
    client, _ = make_client(demo=True)
    for host in ("127.0.0.1:8765", "localhost", "[::1]:8765", "127.0.0.1"):
        assert client.get("/api/state", headers={"host": host}).status_code == 200, host
    assert client.post("/api/spotify/play_pause", headers={"origin": "http://127.0.0.1:8765"}).status_code == 200
    assert client.post("/api/spotify/play_pause", headers={"host": "localhost:8765", "origin": "http://localhost:8765"}).status_code == 200
    # "null" origins (file://, sandboxed iframes) are not the page itself
    assert client.post("/api/spotify/play_pause", headers={"origin": "null"}).status_code == 403
    # the page and its assets are not gated (the kiosk loads them without an Origin anyway)
    assert client.get("/", headers={"host": "attacker.example"}).status_code == 200


def test_api_allows_the_configured_host():
    client, _ = make_client(demo=True, host="192.168.1.20")
    assert client.get("/api/state", headers={"host": "192.168.1.20:8765"}).status_code == 200
    assert client.get("/api/state", headers={"host": "192.168.1.21:8765"}).status_code == 403
    assert client.post("/api/spotify/next", headers={"host": "192.168.1.20:8765", "origin": "http://192.168.1.20:8765"}).status_code == 200


def test_hook_route_requires_a_json_content_type():
    client, _ = make_client()
    body = b'{"session_id":"abc","hook_event_name":"Stop"}'
    assert client.post("/api/hook", content=body, headers={"content-type": "text/plain"}).status_code == 415
    assert client.post("/api/hook", content=body).status_code == 415
    assert client.post("/api/hook", content=body, headers={"content-type": "application/json; charset=utf-8"}).status_code == 200


def test_request_allowed_is_pure():
    from edgeboard.server import request_allowed

    allowed = {"127.0.0.1", "localhost", "::1"}
    assert request_allowed("127.0.0.1:8765", None, allowed)
    assert request_allowed("LOCALHOST", "http://localhost:8765", allowed)
    assert request_allowed("[::1]:8765", "http://[::1]:8765", allowed)
    assert not request_allowed("", None, allowed)
    assert not request_allowed("127.0.0.1", "http://127.0.0.1.evil.example", allowed)
    assert not request_allowed("127.0.0.1", "https://evil.example", allowed)
    assert not request_allowed("evil.example", None, allowed)
    assert not request_allowed("127.0.0.1", "not a url", allowed)


# ---------- build id / page reload ----------


def test_build_id_changes_with_the_static_files(tmp_path):
    from edgeboard import __version__
    from edgeboard.server import STATIC_DIR, build_id

    build = build_id()
    assert build.startswith(__version__ + "+") and len(build) > len(__version__) + 1
    assert build == build_id(STATIC_DIR)  # deterministic
    copy = tmp_path / "static"
    copy.mkdir()
    for name in ("app.js", "style.css", "index.html"):
        copy.joinpath(name).write_bytes((STATIC_DIR / name).read_bytes())
    assert build_id(copy) == build
    with copy.joinpath("app.js").open("a") as fh:
        fh.write("\n// changed\n")
    assert build_id(copy) != build


def test_snapshot_carries_the_build_and_the_page_links_assets_by_it():
    client, _ = make_client()
    build = client.get("/api/state").json()["version"]
    assert "+" in build
    html = client.get("/").text
    assert "__BUILD__" not in html
    assert f'src="/static/app.js?v={build}"' in html and f'href="/static/style.css?v={build}"' in html
