"""Layout check of the demo page in a real browser at the panel's 2560x720.

Skipped unless Playwright is importable (``pip install -e ".[browser]"`` and
``playwright install chromium``); run it alone with ``pytest -m browser``.
The screenshot lands in ``tests/artifacts/`` (git-ignored) for eyeballing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")

from edgeboard.config import Settings  # noqa: E402
from edgeboard.server import create_app  # noqa: E402
from tests.browsing import HEIGHT, WIDTH, TestServer, free_port, launch_chromium, overflowing, panel_context  # noqa: E402

pytestmark = pytest.mark.browser

ARTIFACTS = Path(__file__).parent / "artifacts"


@pytest.fixture(scope="module")
def demo_url():
    port = free_port()
    with TestServer(create_app(Settings(demo=True, host="127.0.0.1", port=port)), port) as server:
        yield server.url + "/?kiosk=1"


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as p:
        b = launch_chromium(p)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    context = panel_context(browser)
    yield context.new_page()
    context.close()


def test_demo_page_fits_the_panel(demo_url, page):
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

    # all three columns are on screen, left to right, each with its own width
    cols = page.evaluate(
        "[...document.querySelectorAll('main.dash > .col')].map(c => { const r = c.getBoundingClientRect(); return [r.left, r.right, r.top, r.bottom]; })"
    )
    assert len(cols) == 3
    for left, right, top, bottom in cols:
        assert 0 <= left < right <= WIDTH and 0 <= top < bottom <= HEIGHT, cols
    assert all(cols[i][1] <= cols[i + 1][0] + 1 for i in range(2)), cols
    # the four cards sit in one row
    tops = page.evaluate("[...document.querySelectorAll('#sessions .card')].map(c => Math.round(c.getBoundingClientRect().top))")
    assert len(set(tops)) == 1, tops
    # the widest clock and date readings still fit the rail, the seconds sitting on the hour:minute baseline
    fits = page.evaluate("""() => {
      const ids = ['clock-hm', 'clock-ampm', 'clock-s', 'clock-day', 'clock-date'];
      const keep = ids.map(id => document.getElementById(id).textContent);
      ['12:34', 'PM', '59', 'Wednesday', 'Sep 30'].forEach((v, i) => document.getElementById(ids[i]).textContent = v);
      const clock = document.querySelector('.clock'), date = document.querySelector('.dateline');
      const hm = document.getElementById('clock-hm').getBoundingClientRect(), s = document.getElementById('clock-s').getBoundingClientRect();
      const out = {
        clock: clock.scrollWidth <= clock.clientWidth + 1, date: date.scrollWidth <= date.clientWidth + 1,
        baseline: Math.abs(hm.bottom - s.bottom) <= 12, beside: s.left > hm.right,
      };
      keep.forEach((v, i) => document.getElementById(ids[i]).textContent = v);
      return out;
    }""")
    assert all(fits.values()), fits

    # no visible element pokes outside the viewport
    assert overflowing(page, WIDTH, HEIGHT) == []

    assert page.text_content("#np-title") == "Midnight City"
    assert page.locator("#limits .limit-pace").count() == 2
    assert page.locator("#limits .limit-pace.warn").count() == 1
    assert page.text_content("#t-msgs") == "279"
    # the activity row: a drawn burn curve, the system trace with its legend values, today's commits
    assert page.get_attribute("#burn-line", "d").startswith("M ")
    assert page.text_content("#legend-cpu") == "cpu 6%" and page.text_content("#legend-gpu") == "gpu 6%"
    assert "history 2m" in page.text_content("#sys-uptime")
    assert page.locator("#git-commits .commit:visible").count() >= 4
    assert page.text_content("#git-summary").startswith("9 commits")
    assert "CPU" in page.text_content("#sys-line") and "DISK" in page.text_content("#sys-line")
    assert page.locator("#disconnected").is_hidden()
    assert page.errors == []


def test_demo_spotify_keeps_the_next_track_on_screen(demo_url, page):
    page.goto(demo_url)
    page.wait_for_function("document.querySelectorAll('#queue li').length === 6", timeout=10_000)
    # the cover sits over the title; the transport row is low but still takes a finger
    art, title = page.locator("#spotify .art-wrap").bounding_box(), page.locator("#np-title").bounding_box()
    assert art["y"] + art["height"] <= title["y"], (art, title)
    for ctl in page.locator("#spotify .ctl").all():
        assert 40 <= ctl.bounding_box()["height"] <= 48, ctl.bounding_box()
    assert page.locator("#np-progress").bounding_box()["height"] >= 24

    def first_row_fits():
        queue, first = page.locator("#queue").bounding_box(), page.locator("#queue li").first.bounding_box()
        return first["y"] + first["height"] <= queue["y"] + queue["height"] + 1, (queue, first)

    # the next track is fully inside the pane, and stays so under a title that wraps to two lines
    assert page.locator("#queue li").first.is_visible() and "Reunion" in page.locator("#queue li").first.text_content()
    assert first_row_fits()[0], first_row_fits()[1]
    page.evaluate("document.getElementById('np-title').textContent = 'Wait (feat. a very long remix title that needs two lines)'")
    assert page.locator("#np-title").evaluate("(el) => el.getBoundingClientRect().height > parseFloat(getComputedStyle(el).lineHeight) * 1.5")
    assert first_row_fits()[0], first_row_fits()[1]
    assert page.errors == []


# Runs before the answering test below: that one answers the demo question and sends a
# preset, after which no attention or idle card is left in the module-scoped demo state.
def test_demo_cards_fill_their_body_and_gauge_the_context(demo_url, page):
    page.goto(demo_url)
    page.wait_for_function("document.querySelectorAll('#sessions .card').length === 4", timeout=10_000)
    # every card carries Claude's last reply; the attention card keeps it above its question
    idle = page.locator("#sessions .card.idle").first
    assert idle.locator(".card-reply").text_content().startswith("Gap analysis")
    assert idle.locator(".card-reply").is_visible()
    asking = page.locator("#sessions .card.attention").first
    assert asking.locator(".card-reply").is_visible()
    assert asking.locator(".card-detail").evaluate("(el) => el.getBoundingClientRect().height > parseFloat(getComputedStyle(el).lineHeight) * 1.5")  # the question wraps to a second line
    # a task list shows its progress and the task in hand
    tasks = page.locator("#sessions .card .card-tasks:visible")
    assert tasks.count() >= 1
    assert "/" in tasks.first.locator(".card-tasks-text").text_content()
    # the context gauge: a mini bar per card, red on the one near its window, compaction count where it happened
    assert page.locator("#sessions .card .card-ctx .bar-fill").count() == 4
    assert page.locator("#sessions .card .card-ctx .bar-fill.hot").count() == 1
    assert page.locator("#sessions .card .card-ctx .card-compact:visible").count() >= 1
    # your last prompt is on every card, one line, marked as yours
    assert page.locator("#sessions .card .card-prompt:visible").count() == 4
    assert page.evaluate("getComputedStyle(document.querySelector('#sessions .card .card-prompt'), '::before').content").startswith('"you')
    # the reply shows whole lines only and as many as the body has room for: at least three on a card without buttons
    fit = page.evaluate("""[...document.querySelectorAll('#sessions .card')].filter((c) => c.querySelector('.card-actions').hidden).map((c) => {
      const body = c.querySelector('.card-body').getBoundingClientRect(), reply = c.querySelector('.card-reply');
      const r = reply.getBoundingClientRect(), lh = parseFloat(getComputedStyle(reply).lineHeight);
      return { lines: Number(reply.style.webkitLineClamp), fits: r.bottom <= body.bottom + 1, whole: Math.abs(r.height / lh - Math.round(r.height / lh)) < .05 };
    })""")
    assert fit and all(f["fits"] and f["whole"] and f["lines"] >= 3 for f in fit), fit
    # the figures grid: model and mode, uptime and messages, the context gauge, agents and commits; every cell at the same x on every card
    cells = page.evaluate("[...document.querySelectorAll('#sessions .card')].map((c) => [...c.querySelectorAll('.card-meta .cell')].map((el) => el.getBoundingClientRect().left - c.getBoundingClientRect().left))")
    assert len(cells) == 4 and all(row == cells[0] for row in cells), cells
    texts = [c.strip() for c in page.locator("#sessions .card .card-meta").all_text_contents()]
    assert any("plan" in t for t in texts) and any("auto-edits" in t for t in texts)
    assert all("up " in t and "msgs" in t for t in texts)
    assert any("commits" in t for t in texts) and any("agents" in t for t in texts)
    # the overlay spells the numbers out
    page.locator("#sessions .card.attention .card-title").first.click()
    assert page.locator("#overlay").is_visible()
    assert "/" in page.text_content("#ov-ctx") and "%" in page.text_content("#ov-ctx")
    assert page.locator("#ov-tasks").is_visible()
    assert page.errors == []


def test_demo_cards_offer_answers_and_presets(demo_url, page):
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
    assert page.errors == []
