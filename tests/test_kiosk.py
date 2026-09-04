"""End-to-end checks against the live kiosk on the panel.

Opt-in: ``scripts/kiosk.sh`` opens Chromium's DevTools port when
``EDGEBOARD_KIOSK_DEBUG_PORT`` is set, and these tests attach to it when
``EDGEBOARD_KIOSK_CDP`` names it (``EDGEBOARD_KIOSK_CDP=http://127.0.0.1:9222
pytest -m kiosk``). They drive the real page with real data, so they only
check invariants (build, fit, stream, console, overlay tap) and leave the
panel as they found it.
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")
expect = playwright.expect

from tests.browsing import overflowing  # noqa: E402

pytestmark = [pytest.mark.browser, pytest.mark.kiosk]

CDP = os.environ.get("EDGEBOARD_KIOSK_CDP", "")
ARTIFACTS = Path(__file__).parent / "artifacts"


@pytest.fixture(scope="module")
def kiosk():
    if not CDP:
        pytest.skip("set EDGEBOARD_KIOSK_CDP=http://127.0.0.1:9222 (and EDGEBOARD_KIOSK_DEBUG_PORT for kiosk.sh)")
    try:
        urllib.request.urlopen(CDP.rstrip("/") + "/json/version", timeout=2).read()
    except OSError as exc:
        pytest.skip(f"no DevTools port at {CDP}: {exc}")
    with playwright.sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP)
        pages = [pg for ctx in browser.contexts for pg in ctx.pages if "kiosk=1" in pg.url]
        if not pages:
            pytest.skip("no kiosk page (?kiosk=1) open in that browser")
        page = pages[0]
        page.errors = []
        page.on("console", lambda msg: page.errors.append(msg.text) if msg.type == "error" else None)
        page.on("pageerror", lambda exc: page.errors.append(str(exc)))
        yield page
        if page.locator("#overlay").is_visible():
            page.mouse.click(5, 5)
        browser.close()  # attached over CDP: this disconnects, the kiosk keeps running


def server_state(page) -> dict:
    origin = page.evaluate("location.origin")
    with urllib.request.urlopen(origin + "/api/state", timeout=5) as resp:
        return json.load(resp)


def test_kiosk_runs_the_servers_current_build(kiosk):
    # the asset links carry the build id the page was served with; a stale page would already have reloaded
    src = kiosk.evaluate("document.querySelector('script[src*=\"?v=\"]').getAttribute('src')")
    assert src.split("?v=")[1] == server_state(kiosk)["version"]


def test_kiosk_fills_the_panel_with_nothing_overflowing(kiosk):
    width, height = kiosk.evaluate("[window.innerWidth, window.innerHeight]")
    want = tuple(int(v) for v in os.environ.get("EDGEBOARD_DISPLAY_SIZE", "2560,720").split(","))
    assert (width, height) == want, f"kiosk window is {width}x{height}"
    ARTIFACTS.mkdir(exist_ok=True)
    kiosk.screenshot(path=str(ARTIFACTS / f"kiosk-{width}x{height}.png"))
    scroll = kiosk.evaluate("[document.documentElement.scrollWidth, document.documentElement.scrollHeight]")
    assert scroll == [width, height], f"page scrolls: {scroll}"
    assert overflowing(kiosk, width, height) == []


def test_kiosk_receives_snapshots_with_a_clean_console(kiosk):
    expect(kiosk.locator("#disconnected")).to_be_hidden()
    before = kiosk.text_content("#clock-s")
    expect(kiosk.locator("#clock-s")).not_to_have_text(before, timeout=3_000)  # the page is ticking
    # a live snapshot renders the summary line the server is counting
    summary = server_state(kiosk)["sessions_summary"]
    expect(kiosk.locator("#sessions-summary")).to_contain_text(f"{summary.get('today', 0)} today")
    kiosk.wait_for_timeout(2_000)  # a couple of SSE renders while we listen
    assert kiosk.errors == []


def test_kiosk_tap_opens_and_closes_the_detail_overlay(kiosk):
    cards = kiosk.locator("#sessions .card")
    if cards.count() == 0:
        pytest.skip("no session card on the panel to tap")
    overlay = kiosk.locator("#overlay")
    expect(overlay).to_be_hidden()
    cards.first.locator(".card-title").click()
    expect(overlay).to_be_visible()
    expect(kiosk.locator("#ov-pill")).to_have_text(cards.first.locator(".pill").text_content().strip())
    kiosk.mouse.click(5, 5)  # backdrop
    expect(overlay).to_be_hidden()
    assert kiosk.errors == []
