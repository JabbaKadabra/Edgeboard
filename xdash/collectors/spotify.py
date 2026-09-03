"""Spotify state and controls through ``playerctl`` (MPRIS over D-Bus)."""

from __future__ import annotations

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
    ]
)
ACTIONS = {"play_pause": "play-pause", "next": "next", "previous": "previous"}

Runner = Callable[[list[str]], tuple[int, str]]


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

    def to_dict(self) -> dict:
        return asdict(self)


def default_runner(args: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=3)
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    return proc.returncode, proc.stdout


def _micros(value: str) -> float:
    try:
        return int(value) / 1_000_000
    except ValueError:
        return 0.0


def parse_metadata(output: str) -> SpotifyState:
    parts = output.rstrip("\n").split(SEP)
    if len(parts) < 8:
        return SpotifyState(running=False)
    status, title, artist, album, art, length, position, shuffle = parts[:8]
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
