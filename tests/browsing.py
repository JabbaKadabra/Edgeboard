"""Shared plumbing for the browser tests (``pytest -m browser``).

Not a test module: ``test_page.py`` (demo layout) and ``test_behaviour.py``
(scripted snapshots) import it after ``pytest.importorskip("playwright.sync_api")``.
"""

from __future__ import annotations

import os
import shutil
import socket
import threading
import time

import pytest

WIDTH, HEIGHT = 2560, 720  # the Xeneon Edge panel


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class TestServer:
    """``app`` under uvicorn on a daemon thread; ``stop()``/``start()`` cut and restore the connection."""

    __test__ = False  # not a test class

    def __init__(self, app, port: int):
        self.app, self.port = app, port
        self.url = f"http://127.0.0.1:{port}"
        self._server = None
        self._thread = None

    def start(self) -> None:
        import uvicorn

        # an open SSE stream would otherwise hold the shutdown for good
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error", timeout_graceful_shutdown=1)
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.time() + 10
        while not self._server.started:
            if time.time() > deadline:
                pytest.fail("test server did not start")
            time.sleep(0.05)

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.should_exit = True
        self._thread.join(timeout=5)
        self._server = self._thread = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()


def launch_chromium(playwright):
    """Playwright's own Chromium, else the system one (``EDGEBOARD_BROWSER`` or PATH)."""
    try:
        return playwright.chromium.launch()
    except playwright.Error:
        system = os.environ.get("EDGEBOARD_BROWSER") or shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
        if not system:
            pytest.skip("no Chromium: run `playwright install chromium`")
        return playwright.chromium.launch(executable_path=system)


def panel_context(browser):
    """A browser context at the panel's size; every page collects console and page errors into ``page.errors``."""
    context = browser.new_context(viewport={"width": WIDTH, "height": HEIGHT})

    def attach(page):
        page.errors = []
        page.on("console", lambda msg: page.errors.append(msg.text) if msg.type == "error" else None)
        page.on("pageerror", lambda exc: page.errors.append(str(exc)))

    context.on("page", attach)
    return context


def overflowing(page, width: int, height: int) -> list[str]:
    """Visible elements poking outside ``width``×``height`` (overflow:hidden containers clip their own children)."""
    return page.evaluate(
        """([W, H]) => {
          const out = [];
          for (const el of document.body.querySelectorAll('*')) {
            if (el.closest('[hidden]') || el.tagName === 'SCRIPT') continue;
            const r = el.getBoundingClientRect();
            if (!r.width || !r.height) continue;
            if (r.right > W + 1 || r.bottom > H + 1 || r.left < -1 || r.top < -1) {
              let p = el.parentElement, clipped = false;
              while (p && p !== document.body) {
                const o = getComputedStyle(p).overflow;
                if (o !== 'visible') { clipped = true; break; }
                p = p.parentElement;
              }
              if (!clipped) out.push(`${el.tagName.toLowerCase()}#${el.id}.${el.className} ${Math.round(r.right)}x${Math.round(r.bottom)}`);
            }
          }
          return out;
        }""",
        [width, height],
    )
