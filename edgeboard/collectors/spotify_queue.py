"""Upcoming tracks from the Spotify Web API.

MPRIS (what ``spotify.py`` reads through playerctl) has no notion of a queue:
Spotify's desktop client does not implement the TrackList interface. The only
way to know what plays next is the Web API's ``/me/player/queue`` endpoint,
which needs a user login. ``scripts/spotify_auth.py`` does that login once
(PKCE, so no client secret) and writes a token file; this module refreshes
the access token from it and polls the queue. Without the file the feature
is simply off (``configured: False``) and nothing is fetched.

Only ``parse_queue`` is pure; ``QueueClient`` does the HTTP and file I/O and
takes an injectable ``httpx.Client`` so tests never touch the network.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

TOKEN_URL = "https://accounts.spotify.com/api/token"
QUEUE_URL = "https://api.spotify.com/v1/me/player/queue"
SCOPES = "user-read-playback-state user-read-currently-playing"
QUEUE_LIMIT = 20
log = logging.getLogger("edgeboard.spotify_queue")


@dataclass
class QueueTrack:
    title: str
    artist: str
    album: str = ""
    art_url: str = ""
    length_s: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def parse_queue(data: dict, limit: int = QUEUE_LIMIT) -> list[QueueTrack]:
    """Flatten the ``/me/player/queue`` payload into the next ``limit`` tracks."""
    tracks: list[QueueTrack] = []
    for item in (data.get("queue") or [])[:limit]:
        if not isinstance(item, dict):
            continue
        artists = item.get("artists") or []
        album = item.get("album") or {}
        images = album.get("images") or [] if isinstance(album, dict) else []
        # images are ordered largest first; the smallest is plenty for a list row
        art = images[-1].get("url", "") if images and isinstance(images[-1], dict) else ""
        tracks.append(
            QueueTrack(
                title=str(item.get("name") or ""),
                artist=", ".join(str(a.get("name", "")) for a in artists if isinstance(a, dict)),
                album=str(album.get("name", "")) if isinstance(album, dict) else "",
                art_url=art,
                length_s=(item.get("duration_ms") or 0) / 1000,
            )
        )
    return tracks


def load_credentials(path: Path) -> dict | None:
    """``{"client_id", "refresh_token"}`` from the token file, or None if absent/invalid."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict) and data.get("client_id") and data.get("refresh_token"):
        return {"client_id": str(data["client_id"]), "refresh_token": str(data["refresh_token"])}
    return None


def save_credentials(path: Path, creds: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(creds, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


class NotConfigured(Exception):
    """No token file: the queue feature is off, not broken."""


class QueueClient:
    """Refreshes the access token when needed and fetches the queue."""

    def __init__(self, token_file: Path, http: httpx.Client | None = None):
        self.token_file = token_file
        self.http = http or httpx.Client(timeout=10)
        self._access_token: str | None = None
        self._expires_at = 0.0

    def _refresh(self, creds: dict) -> None:
        r = self.http.post(
            TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": creds["refresh_token"], "client_id": creds["client_id"]},
        )
        if r.status_code in (400, 401):
            raise RuntimeError("Spotify refresh token rejected; run scripts/spotify_auth.py again")
        r.raise_for_status()
        tok = r.json()
        self._access_token = tok["access_token"]
        self._expires_at = time.monotonic() + float(tok.get("expires_in", 3600)) - 60
        # PKCE clients get a rotated refresh token; keep the file current.
        if tok.get("refresh_token") and tok["refresh_token"] != creds["refresh_token"]:
            save_credentials(self.token_file, {**creds, "refresh_token": tok["refresh_token"]})

    def fetch(self) -> list[QueueTrack]:
        creds = load_credentials(self.token_file)
        if creds is None:
            raise NotConfigured
        if self._access_token is None or time.monotonic() >= self._expires_at:
            self._refresh(creds)
        r = self.http.get(QUEUE_URL, headers={"Authorization": f"Bearer {self._access_token}"})
        if r.status_code == 401:
            self._access_token = None  # revoked early; next poll refreshes
            raise RuntimeError("Spotify access token expired")
        if r.status_code == 403:
            raise RuntimeError("Spotify queue needs a Premium account")
        if r.status_code == 204:
            return []
        r.raise_for_status()
        data = r.json()
        return parse_queue(data) if isinstance(data, dict) else []
