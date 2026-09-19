#!/usr/bin/env python3
"""Hook that feeds the edgeboard dashboard and lets the panel answer prompts.

Works for Claude Code and Codex CLI, which use the same hook JSON on stdin
(``session_id``, ``hook_event_name``, ``cwd``, ``tool_name``, ``tool_input``,
``last_assistant_message``, …): wire it for every hook event (README, "Session
state from hooks" for Claude, "Codex sessions" for Codex). It reads the hook
JSON, POSTs it to /api/hook, and then, when the event is answerable, waits
(``--wait`` seconds, default 90 or EDGEBOARD_ANSWER_WAIT) for a tap on the panel
by long-polling /api/answer/<id>:

- Claude Code ``PreToolUse`` of ``AskUserQuestion``: the panel's answers are
  returned as ``updatedInput.answers``, which skips the terminal dialog.
- Codex CLI ``PermissionRequest``: the panel's choice becomes the hook's
  ``allow``/``deny`` decision. The request has no id of its own, so the script
  generates one and passes it in the posted body.

In every other case, including a dashboard that is down, it exits 0 silently so
the agent behaves as usual. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

DEFAULT_URL = "http://127.0.0.1:8765"
POLL = 25.0  # seconds one GET /api/answer may hang server-side


def wants_answer(hook: dict, agent: str = "claude") -> bool:
    if agent == "codex":
        return hook.get("hook_event_name") == "PermissionRequest" and bool(hook.get("tool_use_id"))
    return (
        hook.get("hook_event_name") == "PreToolUse"
        and hook.get("tool_name") == "AskUserQuestion"
        and isinstance(hook.get("tool_use_id"), str)
        and bool(hook["tool_use_id"])
    )


def decision(hook: dict, reply: dict, agent: str = "claude") -> dict | None:
    """The hook output for an answered request; None when the terminal should ask."""
    if reply.get("status") != "answered" or not isinstance(reply.get("answers"), dict) or not reply["answers"]:
        return None
    if agent == "codex":
        choice = " ".join(str(v) for v in reply["answers"].values()).lower()
        allow = "deny" not in choice and "reject" not in choice
        output = {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow" if allow else "deny"}}
        if not allow:
            output["decision"]["message"] = "Denied from the edgeboard panel."
        return {"hookSpecificOutput": output}
    tool_input = hook.get("tool_input") if isinstance(hook.get("tool_input"), dict) else {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {**tool_input, "answers": reply["answers"]},
        }
    }


def _request(url: str, data: bytes | None, timeout: float) -> dict:
    req = urllib.request.Request(url, data=data, headers={"content-type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=os.environ.get("EDGEBOARD_URL") or DEFAULT_URL)
    parser.add_argument("--wait", type=float, default=float(os.environ.get("EDGEBOARD_ANSWER_WAIT") or 90))
    parser.add_argument("--poll", type=float, default=POLL)
    parser.add_argument("--agent", choices=("claude", "codex"), default="claude")
    args = parser.parse_args(argv)
    base = args.url.rstrip("/")
    try:
        hook = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(hook, dict):
        return 0
    if args.agent == "codex" and hook.get("hook_event_name") == "PermissionRequest" and not hook.get("tool_use_id"):
        hook["tool_use_id"] = uuid.uuid4().hex  # Codex does not send one; the answer route needs a key
    hook["agent"] = args.agent
    try:
        _request(f"{base}/api/hook", json.dumps(hook).encode("utf-8"), timeout=2.0)
    except (OSError, ValueError):
        return 0  # the dashboard is down: nothing else to do
    if not wants_answer(hook, args.agent):
        return 0
    deadline = time.monotonic() + max(args.wait, 0.0)
    tool_use_id = urllib.request.quote(str(hook["tool_use_id"]), safe="")
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 0
        wait = min(args.poll, remaining)
        try:
            reply = _request(f"{base}/api/answer/{tool_use_id}?wait={wait:.1f}", None, timeout=wait + 5.0)
        except (OSError, ValueError):
            return 0  # gone, restarted or 404: the terminal dialog takes over
        if reply.get("status") == "pending":
            continue
        out = decision(hook, reply, args.agent)
        if out is not None:
            sys.stdout.write(json.dumps(out))
        return 0


if __name__ == "__main__":
    sys.exit(main())
