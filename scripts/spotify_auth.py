#!/usr/bin/env python3
"""One-time Spotify login so the dashboard can show the play queue.

MPRIS has no queue, so "up next" comes from the Spotify Web API. This script
runs the Authorization Code + PKCE flow (no client secret) and writes
``{"client_id", "refresh_token"}`` to the token file the server reads
(``EDGEBOARD_SPOTIFY_TOKEN_FILE``, default ~/.config/edgeboard/spotify-token.json).

Setup, once:
  1. https://developer.spotify.com/dashboard → Create app.
  2. Redirect URI: http://127.0.0.1:8766/callback  (must match exactly).
  3. Run:  python scripts/spotify_auth.py --client-id <the app's Client ID>
     and open the printed URL in any browser logged in to your Spotify account.

Stdlib only, so it also runs outside the venv.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
SCOPES = "user-read-playback-state user-read-currently-playing"
DEFAULT_TOKEN_FILE = Path.home() / ".config" / "edgeboard" / "spotify-token.json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--client-id", default=os.environ.get("EDGEBOARD_SPOTIFY_CLIENT_ID"), help="Client ID of your Spotify app")
    ap.add_argument("--port", type=int, default=8766, help="loopback port for the redirect (default 8766)")
    ap.add_argument("--out", type=Path, default=Path(os.environ.get("EDGEBOARD_SPOTIFY_TOKEN_FILE") or DEFAULT_TOKEN_FILE))
    args = ap.parse_args()
    if not args.client_id:
        ap.error("--client-id (or EDGEBOARD_SPOTIFY_CLIENT_ID) is required")

    redirect_uri = f"http://127.0.0.1:{args.port}/callback"
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = AUTH_URL + "?" + urllib.parse.urlencode(
        {
            "client_id": args.client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "state": state,
        }
    )
    print("Open this URL in a browser logged in to Spotify:\n\n  " + url + "\n")
    print(f"Waiting for the redirect on {redirect_uri} …")

    result: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if urllib.parse.urlparse(self.path).path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            if q.get("state", [""])[0] != state:
                result["error"] = "state mismatch"
            elif "error" in q:
                result["error"] = q["error"][0]
            else:
                result["code"] = q.get("code", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"edgeboard: you can close this tab." if "code" in result else b"edgeboard: login failed, check the terminal.")

        def log_message(self, *a):  # silence request logging
            pass

    with HTTPServer(("127.0.0.1", args.port), Handler) as srv:
        while "code" not in result and "error" not in result:
            srv.handle_request()
    if "error" in result:
        print("login failed:", result["error"], file=sys.stderr)
        return 1

    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": redirect_uri,
            "client_id": args.client_id,
            "code_verifier": verifier,
        }
    ).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            tok = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        print("token exchange failed:", exc.code, exc.read().decode(errors="replace"), file=sys.stderr)
        return 1
    if not tok.get("refresh_token"):
        print("no refresh token in response; keys:", sorted(tok), file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"client_id": args.client_id, "refresh_token": tok["refresh_token"]}, indent=2) + "\n")
    try:
        args.out.chmod(0o600)
    except OSError:
        pass
    print(f"saved {args.out}. Restart edgeboard (systemctl --user restart edgeboard) to pick it up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
