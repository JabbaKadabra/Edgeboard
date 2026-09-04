"""Post a message into a running Claude Code session through its inbox socket.

Every interactive session (Claude Code >= 2.1.224) listens on a Unix socket
whose path is ``messagingSocketPath`` in ``~/.claude/sessions/<pid>.json``;
``<pid>.<sha256>.key`` next to it holds the ``peerToken`` other sessions
authenticate with (optional on Linux, sent anyway). The protocol is one JSON
object per line: an ``auth`` line, then a ``user`` message, then EOF. An idle
session starts a new turn with the text; a busy one reads it between tool
calls. Slash commands in the text do not run, so presets are phrased as
instructions. ``find_inbox`` only reads files; ``send_message`` is the one
place that opens a socket (never in demo mode, see the server).
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class Inbox:
    socket_path: str
    token: str | None


def _pid_info(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _peer_token(sessions_dir: Path, pid: str) -> str | None:
    for key in sessions_dir.glob(f"{pid}.*.key"):
        try:
            data = json.loads(key.read_text())
        except (OSError, ValueError):
            continue
        token = data.get("peerToken") if isinstance(data, dict) else None
        if isinstance(token, str) and token:
            return token
    return None


def find_inbox(claude_dir: Path, session_id: str) -> Inbox | None:
    """The inbox of ``session_id`` from its newest pid file, or None when it has none."""
    sessions_dir = claude_dir / "sessions"
    if not sessions_dir.is_dir():
        return None
    candidates = []
    for path in sessions_dir.glob("*.json"):
        info = _pid_info(path)
        if not info or info.get("sessionId") != session_id:
            continue
        sock = info.get("messagingSocketPath")
        if not isinstance(sock, str) or not sock or not os.path.exists(sock):
            continue
        candidates.append((info.get("startedAt") or 0, path.stem, sock))
    if not candidates:
        return None
    _, pid, sock = max(candidates)
    return Inbox(sock, _peer_token(sessions_dir, pid))


def _connect(path: str) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(2.0)
    sock.connect(path)
    return sock


def send_message(inbox: Inbox, text: str, connect: Callable[[str], socket.socket] = _connect) -> None:
    """Deliver ``text`` as a user message; socket errors propagate to the caller."""
    lines = []
    if inbox.token:
        lines.append({"type": "auth", "token": inbox.token})
    lines.append({"type": "user", "message": {"role": "user", "content": text}})
    payload = "".join(json.dumps(line) + "\n" for line in lines).encode("utf-8")
    sock = connect(inbox.socket_path)
    try:
        sock.sendall(payload)
        sock.shutdown(socket.SHUT_WR)
        try:  # drain whatever the session answers so it can close cleanly
            while sock.recv(4096):
                pass
        except OSError:
            pass
    finally:
        sock.close()
