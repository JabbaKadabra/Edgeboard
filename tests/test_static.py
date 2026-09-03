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
