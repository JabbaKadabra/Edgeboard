"""FastAPI application: static page, snapshot API, SSE stream, Spotify controls."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from xdash.collectors import claude_usage, spotify
from xdash.collectors.claude_sessions import collect_sessions
from xdash.collectors.system import SystemSampler
from xdash.config import Settings
from xdash.demo import fill_demo
from xdash.state import State

log = logging.getLogger("xdash")
STATIC_DIR = Path(__file__).parent / "static"


class Collectors:
    """Background loops that keep ``State`` fresh. One task per source."""

    def __init__(self, settings: Settings, state: State, spotify_runner: spotify.Runner):
        self.settings = settings
        self.state = state
        self.spotify_runner = spotify_runner
        self.sampler: SystemSampler | None = None
        self._tasks: list[asyncio.Task] = []
        self._last_error: dict[str, str | None] = {}
        self._events_cache: list = []
        self._token: str | None = None
        self._token_checked = 0.0

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.sampler = await loop.run_in_executor(None, SystemSampler)
        self._tasks = [
            asyncio.create_task(self._loop("system", self.settings.system_interval, self._system)),
            asyncio.create_task(self._loop("spotify", self.settings.spotify_interval, self._spotify)),
            asyncio.create_task(self._loop("sessions", self.settings.sessions_interval, self._sessions)),
            asyncio.create_task(self._loop("timeline", self.settings.timeline_interval, self._timeline)),
            asyncio.create_task(self._loop("usage", self.settings.usage_interval, self._usage)),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _loop(self, name: str, interval: float, fn: Callable[[], Awaitable[None]]) -> None:
        while True:
            started = asyncio.get_running_loop().time()
            try:
                await fn()
                self._note_error(name, None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a collector must never take the server down
                self._note_error(name, f"{type(exc).__name__}: {exc}")
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(0.1, interval - elapsed))

    def _note_error(self, name: str, message: str | None) -> None:
        if message != self._last_error.get(name):
            self._last_error[name] = message
            if message:
                log.warning("%s collector: %s", name, message)
            else:
                log.info("%s collector recovered", name)
        if name in ("usage", "timeline"):
            # Both loops feed the usage panel, so it shows whichever is failing.
            self.state.errors["usage"] = self._last_error.get("usage") or self._last_error.get("timeline")
        else:
            self.state.errors[name] = message

    async def _run(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

    async def _system(self) -> None:
        assert self.sampler is not None
        self.state.system = await self._run(self.sampler.sample)

    async def _spotify(self) -> None:
        result = await self._run(spotify.read_spotify, self.spotify_runner, self.settings.spotify_player)
        self.state.spotify = result.to_dict()

    async def _sessions(self) -> None:
        sessions, summary = await self._run(collect_sessions, self.settings)
        self.state.sessions = [s.to_dict() for s in sessions]
        self.state.sessions_summary = summary

    async def _timeline(self) -> None:
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=7, hours=1)
        events = await self._run(claude_usage.load_all_events, self.settings.claude_dir, since)
        self._events_cache = events
        buckets = claude_usage.timeline(events, now)
        usage = dict(self.state.usage)
        usage["today"] = claude_usage.today_totals(events, now).to_dict()
        usage["timeline"] = [b.to_dict() for b in buckets]
        usage["peak"] = max((b.tokens for b in buckets), default=0)
        if usage.get("source") in ("none", "local"):
            usage["windows"] = [w.to_dict() for w in claude_usage.local_windows(events, now)]
            usage["source"] = "local"
        self.state.usage = usage

    async def _usage(self) -> None:
        now = datetime.now(timezone.utc)
        loop_time = asyncio.get_running_loop().time()
        if self._token is None or loop_time - self._token_checked > 300:
            self._token = await self._run(claude_usage.load_token, self.settings.claude_dir)
            self._token_checked = loop_time
        usage = dict(self.state.usage)
        if not self._token:
            usage["source"] = "local"
            usage["windows"] = [w.to_dict() for w in claude_usage.local_windows(self._events_cache, now)]
            self.state.usage = usage
            raise RuntimeError("no OAuth token in .credentials.json; showing local estimate")
        try:
            async with httpx.AsyncClient() as client:
                data = await claude_usage.fetch_usage(client, self._token, self.settings.usage_url)
        except (httpx.HTTPError, ValueError) as exc:
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 401:
                self._token = None  # force a re-read next round; Claude Code may have refreshed it
            usage["stale"] = True
            self.state.usage = usage
            raise
        usage["windows"] = [w.to_dict() for w in claude_usage.parse_usage_response(data, now)]
        usage["source"] = "api"
        usage["stale"] = False
        usage["updated_at"] = now.isoformat()
        self.state.usage = usage


async def sse_stream(state: State, interval: float = 1.0):
    """Yield one ``state`` event per interval plus a periodic keep-alive comment."""
    tick = 0
    while True:
        yield f"event: state\ndata: {json.dumps(state.snapshot())}\n\n"
        tick += 1
        if tick % 15 == 0:
            yield ": keep-alive\n\n"
        await asyncio.sleep(interval)


def create_app(
    settings: Settings | None = None,
    state: State | None = None,
    spotify_runner: spotify.Runner | None = None,
    start_collectors: bool = True,
) -> FastAPI:
    settings = settings or Settings.from_env()
    state = state or State()
    runner = spotify_runner or spotify.default_runner
    if settings.demo:
        fill_demo(state)
        start_collectors = False
    collectors = Collectors(settings, state, runner)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_collectors:
            await collectors.start()
        try:
            yield
        finally:
            if start_collectors:
                await collectors.stop()

    app = FastAPI(title="xdash", lifespan=lifespan)
    app.state.settings = settings
    app.state.dashboard = state
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/state")
    async def api_state():
        return JSONResponse(state.snapshot(), headers={"Cache-Control": "no-store"})

    @app.get("/api/events")
    async def api_events():
        return StreamingResponse(
            sse_stream(state),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/spotify/{action}")
    async def api_spotify(action: str):
        if action not in spotify.ACTIONS:
            raise HTTPException(status_code=404, detail="unknown action")
        if settings.demo:
            # Demo mode never touches a real player.
            if action == "play_pause":
                state.spotify["status"] = "Paused" if state.spotify.get("status") == "Playing" else "Playing"
            return {"ok": True, "spotify": state.spotify}
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(None, spotify.control, runner, settings.spotify_player, action)
        await asyncio.sleep(0.25)  # let the player update before re-reading
        fresh = await loop.run_in_executor(None, spotify.read_spotify, runner, settings.spotify_player)
        # The polling loop may also write state.spotify; a stale overwrite heals on its next tick.
        state.spotify = fresh.to_dict()
        return {"ok": ok, "spotify": state.spotify}

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    log.info("xdash listening on http://%s:%d (demo=%s)", settings.host, settings.port, settings.demo)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="warning")
