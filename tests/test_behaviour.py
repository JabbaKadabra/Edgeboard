"""Browser tests that script the snapshots the page receives.

The server runs with collectors off and a ``State`` the tests mutate directly;
the SSE loop pushes each change within a second, so a test can stage a status
transition, a deploy or a dropped connection and assert what the page does.
Skipped unless Playwright is importable; run with ``pytest -m browser``.
"""

from __future__ import annotations

import re
import time

import pytest

playwright = pytest.importorskip("playwright.sync_api")
expect = playwright.expect

from edgeboard.config import Settings  # noqa: E402
from edgeboard.demo import fill_demo  # noqa: E402
from edgeboard.server import create_app  # noqa: E402
from edgeboard.state import State  # noqa: E402
from tests.browsing import TestServer, free_port, launch_chromium, panel_context  # noqa: E402

pytestmark = pytest.mark.browser

# WebAudio cannot be heard in a test: this stand-in records every oscillator the page starts.
FAKE_AUDIO = """
window.__chimes = [];
class FakeNode { connect(n) { return n; } }
class FakeOsc extends FakeNode {
  constructor() { super(); this.frequency = { value: 0 }; }
  start() { window.__chimes.push(this.frequency.value); }
  stop() {}
}
class FakeGain extends FakeNode {
  constructor() { super(); this.gain = { setValueAtTime() {}, linearRampToValueAtTime() {}, exponentialRampToValueAtTime() {} }; }
}
window.AudioContext = class {
  constructor() { this.currentTime = 0; this.state = "running"; this.destination = new FakeNode(); }
  createOscillator() { return new FakeOsc(); }
  createGain() { return new FakeGain(); }
  resume() { return Promise.resolve(); }
};
"""


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as p:
        b = launch_chromium(p)
        yield b
        b.close()


class Dash(TestServer):
    """One server with a scripted ``State``: a test mutates it and the SSE loop pushes the change."""

    def __init__(self):
        self.state = State()
        fill_demo(self.state)
        settings = Settings(host="127.0.0.1", port=free_port(), alert_sound=True)
        super().__init__(create_app(settings, self.state, start_collectors=False), settings.port)

    def first(self, status: str) -> dict:
        return next(s for s in self.state.sessions if s["status"] == status)


@pytest.fixture
def dash():
    with Dash() as d:
        yield d


@pytest.fixture
def context(browser):
    ctx = panel_context(browser)
    yield ctx
    ctx.close()


CARDS_UP = "document.querySelectorAll('#sessions .card').length === 4"


def open_dash(context, dash, query="?kiosk=1", init_script=None, fake_clock=False):
    page = context.new_page()
    if init_script:
        page.add_init_script(init_script)
    if fake_clock:
        page.clock.install()
    page.goto(dash.url + "/" + query)
    expect(page.locator("#sessions .card")).to_have_count(4, timeout=10_000)
    if fake_clock:  # stop real time from creeping in: only ``page.clock.run_for`` moves the page's clock from here
        page.clock.pause_at(int(time.time() * 1000) + 1_000)
    return page


def card_of(page, session):
    return page.locator(f'#sessions .card[data-id="{session["id"]}"]')


def test_a_session_finishing_flashes_its_card_raises_the_mascot_and_chimes(dash, context):
    page = open_dash(context, dash, init_script=FAKE_AUDIO)
    working = dash.first("working")
    card = card_of(page, working)
    expect(card).not_to_have_class(re.compile(r"\balert\b"))
    expect(page.locator("#mascot")).not_to_have_class(re.compile(r"\battention\b"))

    working["status"] = "idle"
    working["detail"] = "finished"
    expect(card).to_have_class(re.compile(r"\balert\b"))
    expect(page.locator("#mascot")).to_have_class(re.compile(r"\battention\b"))
    assert page.evaluate("window.__chimes") == [1046, 1318]

    # the highlight goes when the status changes again; the mascot drops its arms
    working["status"] = "working"
    expect(card).not_to_have_class(re.compile(r"\balert\b"))
    expect(page.locator("#mascot")).not_to_have_class(re.compile(r"\battention\b"))
    assert page.errors == []


def test_a_session_turning_to_attention_alerts_but_a_first_sighting_does_not(dash, context):
    page = open_dash(context, dash, init_script=FAKE_AUDIO)
    # the demo's attention card was already asking when the page loaded: no alert, no chime
    expect(page.locator("#sessions .card.attention")).to_have_count(1)
    expect(page.locator("#sessions .card.alert")).to_have_count(0)
    working = dash.first("working")
    working["status"] = "attention"
    working["detail"] = "permission prompt"
    expect(card_of(page, working)).to_have_class(re.compile(r"\balert\b"))
    assert page.evaluate("window.__chimes") == [1046, 1318]
    assert page.errors == []


def test_cards_limits_and_commit_rows_update_in_place(dash, context):
    page = open_dash(context, dash)
    page.evaluate("""() => {
      document.querySelectorAll('#sessions .card, #limits .limit, #git-commits .commit').forEach((el) => { el.dataset.mark = 'kept'; });
    }""")
    working = dash.first("working")
    working["detail"] = "editing server.py"
    dash.state.usage["windows"][0]["utilization"] = 42
    dash.state.git["commits"][0]["message"] = "fix: session card flash restarts (again)"
    expect(card_of(page, working).locator(".card-detail")).to_have_text("editing server.py")
    expect(page.locator("#limits .limit").first.locator(".limit-pct")).to_have_text("42%")
    expect(page.locator("#git-commits .commit").first.locator(".c-msg")).to_have_text("fix: session card flash restarts (again)")
    kept = page.evaluate("[...document.querySelectorAll('#sessions .card, #limits .limit')].map((el) => el.dataset.mark)")
    assert kept == ["kept"] * 6, kept
    # a changed commit list is the one thing rebuilt; an unchanged one keeps its rows across ticks
    page.evaluate("document.querySelectorAll('#git-commits .commit').forEach((el) => { el.dataset.mark = 'kept'; })")
    page.wait_for_timeout(2_200)
    assert page.evaluate("[...document.querySelectorAll('#git-commits .commit')].every((el) => el.dataset.mark === 'kept')")
    assert page.errors == []


def test_the_page_reloads_itself_when_the_server_build_changes(dash, context):
    page = open_dash(context, dash)
    assert page.evaluate("performance.getEntriesByType('navigation')[0].type") == "navigate"
    with page.expect_event("load", timeout=5_000):
        dash.state.build = dash.state.build + ".deployed"
    assert page.evaluate("performance.getEntriesByType('navigation')[0].type") == "reload"
    expect(page.locator("#sessions .card")).to_have_count(4)
    assert page.errors == []


def test_a_dropped_stream_shows_disconnected_until_it_is_back(dash, context):
    page = open_dash(context, dash)
    expect(page.locator("#disconnected")).to_be_hidden()
    dash.stop()
    expect(page.locator("#disconnected")).to_be_visible(timeout=8_000)
    dash.start()
    expect(page.locator("#disconnected")).to_be_hidden(timeout=8_000)
    # the stream reconnected: a fresh change still reaches the page
    working = dash.first("working")
    working["detail"] = "back again"
    expect(card_of(page, working).locator(".card-detail")).to_have_text("back again")
    # the cut stream and the retries while the server was down are the only errors the console saw
    assert all("ERR_INCOMPLETE_CHUNKED_ENCODING" in e or "ERR_CONNECTION_REFUSED" in e for e in page.errors), page.errors


def test_the_overlay_closes_on_its_own_after_twenty_seconds(dash, context):
    page = open_dash(context, dash, fake_clock=True)
    page.locator("#sessions .card .card-title").first.click()
    expect(page.locator("#overlay")).to_be_visible()
    page.clock.fast_forward(19_000)
    expect(page.locator("#overlay")).to_be_visible()
    page.clock.fast_forward(1_500)
    expect(page.locator("#overlay")).to_be_hidden()
    assert page.errors == []


def test_tapping_the_mascot_runs_one_pomodoro_loop(dash, context):
    page = open_dash(context, dash, init_script=FAKE_AUDIO, fake_clock=True)
    pomo, mascot = page.locator("#pomo"), page.locator("#mascot")
    expect(pomo).to_be_hidden()
    page.locator("#mascot-wrap").click()
    expect(pomo).to_be_visible()
    expect(page.locator("#pomo-label")).to_have_text("focus")
    expect(page.locator("#pomo-time")).to_have_text("25:00")
    page.clock.fast_forward(60_000)
    expect(page.locator("#pomo-time")).to_have_text("24:00")
    # the focus phase runs out: the break starts by itself with the coffee cup and its two-note chime
    page.clock.fast_forward(24 * 60_000)
    expect(page.locator("#pomo-label")).to_have_text("break")
    expect(pomo).to_have_class(re.compile(r"\bbreak\b"))
    expect(page.locator("#pomo-time")).to_have_text("5:00")
    assert page.evaluate("window.__chimes") == [880, 660, 990]
    page.clock.fast_forward(5 * 60_000 + 1_000)
    expect(pomo).to_be_hidden()
    expect(mascot).not_to_have_class(re.compile(r"\bbreak\b"))
    assert page.evaluate("window.__chimes") == [880, 660, 990, 990, 784, 523]
    assert page.errors == []


def test_the_cursor_hides_in_the_kiosk_unless_debugging(dash, context):
    page = open_dash(context, dash, query="?kiosk=1")
    assert page.evaluate("getComputedStyle(document.body).cursor") == "none"
    page = open_dash(context, dash, query="?kiosk=1&debug")
    assert page.evaluate("getComputedStyle(document.body).cursor") == "default"
    page = open_dash(context, dash, query="")
    assert page.evaluate("getComputedStyle(document.body).cursor") == "auto"
