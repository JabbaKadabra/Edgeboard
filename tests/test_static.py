"""Guards for the vanilla frontend, which has no JS test harness."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "edgeboard" / "static"


def test_hidden_attribute_beats_author_display_rules():
    # `.art` sets display:block and `.art-placeholder` display:flex; author rules
    # override the UA `[hidden] { display: none }`, so the placeholder never
    # left the album cover. A global !important rule restores `hidden`.
    css = (STATIC / "style.css").read_text()
    assert re.search(r"^\[hidden\]\s*\{\s*display:\s*none\s*!important", css, re.M)


def test_cursor_hidden_only_for_kiosk():
    css = (STATIC / "style.css").read_text()
    body = re.search(r"^body\s*\{(.*?)\}", css, re.M | re.S).group(1)
    assert "cursor" not in body, "bare body must not hide the cursor for every viewer"
    assert re.search(r"^body\.kiosk\s*\{\s*cursor:\s*none", css, re.M)
    # debug must win over kiosk, so it has to come later in the sheet
    assert css.index("body.kiosk") < css.index("body.debug")

    js = (STATIC / "app.js").read_text()
    assert '"kiosk"' in js and '"debug"' in js

    kiosk = (ROOT / "scripts" / "kiosk.sh").read_text()
    assert "kiosk=1" in kiosk


def test_layout_has_queue_and_no_system_meters():
    html = (STATIC / "index.html").read_text()
    js = (STATIC / "app.js").read_text()
    # the system pane holds the one-line stats and the cpu/gpu trace only; no meters, no net trace, no status bar
    assert 'id="spark-cpu"' in html and 'id="sys-line"' in html
    assert 'id="spark-net"' not in html and 'class="meter"' not in html and "setBar(" not in js
    assert "tmbar" not in html and "chip" not in js
    # spotify pane renders the play queue from the snapshot's spotify_queue block
    assert 'id="queue"' in html and "spotify_queue" in js and "spotify_auth.py" in js


def test_spotify_seek_volume_and_tappable_scrollable_queue():
    html = (STATIC / "index.html").read_text()
    js = (STATIC / "app.js").read_text()
    css = (STATIC / "style.css").read_text()
    # seek: the progress bar is a touch target that posts the tapped fraction
    assert 'id="np-progress"' in html and 'postSpotify("seek"' in js
    assert re.search(r"^\.progress\s*\{[^}]*height:\s*(2[4-9]|[3-9]\d)px", css, re.M), "progress bar must be at least 24px tall"
    # volume slider bound to the snapshot's volume field
    assert 'id="np-volume"' in html and 'postSpotify("volume"' in js and "sp.volume" in js
    # the volume control is the same dashed bar as the track progress, only lower
    assert re.search(r'id="np-volume" class="progress progress-vol"', html)
    assert 'id="np-volume-fill" class="progress-fill"' in html
    vol_h = int(re.search(r"^\.progress-vol\s*\{[^}]*height:\s*(\d+)px", css, re.M).group(1))
    bar_h = int(re.search(r"^\.progress\s*\{[^}]*height:\s*(\d+)px", css, re.M).group(1))
    assert 0 < vol_h < bar_h
    # tapping a row drops it and the rows above it right away
    assert "spotify_queue" in js.split("$(\"queue\").addEventListener")[1].split("document.querySelectorAll(\".ctl\")")[0]
    # queue scrolls instead of trimming rows, and a tapped row skips to it
    assert re.search(r"^\.queue\s*\{[^}]*overflow-y:\s*auto", css, re.M)
    assert "getBoundingClientRect().bottom" not in js.split("function renderQueue")[1].split("function tickProgress")[0]
    assert 'postSpotify("skip"' in js and "data-index" in js


def test_mascot_is_pomodoro_tap_target_not_bouncing():
    html = (STATIC / "index.html").read_text()
    js = (STATIC / "app.js").read_text()
    css = (STATIC / "style.css").read_text()
    # the mascot no longer bobs while sessions work (it only changes pose on attention alerts, via drawClaude)
    assert "bounce" not in css
    sessions_js = js.split("function renderSessions")[1].split("function escapeHtml")[0]
    assert "mascot" not in sessions_js
    # tapping the mascot area runs a 25 min focus / 5 min break pomodoro shown above the clock
    assert 'id="mascot-wrap"' in html and 'id="pomo"' in html
    assert html.index('id="pomo"') < html.index('class="clock"')
    pomo_js = js.split("// ---------- pomodoro ----------")[1].split("// ----------")[0]
    assert "25 * 60" in pomo_js and "5 * 60" in pomo_js
    assert '$("mascot-wrap").addEventListener("click"' in pomo_js
    assert "COFFEE" in js  # the break phase draws a coffee cup instead of Claude
    # phase ends flash the mascot; the countdown is smaller than the clock
    assert re.search(r"^\.mascot\.flash\s*\{[^}]*animation:\s*flash", css, re.M)
    pomo_px = int(re.search(r"^\.pomo\s*\{[^}]*font-size:\s*(\d+)px", css, re.M).group(1))
    clock_px = int(re.search(r"^\.clock\s*\{[^}]*font-size:\s*(\d+)px", css, re.M).group(1))
    assert pomo_px < clock_px


def test_pomodoro_chimes_on_phase_change():
    js = (STATIC / "app.js").read_text()
    pomo_js = js.split("// ---------- pomodoro ----------")[1].split("// ----------")[0]
    # transitions play a synthesized WebAudio chime (no asset, the kiosk may be offline);
    # the context is unlocked on the first tap so the automatic transitions at zero can sound
    assert "AudioContext" in pomo_js and "resume()" in pomo_js
    assert "chime(" in pomo_js.split("function advancePomo")[1]
    # the kiosk restarts without any tap, so Chromium must not require a gesture for audio
    kiosk = (ROOT / "scripts" / "kiosk.sh").read_text()
    assert "--autoplay-policy=no-user-gesture-required" in kiosk


def test_session_cards_show_agents_attention_and_open_an_overlay():
    html = (STATIC / "index.html").read_text()
    js = (STATIC / "app.js").read_text()
    css = (STATIC / "style.css").read_text()
    sessions_js = js.split("// ---------- sessions ----------")[1].split("// ---------- spotify ----------")[0]
    # the card foot carries a subagent badge fed by the snapshot's agents / active_agents
    assert "card-agents" in sessions_js and "active_agents" in sessions_js
    # "attention" (permission prompt / question from a hook) gets its own colour and pulses
    assert re.search(r"^\.attention \.pill\s*\{", css, re.M) and re.search(r"^\.card\.attention\s*\{", css, re.M)
    # tapping a card opens a full-height overlay kept in index.html and filled from every snapshot
    assert 'id="overlay"' in html and 'id="ov-prompt"' in html and 'id="ov-cwd"' in html
    assert '$("sessions").addEventListener("click"' in sessions_js
    assert "last_prompt" in sessions_js and "renderOverlay(" in sessions_js.split("function renderSessions")[1]
    # dismissed by tapping the backdrop or after 20 s
    assert "20 * 1000" in sessions_js or "20000" in sessions_js
    assert '$("overlay").addEventListener("click"' in sessions_js


def test_server_unit_restarts_after_clean_exit():
    # A clean exit (stray pkill) must not leave the kiosk on "disconnected", and a
    # crash loop during a bad deploy must not park the unit for good.
    unit = (ROOT / "systemd" / "edgeboard.service").read_text()
    assert re.search(r"^Restart=always$", unit, re.M)
    assert re.search(r"^RestartSec=3$", unit, re.M)
    assert re.search(r"^StartLimitIntervalSec=0$", unit, re.M)
    assert "systemctl --user stop edgeboard" in (ROOT / "README.md").read_text()


def test_limits_show_pace_projection():
    js = (STATIC / "app.js").read_text()
    css = (STATIC / "style.css").read_text()
    usage_js = js.split("// ---------- usage ----------")[1].split("// ---------- sessions ----------")[0]
    # the server's projection (rate_per_hour / projected_full_at) becomes a third line under the bar:
    # amber "at this pace 100% at HH:MM" before the reset, "safe until reset" otherwise, nothing when flat
    assert "projected_full_at" in usage_js and "rate_per_hour" in usage_js
    assert "at this pace 100% at" in usage_js and "safe until reset" in usage_js
    assert re.search(r"^\.limit-pace\.warn\s*\{[^}]*color:\s*var\(--accent-2\)", css, re.M)


def test_attention_alerts_flash_card_pose_mascot_and_chime():
    js = (STATIC / "app.js").read_text()
    css = (STATIC / "style.css").read_text()
    sessions_js = js.split("// ---------- sessions ----------")[1].split("// ---------- spotify ----------")[0]
    # transitions are detected per session id against the previous snapshot: working -> idle and -> attention
    assert "prevStatus" in sessions_js and '"attention"' in sessions_js and '"working"' in sessions_js
    # the card flashes, then stays highlighted until its status changes again
    assert re.search(r"^\.card\.alert\s*\{[^}]*animation:\s*card-flash", css, re.M)
    assert "@keyframes card-flash" in css
    # the mascot takes the arms-up attention pose (pink) while any card is alerting
    assert "ALERT" in js and re.search(r"^\.mascot\.attention rect\s*\{[^}]*var\(--pink\)", css, re.M)
    # the chime is opt-in via the snapshot's settings.alert_sound (EDGEBOARD_ALERT_SOUND)
    assert "alert_sound" in sessions_js and 'chime("alert")' in sessions_js
    assert "alert:" in js.split("const CHIMES")[1].split("};")[0]


def test_session_cards_answer_questions_and_send_presets():
    html = (STATIC / "index.html").read_text()
    js = (STATIC / "app.js").read_text()
    css = (STATIC / "style.css").read_text()
    sessions_js = js.split("// ---------- sessions ----------")[1].split("// ---------- spotify ----------")[0]
    # the card grows an action row: option buttons for a pending AskUserQuestion, presets for an idle session
    assert "card-actions" in sessions_js and "question" in sessions_js and "can_send" in sessions_js and "presets" in sessions_js
    # buttons post to the session routes and must not open the overlay
    assert "/api/sessions/" in sessions_js and "stopPropagation" in sessions_js
    assert '"answer"' in sessions_js and '"send"' in sessions_js and '"pass"' in sessions_js or "pass: true" in sessions_js
    # the overlay carries the full question set, the presets, a free-text input and what Claude last said
    for element in ('id="ov-questions"', 'id="ov-presets"', 'id="ov-input"', 'id="ov-send"', 'id="ov-reply"', 'id="ov-mode"', 'id="ov-waiting"'):
        assert element in html, element
    assert "last_reply" in sessions_js and "permission_mode" in sessions_js and "waiting_since" in sessions_js
    # finger-sized buttons that never overflow the card
    assert re.search(r"^\.card-actions\s*\{[^}]*overflow:\s*hidden", css, re.M)
    m = re.search(r"^\.card-actions button\s*\{[^}]*min-height:\s*(\d+)px", css, re.M)
    assert m and int(m.group(1)) >= 44
    # the 20 s overlay timer restarts on a tap inside it
    assert "20 * 1000" in sessions_js


def test_page_reloads_itself_when_the_server_build_changes():
    # After a deploy the kiosk would keep running the cached app.js against a new
    # API. The snapshot carries the build id; a change reloads the page, and the
    # asset links carry the build so the reload cannot come back from the cache.
    html = (STATIC / "index.html").read_text()
    js = (STATIC / "app.js").read_text()
    assert 'src="/static/app.js?v=__BUILD__"' in html and 'href="/static/style.css?v=__BUILD__"' in html
    root_js = js.split("// ---------- render root ----------")[1].split("// ---------- transport ----------")[0]
    assert "snap.version" in root_js and "location.reload()" in root_js
