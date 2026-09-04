"""The per-session inbox socket: find it from the pid file, post a message into it."""

import json
import socket
import threading
from pathlib import Path

import pytest

from edgeboard.collectors.claude_inbox import Inbox, find_inbox, send_message

SID = "11111111-2222-3333-4444-555555555555"


def _pid_file(claude_dir: Path, pid: int, sock: Path | None, started=1, key: str | None = "tok-1", **extra) -> None:
    sessions = claude_dir / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    info = {"pid": pid, "sessionId": SID, "cwd": "/home/me/proj", "startedAt": started, **extra}
    if sock is not None:
        info["messagingSocketPath"] = str(sock)
    (sessions / f"{pid}.json").write_text(json.dumps(info))
    if key is not None:
        (sessions / f"{pid}.{'ab' * 32}.key").write_text(json.dumps({"peerToken": key, "procStart": "1"}))


def test_find_inbox_reads_socket_path_and_peer_token(tmp_path):
    sock = tmp_path / "1.sock"
    sock.touch()
    _pid_file(tmp_path, 1, sock)
    assert find_inbox(tmp_path, SID) == Inbox(str(sock), "tok-1")


def test_find_inbox_without_a_key_file_still_finds_the_socket(tmp_path):
    sock = tmp_path / "1.sock"
    sock.touch()
    _pid_file(tmp_path, 1, sock, key=None)
    assert find_inbox(tmp_path, SID) == Inbox(str(sock), None)
    (tmp_path / "sessions" / f"1.{'ab' * 32}.key").write_text("not json")
    assert find_inbox(tmp_path, SID) == Inbox(str(sock), None)


def test_find_inbox_is_none_without_pid_file_path_or_socket(tmp_path):
    assert find_inbox(tmp_path, SID) is None  # no sessions dir at all
    _pid_file(tmp_path, 1, None)
    assert find_inbox(tmp_path, SID) is None  # no messagingSocketPath (old Claude Code)
    _pid_file(tmp_path, 1, tmp_path / "gone.sock")
    assert find_inbox(tmp_path, SID) is None  # socket path does not exist
    (tmp_path / "sessions" / "1.json").write_text("{broken")
    assert find_inbox(tmp_path, SID) is None
    sock = tmp_path / "2.sock"
    sock.touch()
    _pid_file(tmp_path, 2, sock)
    assert find_inbox(tmp_path, "other-session") is None


def test_find_inbox_prefers_the_newest_pid_file(tmp_path):
    old, new = tmp_path / "old.sock", tmp_path / "new.sock"
    old.touch()
    new.touch()
    _pid_file(tmp_path, 1, old, started=1, key="old")
    _pid_file(tmp_path, 2, new, started=2, key="new")
    assert find_inbox(tmp_path, SID) == Inbox(str(new), "new")


class _Listener:
    """A Unix socket server that records what one client sent until EOF."""

    def __init__(self, path: Path):
        self.path, self.received = path, b""
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen(1)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        conn, _ = self.server.accept()
        with conn:
            while chunk := conn.recv(4096):
                self.received += chunk
            conn.sendall(b'{"type":"ack"}\n')

    def lines(self):
        self.thread.join(timeout=2)
        return [json.loads(line) for line in self.received.decode().splitlines()]


def test_send_message_writes_auth_then_user_message(tmp_path):
    listener = _Listener(tmp_path / "s.sock")
    send_message(Inbox(str(listener.path), "tok"), "run the tests\nplease")
    assert listener.lines() == [
        {"type": "auth", "token": "tok"},
        {"type": "user", "message": {"role": "user", "content": "run the tests\nplease"}},
    ]


def test_send_message_skips_auth_without_a_token(tmp_path):
    listener = _Listener(tmp_path / "s.sock")
    send_message(Inbox(str(listener.path), None), "hi")
    assert listener.lines() == [{"type": "user", "message": {"role": "user", "content": "hi"}}]


def test_send_message_raises_when_nobody_listens(tmp_path):
    with pytest.raises(OSError):
        send_message(Inbox(str(tmp_path / "nobody.sock"), "tok"), "hi")
