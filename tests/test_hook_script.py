"""scripts/edgeboard-hook.py: forward hook events, wait for the panel's answer to AskUserQuestion."""

import importlib.util
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "edgeboard-hook.py"


def _load():
    spec = importlib.util.spec_from_file_location("edgeboard_hook", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ASK = {
    "session_id": "abc",
    "hook_event_name": "PreToolUse",
    "tool_name": "AskUserQuestion",
    "tool_use_id": "toolu_1",
    "tool_input": {"questions": [{"question": "Deploy where?", "options": [{"label": "staging"}, {"label": "prod"}]}]},
}


def test_decision_wraps_the_answers_into_updated_input():
    mod = _load()
    out = mod.decision(ASK, {"status": "answered", "answers": {"Deploy where?": "prod"}})
    assert out == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {**ASK["tool_input"], "answers": {"Deploy where?": "prod"}},
        }
    }
    assert mod.decision(ASK, {"status": "pass"}) is None
    assert mod.decision(ASK, {"status": "pending"}) is None
    assert mod.decision(ASK, {"status": "answered"}) is None  # no answers: leave it to the terminal


def test_wants_answer_only_for_ask_user_question():
    mod = _load()
    assert mod.wants_answer(ASK)
    assert not mod.wants_answer({**ASK, "tool_name": "Bash"})
    assert not mod.wants_answer({**ASK, "hook_event_name": "PostToolUse"})
    assert not mod.wants_answer({**ASK, "tool_use_id": ""})


class _Dashboard(BaseHTTPRequestHandler):
    hooks: list = []
    polls = 0

    def log_message(self, *a):  # quiet
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        _Dashboard.hooks.append(json.loads(body))
        self._reply({"ok": True})

    def do_GET(self):
        _Dashboard.polls += 1
        assert self.path.startswith("/api/answer/toolu_1?wait=")
        self._reply({"status": "pending"} if _Dashboard.polls == 1 else {"status": "answered", "answers": {"Deploy where?": "staging"}})

    def _reply(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _serve():
    server = HTTPServer(("127.0.0.1", 0), _Dashboard)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _run(body, *args, url):
    return subprocess.run([sys.executable, str(SCRIPT), "--url", url, *args], input=json.dumps(body), capture_output=True, text=True, timeout=20)


def test_script_forwards_the_event_and_prints_the_answer_when_it_arrives():
    server = _serve()
    _Dashboard.hooks, _Dashboard.polls = [], 0
    url = f"http://127.0.0.1:{server.server_port}"
    r = _run(ASK, "--wait", "10", "--poll", "0.2", url=url)
    server.shutdown()
    assert r.returncode == 0, r.stderr
    assert _Dashboard.hooks == [ASK] and _Dashboard.polls == 2
    assert json.loads(r.stdout)["hookSpecificOutput"]["updatedInput"]["answers"] == {"Deploy where?": "staging"}


def test_script_stays_silent_for_other_events_and_when_the_dashboard_is_down():
    server = _serve()
    _Dashboard.hooks, _Dashboard.polls = [], 0
    url = f"http://127.0.0.1:{server.server_port}"
    r = _run({"session_id": "abc", "hook_event_name": "Stop"}, url=url)
    server.shutdown()
    assert (r.returncode, r.stdout) == (0, "") and _Dashboard.hooks == [{"session_id": "abc", "hook_event_name": "Stop"}] and _Dashboard.polls == 0
    r = _run(ASK, "--wait", "1", url="http://127.0.0.1:1")  # nobody listens
    assert (r.returncode, r.stdout) == (0, "")
    r = subprocess.run([sys.executable, str(SCRIPT), "--url", url], input="not json", capture_output=True, text=True, timeout=20)
    assert (r.returncode, r.stdout) == (0, "")
