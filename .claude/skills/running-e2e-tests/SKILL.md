---
name: running-e2e-tests
description: Use when verifying a change to edgeboard's page (edgeboard/static), kiosk script or the snapshot fields the page reads, when a browser or kiosk test fails or is skipped, or before reporting UI work as done
---

# Running the end-to-end tests

Three suites drive the real page in Chromium. A UI change is verified when the right one ran in a browser and the report says so. Grepping `app.js` in `test_static.py` proves the code exists, not that it works.

| Suite | Proves | Run |
|---|---|---|
| `tests/test_behaviour.py` | what the page does when a snapshot changes (scripted `State`, SSE) | `.venv/bin/pytest tests/test_behaviour.py -k <name>` |
| `tests/test_page.py` | the demo page fits 2560×720 and renders every pane | `.venv/bin/pytest tests/test_page.py` |
| `tests/test_kiosk.py` | the live panel: build id, fit, stream, console, overlay tap | `EDGEBOARD_KIOSK_CDP=http://127.0.0.1:9222 .venv/bin/pytest -m kiosk` |

## Where the check goes

Decide by what is observable, not by which file is easiest:

- The change shows in the DOM, a class, `getComputedStyle`, a text or a timer → a case in `test_behaviour.py` (mutate `dash.state`, then `expect(...)`). This includes CSS values: `animationIterationCount`, `cursor`, colours.
- The change moves or sizes something → `test_page.py`.
- The change is only reachable on the panel (kiosk flags, touch, window size) → `test_kiosk.py`, invariants only, panel restored afterwards.
- Nothing observable (a comment, a unit file) → `test_static.py` is fine.

## Running

1. Run the affected file with `-k` while iterating (about 1 s per test); run `.venv/bin/pytest -m browser -rs` before reporting (about 25 s).
2. Read the summary line. `skipped` with a browser test in it means Playwright or Chromium is missing and nothing was verified; `-rs` shows the reason. Fix with `.venv/bin/pip install -e ".[browser]" && .venv/bin/playwright install chromium`.
3. Screenshots land in `tests/artifacts/`; look at them after a layout change.

## The kiosk suite

Check reachability, never assume it and never fake it:

```sh
curl -s -m 2 http://127.0.0.1:9222/json/version >/dev/null && echo cdp-up || echo cdp-down
systemctl --user is-active edgeboard.service edgeboard-kiosk.service
curl -s http://127.0.0.1:8765/api/state | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])'
.venv/bin/python -c 'from edgeboard.server import build_id; print(build_id())'
```

Run it only when the port answers AND the two build ids match: the panel shows what `edgeboard.service` serves, so an undeployed change passes the kiosk suite without being tested.

If the port does not answer, make it reachable yourself; that is part of running the suite:

1. `EDGEBOARD_KIOSK_DEBUG_PORT=9222` must be in `.env` (git-ignored). Add the line if it is missing; keep every other line as it is.
2. If the units are not installed (`Unit edgeboard.service not found`), install them as the README says: copy `systemd/*.service` to `~/.config/systemd/user/`, `systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XDG_CURRENT_DESKTOP`, `daemon-reload`, then `enable --now` both units.
3. If they are installed but inactive, `systemctl --user start edgeboard.service edgeboard-kiosk.service`. If the served build id is older than the local one, `systemctl --user restart edgeboard.service`; the kiosk page reloads itself.
4. Poll `http://127.0.0.1:9222/json/version` for up to 40 s before running.

Say in the report which of these you did. What stays the user's call: the monitor the kiosk window lands on (compositor rules, `EDGEBOARD_DISPLAY_OFFSET`) and any other `.env` key; a fit failure that reports the wrong window size is that placement, not a page bug.

## Reading a failure

- `expect(...)` timeouts print the actual value and every intermediate one; `page.errors` holds console and page errors. Assert on those before touching the test.
- `PWDEBUG=1 .venv/bin/pytest tests/test_behaviour.py -k <name>` runs headed with the inspector.
- A test that fails after a deliberate behaviour change is updated to the new behaviour, with the spec and CLAUDE.md, in the same change.

## Harness facts that bite

- `context.set_offline()` does not cut an open SSE stream to localhost; use `dash.stop()` / `dash.start()`. The cut stream logs `ERR_INCOMPLETE_CHUNKED_ENCODING` and the retries `ERR_CONNECTION_REFUSED`; nothing else is acceptable in `page.errors`.
- Fake clock: `page.clock.install()` before `goto`, `pause_at` after, then `fast_forward`. `run_for` fires the page's 1 s interval once per second of fake time and takes 20 s real time per 25 min.
- Under a fake clock use `expect`, which polls from Python; `wait_for_function` polls inside the page.
- Fixture order `(dash, context)` closes the browser context before the server stops, so teardown is clean.
- The kiosk tap is a mouse click over CDP; real touch needs a context created with touch enabled, which an attached session cannot change.

## Report shape

The report on a UI change has these lines, in this order:

1. Browser run: command, `N passed, M skipped`, and the skip reasons if M > 0.
2. Which test case covers the change (file and name), new or extended.
3. Kiosk run: the command and counts, plus what you did to bring the kiosk up (nothing, `.env`, installed units, started, restarted); or `skipped: <reason>` when it could not be made reachable.
4. Anything not verified, stated as such.
