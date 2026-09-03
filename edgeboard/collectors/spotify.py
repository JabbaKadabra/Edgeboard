"""Spotify state and controls through ``playerctl`` (MPRIS over D-Bus)."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import asdict, dataclass
from typing import Callable

SEP = "\x1f"
FORMAT = SEP.join(
    [
        "{{status}}",
        "{{title}}",
        "{{artist}}",
        "{{album}}",
        "{{mpris:artUrl}}",
        "{{mpris:length}}",
        "{{position}}",
        "{{shuffle}}",
        "{{volume}}",
    ]
)
ACTIONS = {"play_pause": "play-pause", "next": "next", "previous": "previous"}

Runner = Callable[[list[str]], tuple[int, str]]
log = logging.getLogger("edgeboard.spotify")


@dataclass
class SpotifyState:
    running: bool = False
    available: bool = True
    status: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    art_url: str = ""
    length_s: float = 0.0
    position_s: float = 0.0
    shuffle: bool = False
    volume: float = 1.0

    def to_dict(self) -> dict:
        return asdict(self)


def default_runner(args: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=3)
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    if proc.returncode != 0 and proc.stderr:
        log.debug("%s: %s", " ".join(args), proc.stderr.strip())
    return proc.returncode, proc.stdout


def _micros(value: str) -> float:
    try:
        return int(value) / 1_000_000
    except ValueError:
        return 0.0


def _fraction(value: str) -> float:
    try:
        return min(max(float(value.strip()), 0.0), 1.0)
    except ValueError:
        return 1.0


def parse_metadata(output: str) -> SpotifyState:
    parts = output.rstrip("\n").split(SEP)
    if len(parts) < 8:
        return SpotifyState(running=False)
    status, title, artist, album, art, length, position, shuffle = parts[:8]
    # older playerctl output had eight fields; volume is a later addition
    volume = _fraction(parts[8]) if len(parts) > 8 else 1.0
    return SpotifyState(
        running=True,
        status=status.strip() or "Stopped",
        title=title.strip(),
        artist=artist.strip(),
        album=album.strip(),
        art_url=art.strip(),
        length_s=_micros(length.strip()),
        position_s=_micros(position.strip()),
        shuffle=shuffle.strip().lower() == "on",
        volume=volume,
    )


def read_spotify(runner: Runner, player: str) -> SpotifyState:
    code, out = runner(["playerctl", "-p", player, "metadata", "--format", FORMAT])
    if code == 127:
        return SpotifyState(running=False, available=False)
    if code != 0:
        return SpotifyState(running=False)
    return parse_metadata(out)


def control(runner: Runner, player: str, action: str) -> bool:
    if action not in ACTIONS:
        raise ValueError(f"unknown spotify action: {action}")
    code, _ = runner(["playerctl", "-p", player, ACTIONS[action]])
    return code == 0


def seek(runner: Runner, player: str, seconds: float) -> bool:
    code, _ = runner(["playerctl", "-p", player, "position", f"{seconds:g}"])
    return code == 0


def set_volume(runner: Runner, player: str, volume: float) -> bool:
    code, _ = runner(["playerctl", "-p", player, "volume", f"{volume:g}"])
    return code == 0


def skip(runner: Runner, player: str, count: int) -> bool:
    """Advance ``count`` tracks with repeated ``next``; MPRIS has no jump-to-queue-entry."""
    for _ in range(count):
        code, _ = runner(["playerctl", "-p", player, "next"])
        if code != 0:
            return False
    return True
