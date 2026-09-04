"""FastAPI application: static page, snapshot API, SSE stream, Spotify controls."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from edgeboard.answers import Answers
from edgeboard.collectors import claude_usage, spotify, spotify_queue
from edgeboard.collectors.claude_inbox import find_inbox, send_message
from edgeboard.collectors.claude_sessions import ATTENTION, IDLE, WORKING, attention_transitions, collect_sessions, os_pid_alive, prune_hooks
from edgeboard.collectors.system import SystemSampler
from edgeboard.config import Settings
from edgeboard.demo import fill_demo
from edgeboard.state import State

log = logging.getLogger("edgeboard")
STATIC_DIR = Path(__file__).parent / "static"


class SeekBody(BaseModel):
    fraction: float = Field(ge=0.0, le=1.0)


class VolumeBody(BaseModel):
    volume: float = Field(ge=0.0, le=1.0)


class SkipBody(BaseModel):
    index: int = Field(ge=0, lt=spotify_queue.QUEUE_LIMIT)


class AnswerBody(BaseModel):
    """Either ``answers`` (question text -> chosen label or free text) or ``pass`` (answer in the terminal)."""

    tool_use_id: str = Field(min_length=1)
    answers: dict[str, str] | None = None
    pass_: bool = Field(default=False, alias="pass")

    @model_validator(mode="after")
    def _one_of(self) -> "AnswerBody":
        if bool(self.answers) == self.pass_:
            raise ValueError("give either answers or pass")
        return self


class SendBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def _strip(self) -> "SendBody":
        self.text = self.text.strip()
        if not self.text:
            raise ValueError("text is blank")
        return self


ANSWER_WAIT_MAX = 30.0  # longest a single GET /api/answer long-poll may hang

# Hosts a request may name (``Host`` header, ``Origin`` host) besides the
# configured bind address. The server has no auth, so anything on the machine
# that can talk to loopback must at least *be* loopback: this stops a web page
# in the desktop browser from driving the API with a cross-site "simple
# request" (no preflight) and DNS rebinding from reading the snapshot.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", ""})


# A rate-limited usage poll keeps the last value and backs off; only once the
# panel has been stale this long is it worth a red error under the clock.
USAGE_STALE_AFTER = 15 * 60
USAGE_BACKOFF_MAX = 10 * 60


Notifier = Callable[[str, str], None]


def _hostname(netloc: str) -> str:
    """``[::1]:8765`` -> ``::1``, ``localhost:8765`` -> ``localhost``; lower-cased."""
    try:
        return (urlsplit(f"//{netloc}").hostname or "").lower()
    except ValueError:
        return ""


def request_allowed(host: str, origin: str | None, allowed_hosts: frozenset[str] | set[str]) -> bool:
    """Whether an API request's ``Host`` and (when sent) ``Origin`` headers name one of ``allowed_hosts``.

    Browsers send ``Origin`` on every cross-site request and on same-origin
    POSTs; a bare ``null`` (file://, sandboxed frames) is not the page itself.
    """
    if _hostname(host) not in allowed_hosts:
        return False
    if origin is None:
        return True
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False
    return _hostname(parts.netloc) in allowed_hosts


def desktop_notify(title: str, body: str) -> None:
    """Show a desktop notification on the main monitor via ``notify-send`` (no-op without it)."""
    if not shutil.which("notify-send"):
        return
    subprocess.run(["notify-send", "-a", "edgeboard", "-u", "normal", title, body], check=False, timeout=5)


class Collectors:
    """Background loops that keep ``State`` fresh. One task per source."""

    def __init__(self, settings: Settings, state: State, spotify_runner: spotify.Runner, notifier: Notifier = desktop_notify, answers: Answers | None = None):
        self.settings = settings
        self.state = state
        self.spotify_runner = spotify_runner
        self.notifier = notifier
        self.answers = answers or Answers(state.hooks)
        self._statuses: dict[str, str] = {}  # session id -> status of the previous round, for attention alerts
        self.sampler: SystemSampler | None = None
        self._tasks: list[asyncio.Task] = []
        self._last_error: dict[str, str | None] = {}
        self._events_cache: list = []
        self._token: str | None = None
        self._token_checked = 0.0
        self._usage_ok_at = 0.0  # loop time of the last successful usage poll
        self._usage_rate_limited = 0  # consecutive 429s, drives the backoff
        self._usage_samples: dict[str, list[claude_usage.Sample]] = {}  # per window key, for the pace projection
        self._queue_client = spotify_queue.QueueClient(settings.spotify_token_file)

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.sampler = await loop.run_in_executor(None, SystemSampler)
        self._tasks = [
            asyncio.create_task(self._loop("system", self.settings.system_interval, self._system)),
            asyncio.create_task(self._loop("spotify", self.settings.spotify_interval, self._spotify)),
            asyncio.create_task(self._loop("queue", self.settings.spotify_queue_interval, self._queue)),
            asyncio.create_task(self._loop("sessions", self.settings.sessions_interval, self._sessions)),
            asyncio.create_task(self._loop("timeline", self.settings.timeline_interval, self._timeline)),
            asyncio.create_task(self._loop("usage", self.settings.usage_interval, self._usage)),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def refresh_queue_soon(self, delay: float = 1.5) -> None:
        """Re-read the queue ahead of its regular interval, once the player has caught up."""
        if not self._tasks:
            return  # collectors are not running (tests, demo)

        async def refresh() -> None:
            await asyncio.sleep(delay)
            try:
                await self._queue()
                self._note_error("queue", None)
            except Exception as exc:  # noqa: BLE001 - same contract as _loop
                self._note_error("queue", f"{type(exc).__name__}: {exc}")

        asyncio.get_running_loop().create_task(refresh())

    async def _loop(self, name: str, interval: float, fn: Callable[[], Awaitable[float | None]]) -> None:
        """Run ``fn`` every ``interval`` seconds; it may return a longer delay for the next round."""
        while True:
            started = asyncio.get_running_loop().time()
            wait = interval
            try:
                delay = await fn()
                if delay:
                    wait = delay
                self._note_error(name, None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a collector must never take the server down
                self._note_error(name, f"{type(exc).__name__}: {exc}")
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(0.1, wait - elapsed))

    def _note_error(self, name: str, message: str | None) -> None:
        if message != self._last_error.get(name):
            self._last_error[name] = message
            if message:
                log.warning("%s collector: %s", name, message)
            else:
                log.info("%s collector recovered", name)
        # Loops that share a panel merge their errors so it shows whichever is failing.
        shared = {"usage": ("usage", "timeline"), "timeline": ("usage", "timeline"), "spotify": ("spotify", "queue"), "queue": ("spotify", "queue")}
        if name in shared:
            panel, *_ = shared[name]
            self.state.errors[panel] = next((self._last_error.get(n) for n in shared[name] if self._last_error.get(n)), None)
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

    async def _queue(self) -> None:
        if not self.state.spotify.get("running"):
            # Nothing playing: no point spending API calls; keep the last list only while configured.
            self.state.spotify_queue = {**self.state.spotify_queue, "tracks": []}
            return
        try:
            tracks = await self._run(self._queue_client.fetch)
        except spotify_queue.NotConfigured:
            self.state.spotify_queue = {"configured": False, "tracks": []}
            return
        self.state.spotify_queue = {"configured": True, "tracks": [t.to_dict() for t in tracks]}

    async def _sessions(self) -> None:
        # Copy the hook map for the executor thread; the hook route writes it from the loop.
        # Pruning keeps the dict object: the answer registry flags questions in it.
        for sid in set(self.state.hooks) - set(prune_hooks(self.state.hooks, time.time())):
            del self.state.hooks[sid]
        self.answers.expire()
        sessions, summary = await self._run(collect_sessions, self.settings, None, os_pid_alive, dict(self.state.hooks))
        self.state.sessions = [s.to_dict() for s in sessions]
        self.state.sessions_summary = summary
        # Attention alerts: the page flashes the card itself; the desktop notification is opt-in.
        alerts = attention_transitions(self._statuses, self.state.sessions)
        self._statuses = {s["id"]: s["status"] for s in self.state.sessions}
        if self.settings.alert_notify:
            for s in alerts:
                title = "Claude needs you" if s["status"] == ATTENTION else "Claude is waiting for you"
                await self._run(self.notifier, title, f"{s.get('name') or s['id']}: {s.get('detail') or ''}".rstrip(": "))

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

    async def _usage(self) -> float | None:
        """Poll the usage endpoint. Returns a backoff delay while rate limited."""
        now = datetime.now(timezone.utc)
        loop_time = asyncio.get_running_loop().time()
        if not self._usage_ok_at:
            self._usage_ok_at = loop_time
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
            usage["stale"] = True
            self.state.usage = usage
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 401:
                self._token = None  # force a re-read next round; Claude Code may have refreshed it
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                return self._usage_backoff(exc.response, loop_time)
            raise
        self._usage_ok_at = loop_time
        self._usage_rate_limited = 0
        windows = claude_usage.parse_usage_response(data, now)
        claude_usage.project_windows(windows, self._usage_samples, now)
        usage["windows"] = [w.to_dict() for w in windows]
        usage["source"] = "api"
        usage["stale"] = False
        usage["updated_at"] = now.isoformat()
        self.state.usage = usage
        return None

    def _usage_backoff(self, response: httpx.Response, loop_time: float) -> float:
        """The endpoint is shared with running Claude Code sessions and rate limits bursts.

        Keep the last value (already marked stale), double the poll interval per
        consecutive 429 up to ``USAGE_BACKOFF_MAX`` and honour a Retry-After that
        asks for longer. It only becomes an error after ``USAGE_STALE_AFTER``.
        """
        self._usage_rate_limited += 1
        delay = min(self.settings.usage_interval * 2**self._usage_rate_limited, USAGE_BACKOFF_MAX)
        try:
            delay = max(delay, float(response.headers.get("retry-after", 0)))
        except ValueError:
            pass
        if loop_time - self._usage_ok_at > USAGE_STALE_AFTER:
            raise RuntimeError(f"usage endpoint rate limited for {int((loop_time - self._usage_ok_at) // 60)} min")
        log.debug("usage endpoint rate limited, next poll in %.0f s", delay)
        return delay


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
    answers = Answers(state.hooks)
    collectors = Collectors(settings, state, runner, answers=answers)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_collectors:
            await collectors.start()
        try:
            yield
        finally:
            if start_collectors:
                await collectors.stop()

    app = FastAPI(title="edgeboard", lifespan=lifespan)
    allowed_hosts = LOOPBACK_HOSTS | ({settings.host.lower()} - WILDCARD_HOSTS)

    @app.middleware("http")
    async def origin_guard(request: Request, call_next):
        if request.url.path.startswith("/api/") and not request_allowed(request.headers.get("host", ""), request.headers.get("origin"), allowed_hosts):
            return JSONResponse({"detail": "host or origin not allowed"}, status_code=403)
        return await call_next(request)

    app.state.settings = settings
    app.state.dashboard = state
    app.state.answers = answers
    state.settings = {"alert_sound": settings.alert_sound, "presets": [{"label": label, "text": text} for label, text in settings.presets]}
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

    # Claude Code hooks (README: "Session state from hooks") post their stdin JSON
    # here. The body is stored as-is per session with a receipt time; the sessions
    # collector merges it into the cards and drops it after HOOK_TTL.
    @app.post("/api/hook")
    async def api_hook(request: Request):
        # JSON only: a ``text/plain`` body is what a cross-site page can send without a preflight.
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise HTTPException(status_code=415, detail="content-type must be application/json")
        try:
            body = json.loads(await request.body())
        except ValueError:
            raise HTTPException(status_code=400, detail="body must be JSON")
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body must be a JSON object")
        sid, event = body.get("session_id"), body.get("hook_event_name")
        if not isinstance(sid, str) or not sid or not isinstance(event, str) or not event:
            raise HTTPException(status_code=400, detail="session_id and hook_event_name are required")
        state.hooks[sid] = {**body, "ts": time.time()}
        if event == "PreToolUse" and body.get("tool_name") == "AskUserQuestion" and isinstance(body.get("tool_use_id"), str) and body["tool_use_id"]:
            answers.open(body["tool_use_id"], sid)
        return {"ok": True}

    # The hook script long-polls here for the panel's answer (scripts/edgeboard-hook.py).
    @app.get("/api/answer/{tool_use_id}")
    async def api_answer(tool_use_id: str, wait: float = 0.0):
        if answers.session_of(tool_use_id) is None:
            raise HTTPException(status_code=404, detail="no such pending question")
        result = await answers.wait(tool_use_id, min(max(wait, 0.0), ANSWER_WAIT_MAX))
        if result is None:
            return {"status": "pending"}
        if result.get("pass"):
            return {"status": "pass"}
        return {"status": "answered", "answers": result["answers"]}

    @app.post("/api/sessions/{session_id}/answer")
    async def api_session_answer(session_id: str, body: AnswerBody):
        result = {"pass": True} if body.pass_ else {"answers": body.answers}
        if settings.demo:
            return _demo_answer(state, session_id, body.tool_use_id)
        if answers.session_of(body.tool_use_id) != session_id:
            raise HTTPException(status_code=404, detail="no such pending question")
        if answers.is_abandoned(body.tool_use_id):
            raise HTTPException(status_code=409, detail="the hook stopped waiting; answer in the terminal")
        if not answers.resolve(body.tool_use_id, session_id, result):
            raise HTTPException(status_code=409, detail="already answered")
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/send")
    async def api_session_send(session_id: str, body: SendBody):
        if settings.demo:
            return _demo_send(state, session_id, body.text)
        loop = asyncio.get_running_loop()
        inbox = await loop.run_in_executor(None, find_inbox, settings.claude_dir, session_id)
        if inbox is None:
            raise HTTPException(status_code=404, detail="session has no inbox (gone, headless or Claude Code < 2.1.224)")
        try:
            await loop.run_in_executor(None, send_message, inbox, body.text)
        except OSError as exc:
            raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}")
        # Until the transcript shows the new turn, let the card say what just happened.
        state.hooks[session_id] = {"session_id": session_id, "hook_event_name": "UserPromptSubmit", "prompt": body.text, "ts": time.time()}
        return {"ok": True}

    async def _spotify_command(fn, *args) -> dict:
        """Run one playerctl command, then re-read metadata so the reply is current."""
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(None, fn, runner, settings.spotify_player, *args)
        await asyncio.sleep(0.25)  # let the player update before re-reading
        fresh = await loop.run_in_executor(None, spotify.read_spotify, runner, settings.spotify_player)
        # The polling loop may also write state.spotify; a stale overwrite heals on its next tick.
        state.spotify = fresh.to_dict()
        return {"ok": ok, "spotify": state.spotify}

    # Demo mode never touches a real player: these routes only mutate the canned state.
    @app.post("/api/spotify/seek")
    async def api_spotify_seek(body: SeekBody):
        length = float(state.spotify.get("length_s") or 0)
        if settings.demo:
            state.spotify["position_s"] = body.fraction * length
            return {"ok": True, "spotify": state.spotify}
        return await _spotify_command(spotify.seek, body.fraction * length)

    @app.post("/api/spotify/volume")
    async def api_spotify_volume(body: VolumeBody):
        if settings.demo:
            state.spotify["volume"] = body.volume
            return {"ok": True, "spotify": state.spotify}
        return await _spotify_command(spotify.set_volume, body.volume)

    @app.post("/api/spotify/skip")
    async def api_spotify_skip(body: SkipBody):
        tracks = state.spotify_queue.get("tracks") or []
        if settings.demo:
            if body.index < len(tracks):
                state.spotify = {**state.spotify, **tracks[body.index], "position_s": 0.0}
                state.spotify_queue = {**state.spotify_queue, "tracks": tracks[body.index + 1 :]}
            return {"ok": True, "spotify": state.spotify, "spotify_queue": state.spotify_queue}
        reply = await _spotify_command(spotify.skip, body.index + 1)
        # Drop the skipped rows now rather than at the next queue poll, so the
        # page and every snapshot in between agree with what the user just did.
        state.spotify_queue = {**state.spotify_queue, "tracks": tracks[body.index + 1 :]}
        collectors.refresh_queue_soon()
        return {**reply, "spotify_queue": state.spotify_queue}

    @app.post("/api/spotify/{action}")
    async def api_spotify(action: str):
        if action not in spotify.ACTIONS:
            raise HTTPException(status_code=404, detail="unknown action")
        if settings.demo:
            if action == "play_pause":
                state.spotify["status"] = "Paused" if state.spotify.get("status") == "Playing" else "Playing"
            return {"ok": True, "spotify": state.spotify}
        return await _spotify_command(spotify.control, action)

    return app


def _demo_session(state: State, session_id: str) -> dict:
    for s in state.sessions:
        if s.get("id") == session_id:
            return s
    raise HTTPException(status_code=404, detail="no such session")


def _demo_answer(state: State, session_id: str, tool_use_id: str) -> dict:
    s = _demo_session(state, session_id)
    if not s.get("question") or s["question"].get("tool_use_id") != tool_use_id:
        raise HTTPException(status_code=404, detail="no such pending question")
    s.update({"question": None, "status": WORKING, "detail": "thinking", "waiting_since": None})
    return {"ok": True}


def _demo_send(state: State, session_id: str, text: str) -> dict:
    s = _demo_session(state, session_id)
    if not s.get("can_send"):
        raise HTTPException(status_code=404, detail="session has no inbox")
    s.update({"status": WORKING, "detail": "working on your prompt", "last_prompt": text, "waiting_since": None})
    return {"ok": True}


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    log.info("edgeboard listening on http://%s:%d (demo=%s)", settings.host, settings.port, settings.demo)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="warning")
