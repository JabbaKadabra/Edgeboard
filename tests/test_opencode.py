"""OpenCode service discovery, session mapping and answer translation (fake transport, no service)."""

import json
from datetime import datetime, timezone

import httpx

from edgeboard.collectors import opencode
from edgeboard.collectors.claude_transcripts import HISTORY_MAX
from edgeboard.collectors.opencode import (
    Pending,
    PendingStore,
    Service,
    clean_tool_hint,
    collect_sessions,
    form_answer,
    form_questions,
    permission_question,
    read_service,
    tool_line,
)
from edgeboard.config import Settings

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
SERVICE = Service("http://127.0.0.1:49374", "secret")


def _session(sid="ses_1", **extra) -> dict:
    return {
        "id": sid,
        "projectID": "p1",
        "agent": "build",
        "model": {"id": "deepseek-v4.1-flash", "providerID": "opencode-go", "variant": "max"},
        "cost": 0.1,
        "tokens": {"input": 100, "output": 10, "reasoning": 1, "cache": {"read": 20, "write": 0}},
        "time": {"created": 1789800000000, "updated": 1789800100000},
        "title": "Fix the collector",
        "location": {"directory": "/home/me/proj"},
        **extra,
    }


def _assistant(text=None, tool=None, tokens=None) -> dict:
    content = []
    if text is not None:
        content.append({"type": "text", "text": text})
    if tool is not None:
        content.append({"type": "tool", "id": "call_1", "name": tool[0], "state": {"status": tool[2], "input": tool[1]}})
    message = {"id": "msg_1", "type": "assistant", "agent": "build", "model": {"id": "deepseek-v4.1-flash"}, "content": content, "time": {"created": 1789800100000}}
    if tokens is not None:
        message["tokens"] = tokens
    return message


def test_read_service_requires_url_and_password(tmp_path):
    path = tmp_path / "service.json"
    assert read_service(path) is None
    path.write_text(json.dumps({"url": "http://127.0.0.1:49374", "password": "pw", "pid": 1}))
    assert read_service(path) == Service("http://127.0.0.1:49374", "pw")
    path.write_text(json.dumps({"url": "http://127.0.0.1:49374"}))
    assert read_service(path) is None
    path.write_text("not json")
    assert read_service(path) is None


def test_tool_hints_cover_opencode_tool_names():
    assert tool_line("shell", {"command": "dotnet test -q"}) == "running dotnet test -q"
    assert tool_line("edit", {"filePath": "/home/me/proj/server.py"}) == "editing server.py"
    assert tool_line("read", {"filePath": "/home/me/proj/a/b.py"}) == "reading b.py"
    assert clean_tool_hint("read", {"path": "/x/y.py"}) == "y.py"
    assert tool_line("unknown-tool", {}) == "running unknown-tool"


def test_classify_follows_permissions_forms_and_tools():
    now = datetime.now(timezone.utc)
    recent_ms = int(now.timestamp() * 1000) - 60_000
    session = _session()
    session["time"] = {"created": recent_ms - 600_000, "updated": recent_ms}
    idle_message = [{"type": "idle", "time": {"created": recent_ms}}]
    user = [{"type": "user", "text": "do it", "time": {"created": recent_ms}}]
    running = [_assistant(tool=("shell", {"command": "pytest -q"}, "running"))]
    assert opencode.classify(session, True, {"id": "per_1"}, None, running, now) == ("attention", "needs permission")
    assert opencode.classify(session, True, None, {"id": "frm_1"}, running, now) == ("attention", "asking you a question")
    assert opencode.classify(session, True, None, None, running, now) == ("working", "running pytest -q")
    assert opencode.classify(session, True, None, None, user, now) == ("working", "working on your prompt")
    assert opencode.classify(session, True, None, None, [_assistant(text="done")], now) == ("working", "thinking")
    assert opencode.classify(session, True, None, None, idle_message, now) == ("idle", "waiting for you")
    assert opencode.classify(session, False, None, None, idle_message, now) == ("idle", "waiting for you")
    stale = {**session, "time": {"created": recent_ms - 7_200_000, "updated": recent_ms - 7_200_000}}
    assert opencode.classify(stale, False, None, None, [{"type": "idle", "time": {"created": recent_ms - 7_200_000}}], now) == ("done", "finished")


def test_permission_and_form_questions():
    question = permission_question({"id": "per_1", "action": "shell", "resources": ["git push origin main"], "message": ""})
    assert question["tool_use_id"] == "per_1" and question["questions"][0]["options"] == ["Allow once", "Always allow", "Deny"]
    assert "git push origin main" in question["questions"][0]["question"]

    form = {
        "id": "frm_1",
        "title": "Rollout",
        "fields": [
            {"key": "env", "type": "string", "title": "Target", "description": "Deploy where?", "options": [{"value": "staging", "label": "staging"}, {"value": "prod", "label": "prod"}]},
            {"key": "dry", "type": "boolean", "title": "Dry run"},
            {"key": "n", "type": "integer", "title": "Replicas"},
        ],
    }
    flattened = form_questions(form)
    assert flattened["tool_use_id"] == "frm_1" and len(flattened["questions"]) == 3
    assert flattened["questions"][0]["options"] == ["staging", "prod"]
    assert flattened["questions"][1]["options"] == ["Yes", "No"] and flattened["questions"][1]["multi"] is False
    assert flattened["questions"][2]["options"] == []


def test_form_answer_translates_labels_to_values():
    entry = Pending("ses_1", "form", "frm_1", {"Deploy where?": {"key": "env", "type": "string", "options": {"staging": "staging", "prod": "prod"}}, "Replicas": {"key": "n", "type": "integer", "options": {}}, "Dry run": {"key": "dry", "type": "boolean", "options": {"Yes": True, "No": False}}})
    assert form_answer(entry, {"Deploy where?": "prod", "Replicas": "3", "Dry run": "No"}) == {"env": "prod", "n": 3, "dry": False}
    assert form_answer(entry, {"Deploy where?": "prod"}) is None  # a missing answer is refused
    multi = Pending("ses_1", "form", "frm_2", {"Which ones?": {"key": "tags", "type": "multiselect", "options": {"a": "a", "b": "b"}}})
    assert form_answer(multi, {"Which ones?": "a, b"}) == {"tags": ["a", "b"]}


def test_pending_store_expires_and_forgets():
    store = PendingStore()
    store.remember("per_1", Pending("ses_1", "permission", "per_1"))
    store.remember("per_2", Pending("ses_2", "permission", "per_2"))
    assert store.get("per_1") is not None
    store.forget_session("ses_1")
    assert store.get("per_1") is None and store.get("per_2") is not None
    store.expire(now=store.get("per_2").seen_at + 99999)
    assert store.get("per_2") is None


def test_session_history_keeps_the_conversation_in_order():
    messages = [
        _assistant(text="newest answer"),
        {"type": "user", "text": "newest prompt"},
        _assistant(text="older answer"),
        _assistant(tool=("shell", {"command": "ls"}, "completed")),  # a tool-only step carries no text
        {"type": "user", "text": "older prompt"},
        {"type": "idle"},
    ]
    assert opencode._session_history(messages) == [
        {"role": "user", "text": "older prompt"},
        {"role": "assistant", "text": "older answer"},
        {"role": "user", "text": "newest prompt"},
        {"role": "assistant", "text": "newest answer"},
    ]
    # the API lists messages newest first; only the newest HISTORY_MAX survive, oldest first
    long = [{"type": "user", "text": f"p{i}"} for i in range(20)]
    history = opencode._session_history(long)
    assert len(history) == HISTORY_MAX and history[0]["text"] == "p9" and history[-1]["text"] == "p0"


def _fake_request(routes: dict):
    def request(method: str, path: str, body: dict | None = None) -> dict:
        key = (method, path.split("?")[0])
        if key not in routes:
            raise httpx.HTTPStatusError("404", request=httpx.Request(method, path), response=httpx.Response(404))
        return routes[key]

    return request


def test_collect_sessions_maps_the_service(monkeypatch, tmp_path):
    routes = {
        ("GET", "/api/session"): {"data": [_session(), _session("ses_2", parentID="ses_1", title="sub")]},
        ("GET", "/api/session/active"): {"data": {"ses_1": {"type": "running"}}},
        ("GET", "/api/session/ses_1/message"): {"data": [_assistant(tool=("shell", {"command": "pytest -q"}, "running"), tokens={"input": 100, "output": 5, "reasoning": 1, "cache": {"read": 900, "write": 0}}), {"type": "user", "text": "Fix it", "time": {"created": 1789800000000}}]},
        ("GET", "/api/session/ses_1/permission"): {"data": [{"id": "per_1", "action": "shell", "resources": ["git push"]}]},
        ("GET", "/api/session/ses_1/form"): {"data": []},
        ("GET", "/api/model"): {"data": [{"id": "deepseek-v4.1-flash", "limit": {"context": 1_000_000}}]},
    }
    state_file = tmp_path / "service.json"
    state_file.write_text(json.dumps({"url": SERVICE.url, "password": SERVICE.password}))
    settings = Settings(agents=("opencode",), opencode_state_file=state_file)
    monkeypatch.setattr(opencode, "default_request", lambda service, timeout=5.0: _fake_request(routes))
    sessions, summary = collect_sessions(settings, NOW, {})
    assert len(sessions) == 1
    session = sessions[0]
    assert session.agent == "opencode" and session.agent_detail == "build"
    assert session.status == "attention" and session.detail == "needs permission"
    assert session.name == "Fix the collector" and session.project == "proj"
    assert session.last_prompt == "Fix it" and session.can_send is True
    assert session.history == [{"role": "user", "text": "Fix it"}]  # the assistant step was tool-only
    assert session.agents == 1  # the child session is counted on the parent card
    assert session.context_tokens == 1000 and session.context_window == 1_000_000 and session.context_pct == 0
    assert session.question["tool_use_id"] == "per_1" and session.question["answerable"] is True
    assert opencode.PENDING.get("per_1") is not None
    assert summary["today"] == 1 and summary["attention"] == 1
    opencode.PENDING._items.clear()


def test_collect_sessions_is_silent_without_a_service(tmp_path):
    settings = Settings(agents=("opencode",), opencode_state_file=tmp_path / "missing.json")
    sessions, summary = collect_sessions(settings, NOW, {})
    assert sessions == [] and summary["today"] == 0


def test_collect_sessions_is_silent_when_the_service_is_down(monkeypatch, tmp_path):
    state_file = tmp_path / "service.json"
    state_file.write_text(json.dumps({"url": SERVICE.url, "password": SERVICE.password}))
    settings = Settings(agents=("opencode",), opencode_state_file=state_file)

    def refuse(service, timeout=5.0):
        def request(method, path, body=None):
            raise httpx.ConnectError("connection refused")

        return request

    monkeypatch.setattr(opencode, "default_request", refuse)
    sessions, summary = collect_sessions(settings, NOW, {})
    assert sessions == [] and summary["today"] == 0
