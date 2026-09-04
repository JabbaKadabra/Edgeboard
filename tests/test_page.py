"""Layout check of the demo page in a real browser at the panel's 2560x720.

Skipped unless Playwright is importable (``pip install -e ".[browser]"`` and
``playwright install chromium``); run it alone with ``pytest -m browser``.
The screenshot lands in ``tests/artifacts/`` (git-ignored) for eyeballing.
"""

from __future__ import annotations

import os
import shutil
import socket
import threading
import time
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")

from edgeboard.config import Settings  # noqa: E402
from edgeboard.server import create_app  # noqa: E402

pytestmark = pytest.mark.browser

WIDTH, HEIGHT = 2560, 720
ARTIFACTS = Path(__file__).parent / "artifacts"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def demo_url():
    import uvicorn

    port = _free_port()
    app = create_app(Settings(demo=True, host="127.0.0.1", port=port))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        if time.time() > deadline:
            pytest.fail("demo server did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/?kiosk=1"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except playwright.Error:
            system = os.environ.get("EDGEBOARD_BROWSER") or shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
            if not system:
                pytest.skip("no Chromium: run `playwright install chromium`")
            b = p.chromium.launch(executable_path=system)
        yield b
        b.close()


def test_demo_page_fits_the_panel(demo_url, browser):
    page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
    console_errors: list[str] = []
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: console_errors.append(str(exc)))
    page.goto(demo_url)
    # the first snapshot renders the four demo session cards (EDGEBOARD_SESSIONS_SHOWN default)
    page.wait_for_function("document.querySelectorAll('#sessions .card').length === 4", timeout=10_000)
    page.wait_for_function("document.querySelectorAll('#limits .limit').length === 2", timeout=10_000)
    page.wait_for_timeout(1500)  # a couple of SSE ticks, so re-renders had their chance to shift things

    ARTIFACTS.mkdir(exist_ok=True)
    page.screenshot(path=str(ARTIFACTS / f"demo-{WIDTH}x{HEIGHT}.png"))

    # nothing scrolls: the page is exactly one panel
    scroll = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.scrollHeight]")
    assert scroll == [WIDTH, HEIGHT], f"page scrolls: {scroll}"

    # all four columns are on screen, left to right, each with its own width
    cols = page.evaluate(
        "[...document.querySelectorAll('main.dash > .col')].map(c => { const r = c.getBoundingClientRect(); return [r.left, r.right, r.top, r.bottom]; })"
    )
    assert len(cols) == 4
    for left, right, top, bottom in cols:
        assert 0 <= left < right <= WIDTH and 0 <= top < bottom <= HEIGHT, cols
    assert all(cols[i][1] <= cols[i + 1][0] + 1 for i in range(3)), cols

    # no visible element pokes outside the viewport (overflow:hidden containers clip their own children)
    overflow = page.evaluate(
        """() => {
          const out = [];
          for (const el of document.body.querySelectorAll('*')) {
            if (el.closest('[hidden]') || el.tagName === 'SCRIPT') continue;
            const r = el.getBoundingClientRect();
            if (!r.width || !r.height) continue;
            if (r.right > %d + 1 || r.bottom > %d + 1 || r.left < -1 || r.top < -1) {
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
        }""" % (WIDTH, HEIGHT)
    )
    assert overflow == [], overflow

    assert page.text_content("#np-title") == "Midnight City"
    assert page.locator("#limits .limit-pace").count() == 2
    assert page.locator("#limits .limit-pace.warn").count() == 1
    assert page.locator("#disconnected").is_hidden()
    assert console_errors == []


# Runs before the answering test below: that one answers the demo question and sends a
# preset, after which no attention or idle card is left in the module-scoped demo state.
def test_demo_cards_fill_their_body_and_gauge_the_context(demo_url, browser):
    page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(demo_url)
    page.wait_for_function("document.querySelectorAll('#sessions .card').length === 4", timeout=10_000)
    # the idle card carries Claude's last reply; the attention card shows its question instead
    idle = page.locator("#sessions .card.idle").first
    assert idle.locator(".card-reply").text_content().startswith("Gap analysis")
    assert idle.locator(".card-reply").is_visible()
    asking = page.locator("#sessions .card.attention").first
    assert asking.locator(".card-reply").is_hidden()
    # a task list shows its progress and the task in hand
    tasks = page.locator("#sessions .card .card-tasks:visible")
    assert tasks.count() >= 1
    assert "/" in tasks.first.locator(".card-tasks-text").text_content()
    # the context gauge: a mini bar per card, red on the one near its window, compaction count where it happened
    assert page.locator("#sessions .card .card-ctx .bar-fill").count() == 4
    assert page.locator("#sessions .card .card-ctx .bar-fill.hot").count() == 1
    assert page.locator("#sessions .card .card-ctx .card-compact:visible").count() >= 1
    # the overlay spells the numbers out
    page.locator("#sessions .card.attention .card-title").first.click()
    assert page.locator("#overlay").is_visible()
    assert "/" in page.text_content("#ov-ctx") and "%" in page.text_content("#ov-ctx")
    assert page.locator("#ov-tasks").is_visible()
    assert errors == []


def test_demo_cards_offer_answers_and_presets(demo_url, browser):
    page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(demo_url)
    page.wait_for_function("document.querySelectorAll('#sessions .card').length === 4", timeout=10_000)
    asking = page.locator("#sessions .card.attention").first
    # the demo question has two parts, so the card shows the first one and defers the answering to the overlay
    assert asking.locator(".card-detail").text_content().startswith("Which environment")
    buttons = asking.locator(".card-actions button")
    assert [b.strip() for b in buttons.all_text_contents()] == ["answer…", "terminal"]
    # an idle session offers the presets from the snapshot settings
    idle = page.locator("#sessions .card.idle").first
    assert idle.locator(".card-actions button").count() >= 3
    assert "continue" in idle.locator(".card-actions button").first.text_content()
    # "answer…" opens the overlay with every question; pick one option each and send
    buttons.first.click()
    assert page.locator("#overlay").is_visible()
    assert page.locator("#ov-questions .ov-q").count() == 2
    for q in page.locator("#ov-questions .ov-q").all():
        q.locator("button").first.click()
    page.locator('#ov-questions button[data-act="answer-all"]').click()
    page.wait_for_function("document.querySelectorAll('#sessions .card.attention').length === 0", timeout=5_000)
    assert page.locator("#ov-questions").is_hidden()  # answered: the question block goes away, the overlay stays
    page.mouse.click(5, 5)  # backdrop closes it
    assert page.locator("#overlay").is_hidden()
    # a preset tap on the idle card sends without opening the overlay
    idle.locator(".card-actions button").first.click()
    page.wait_for_function("document.querySelectorAll('#sessions .card.idle').length === 0", timeout=5_000)
    assert page.locator("#overlay").is_hidden()
    # the overlay of a live session has the full preset list and a free-text input
    page.locator("#sessions .card .card-title").first.click()
    assert page.locator("#overlay").is_visible()
    assert page.locator("#ov-presets button").count() >= 3
    assert page.locator("#ov-input").is_visible()
    assert errors == []
