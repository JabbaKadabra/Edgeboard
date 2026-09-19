"""Browser tests that script the snapshots the page receives.

The server runs with collectors off and a ``State`` the tests mutate directly;
the SSE loop pushes each change within a second, so a test can stage a status
transition, a deploy or a dropped connection and assert what the page does.
Skipped unless Playwright is importable; run with ``pytest -m browser``.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

import pytest

playwright = pytest.importorskip("playwright.sync_api")
expect = playwright.expect

from edgeboard.config import Settings  # noqa: E402
from edgeboard.demo import fill_demo  # noqa: E402
from edgeboard.server import create_app  # noqa: E402
from edgeboard.state import State  # noqa: E402
from tests.browsing import HEIGHT, WIDTH, TestServer, free_port, launch_chromium, panel_context  # noqa: E402

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
        self.opened: list[str] = []  # URLs the page asked the server to open in the desktop browser
        settings = Settings(host="127.0.0.1", port=free_port(), alert_sound=True)
        app = create_app(settings, self.state, start_collectors=False, opener=self._open)
        super().__init__(app, settings.port)

    def _open(self, url: str) -> bool:
        self.opened.append(url)
        return True

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


def test_cards_show_which_agent_a_session_belongs_to(dash, context):
    page = open_dash(context, dash)
    # the demo has a codex and an opencode card: each carries its agent in the figures grid
    badges = page.locator("#sessions .card .card-agent")
    expect(badges).to_have_count(4)
    assert [b.strip() for b in badges.all_text_contents()] == ["claude", "claude", "codex", "opencode build"]
    # a session without an agent (older snapshot) shows no badge at all
    session = dash.state.sessions[0]
    session.pop("agent")
    expect(card_of(page, session).locator(".card-agent")).to_be_hidden()
    session["agent"] = "claude"
    expect(card_of(page, session).locator(".card-agent")).to_have_text("claude")
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


def test_github_rows_follow_the_runs_and_a_new_failure_chimes(dash, context):
    page = open_dash(context, dash, init_script=FAKE_AUDIO)
    expect(page.locator("#github-runs .run")).to_have_count(2)
    expect(page.locator("#github-runs .run.running")).to_have_count(1)
    expect(page.locator("#github-runs .run.failed")).to_have_count(1)
    expect(page.locator("#github-summary")).to_have_text("1 running · 1 failed")
    # rows keep their DOM nodes across snapshots
    page.evaluate("document.querySelectorAll('#github-runs .run').forEach((el) => { el.dataset.mark = 'kept'; })")
    # the running run finishes red: its row turns failed and the page chimes
    running = next(r for r in dash.state.github["runs"] if r["status"] != "completed")
    running["status"], running["conclusion"] = "completed", "failure"
    running["updated_at"] = datetime.now(timezone.utc).isoformat()
    dash.state.github["running"], dash.state.github["failed"] = 0, 2
    expect(page.locator("#github-runs .run.failed")).to_have_count(2)
    expect(page.locator("#github-summary")).to_have_text("2 failed")
    assert page.evaluate("[...document.querySelectorAll('#github-runs .run')].every((el) => el.dataset.mark === 'kept')")
    assert page.evaluate("window.__chimes") == [1046, 1318]
    # a brand-new failed run chimes again
    now = datetime.now(timezone.utc).isoformat()
    dash.state.github["runs"] = [*dash.state.github["runs"], {
        "id": 9003, "repo": "JabbaKadabra/Edgeboard", "name": "ci", "title": "new", "branch": "main",
        "status": "completed", "conclusion": "failure", "url": "", "number": 18,
        "started_at": now, "updated_at": now,
    }]
    dash.state.github["failed"] = 3
    expect(page.locator("#github-runs .run")).to_have_count(3)
    assert page.evaluate("window.__chimes") == [1046, 1318, 1046, 1318]
    # clearing the list takes the rows away and shows the empty state
    dash.state.github["runs"] = []
    dash.state.github.update({"running": 0, "failed": 0})
    expect(page.locator("#github-runs .run")).to_have_count(0)
    expect(page.locator("#github-empty")).to_be_visible()
    expect(page.locator("#github-empty")).to_have_text("no running or failed runs")
    assert page.errors == []


def test_tapping_the_ci_pane_opens_run_details_that_link_to_github(dash, context):
    page = open_dash(context, dash)
    expect(page.locator("#ci-overlay")).to_be_hidden()
    page.locator("#github-panel").click()
    expect(page.locator("#ci-overlay")).to_be_visible()
    rows = page.locator("#ci-runs .ci-run")
    expect(rows).to_have_count(2)
    # the demo's running run: workflow, title, repo@branch, run number and elapsed state
    running = page.locator('#ci-runs .ci-run[data-id="9001"]')
    expect(running).to_have_class(re.compile(r"\brunning\b"))
    assert running.get_attribute("href") == "https://github.com/NordsteinSoftware/Proxytrace/actions/runs/9001"
    expect(running.locator(".ci-name")).to_have_text("E2E")
    expect(running.locator(".ci-title")).to_have_text("fix: clear the open bug backlog")
    expect(running.locator(".ci-sub")).to_contain_text("Proxytrace@bugfixes")
    expect(running.locator(".ci-sub")).to_contain_text("#412")
    expect(running.locator(".ci-when")).to_contain_text("running")
    failed = page.locator('#ci-runs .ci-run[data-id="9002"]')
    expect(failed).to_have_class(re.compile(r"\bfailed\b"))
    assert failed.get_attribute("href") == "https://github.com/JabbaKadabra/Edgeboard/actions/runs/9002"
    expect(failed.locator(".ci-when")).to_contain_text("failed")
    expect(failed.locator(".ci-open")).to_have_text("open ↗")
    # tapping a run asks the server to open it in the desktop browser and never
    # navigates the kiosk away; the row shows it was handed over, also after
    # the next snapshot repaints it
    expect(page).to_have_url(re.compile(r"127\.0\.0\.1"))
    failed.click()
    expect(failed).to_have_class(re.compile(r"\bopened\b"))
    assert dash.opened == ["https://github.com/JabbaKadabra/Edgeboard/actions/runs/9002"]
    dash.state.github["runs"][1]["updated_at"] = datetime.now(timezone.utc).isoformat()
    page.wait_for_timeout(1_200)  # a snapshot went by
    expect(failed).to_have_class(re.compile(r"\bopened\b"))
    expect(page.locator("#ci-overlay")).to_be_visible()
    expect(page).to_have_url(re.compile(r"127\.0\.0\.1"))  # still the dashboard
    # the overlay stays live: the running run ends red and its row updates in place
    page.evaluate("document.querySelectorAll('#ci-runs .ci-run').forEach((el) => { el.dataset.mark = 'kept'; })")
    running_run = next(r for r in dash.state.github["runs"] if r["status"] != "completed")
    running_run["status"], running_run["conclusion"] = "completed", "timed_out"
    running_run["updated_at"] = datetime.now(timezone.utc).isoformat()
    dash.state.github["running"], dash.state.github["failed"] = 0, 2
    expect(page.locator("#ci-runs .ci-run.failed")).to_have_count(2)
    expect(running.locator(".ci-when")).to_contain_text("timed out")
    assert page.evaluate("[...document.querySelectorAll('#ci-runs .ci-run')].every((el) => el.dataset.mark === 'kept')")
    # a run the API gave no url for is listed but cannot be opened
    now = datetime.now(timezone.utc).isoformat()
    dash.state.github["runs"] = [*dash.state.github["runs"], {
        "id": 9003, "repo": "JabbaKadabra/Edgeboard", "name": "ci", "title": "no url", "branch": "main",
        "status": "in_progress", "conclusion": "", "url": "", "number": 18,
        "started_at": now, "updated_at": now,
    }]
    dash.state.github["running"], dash.state.github["failed"] = 1, 2
    expect(rows).to_have_count(3)
    no_link = page.locator('#ci-runs .ci-run[data-id="9003"]')
    expect(no_link).to_have_js_property("tagName", "DIV")
    expect(no_link.locator(".ci-open")).to_have_text("no link")
    # clearing the list leaves the pane's empty text in the overlay
    dash.state.github["runs"] = []
    dash.state.github.update({"running": 0, "failed": 0})
    expect(rows).to_have_count(0)
    expect(page.locator("#ci-empty")).to_have_text("no running or failed runs")
    # a backdrop tap closes it
    page.locator("#ci-overlay").click(position={"x": 5, "y": 5})
    expect(page.locator("#ci-overlay")).to_be_hidden()
    assert page.errors == []


def test_the_ci_overlay_closes_on_its_own_after_twenty_seconds(dash, context):
    page = open_dash(context, dash, fake_clock=True)
    page.locator("#github-panel").click()
    expect(page.locator("#ci-overlay")).to_be_visible()
    page.clock.fast_forward(19_000)
    expect(page.locator("#ci-overlay")).to_be_visible()
    page.clock.fast_forward(1_500)
    expect(page.locator("#ci-overlay")).to_be_hidden()
    assert page.errors == []


def test_a_card_refits_its_reply_and_shows_mode_uptime_and_commits(dash, context):
    page = open_dash(context, dash)
    working = dash.first("working")
    card = card_of(page, working)
    reply = card.locator(".card-reply")
    expect(card.locator(".card-tasks")).to_be_visible()
    before = int(reply.evaluate("(el) => el.style.webkitLineClamp"))
    # the task row goes: the reply gets that line
    working["tasks"] = None
    expect(card.locator(".card-tasks")).to_be_hidden()
    expect(reply).to_have_js_property("style.webkitLineClamp", str(before + 1))
    # the reply never spills past the body
    assert reply.evaluate("(el) => el.getBoundingClientRect().bottom <= el.parentElement.getBoundingClientRect().bottom + 1")
    # figures follow the snapshot: the permission mode (nothing for the default), commits on a live card, the message count
    working["permission_mode"] = "bypassPermissions"
    working["commits"] = 3
    working["messages"] = 99
    expect(card.locator(".card-mode")).to_have_text("bypass")
    expect(card.locator(".card-commits")).to_have_text("3 commits")
    expect(card.locator(".card-msgs")).to_have_text("99 msgs")
    working["permission_mode"] = "default"
    expect(card.locator(".card-mode")).to_be_hidden()
    # uptime counts from started_at and keeps ticking
    expect(card.locator(".card-up")).to_have_text(re.compile(r"^up \d+m$"))
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


def test_the_overlay_transcript_follows_only_while_the_reader_is_at_the_end(dash, context):
    page = open_dash(context, dash)
    idle = dash.first("idle")
    idle["history"] = [{"role": "user" if i % 2 == 0 else "assistant", "text": f"message {i} " + "x" * 180} for i in range(20)]
    card_of(page, idle).locator(".card-title").click()
    box = page.locator("#ov-history")
    expect(box.locator(".ov-msg")).to_have_count(20)
    # opening lands on the newest message
    assert box.evaluate("(el) => el.scrollTop + el.clientHeight >= el.scrollHeight - 2")
    # scrolled back to the top, a new message must not yank the reader down
    box.evaluate("(el) => { el.scrollTop = 0; }")
    idle["history"] = idle["history"] + [{"role": "assistant", "text": "the newest message"}]
    expect(box.locator(".ov-msg")).to_have_count(21)
    assert box.evaluate("(el) => el.scrollTop") == 0
    # back at the end, it follows again
    box.evaluate("(el) => { el.scrollTop = el.scrollHeight; }")
    idle["history"] = idle["history"] + [{"role": "user", "text": "newest yet"}]
    expect(box.locator(".ov-msg")).to_have_count(22)
    assert box.evaluate("(el) => el.scrollTop + el.clientHeight >= el.scrollHeight - 2")
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


def test_the_clock_reads_like_a_watch_in_twelve_and_twenty_four_hour_locales(dash, browser):
    # hour:minute big, the seconds ticking beside them under the meridiem (12 h locales only),
    # the date on its own line with the weekday split from the rest
    for locale, hm, ampm, day, date in (("en-US", "5:07", "PM", "Wednesday", "Sep 30"), ("de-DE", "17:07", "", "Mittwoch", "30. Sep")):
        ctx = browser.new_context(viewport={"width": WIDTH, "height": HEIGHT}, locale=locale, timezone_id="Europe/Vienna")
        page = ctx.new_page()
        page.clock.install(time="2026-09-30T17:07:09+02:00")
        page.goto(dash.url + "/?kiosk=1")
        expect(page.locator("#clock-hm")).to_have_text(hm)
        expect(page.locator("#clock-ampm")).to_have_text(ampm)
        expect(page.locator("#clock-s")).to_have_text(re.compile(r"^(09|1\d)$"))
        expect(page.locator("#clock-day")).to_have_text(day)
        expect(page.locator("#clock-date")).to_have_text(re.compile("^" + re.escape(date)))
        ctx.close()


def test_the_cursor_hides_in_the_kiosk_unless_debugging(dash, context):
    page = open_dash(context, dash, query="?kiosk=1")
    assert page.evaluate("getComputedStyle(document.body).cursor") == "none"
    page = open_dash(context, dash, query="?kiosk=1&debug")
    assert page.evaluate("getComputedStyle(document.body).cursor") == "default"
    page = open_dash(context, dash, query="")
    assert page.evaluate("getComputedStyle(document.body).cursor") == "auto"
