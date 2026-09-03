"""Pure parsing of Claude Code transcript files (JSONL, one per session).

Nothing in here touches the network or D-Bus; the only I/O helper is
``read_tail`` which reads the end of a file.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

TITLE_MAX = 60
_TAG_RE = re.compile(r"<[^>]+>[\s\S]*?</[^>]+>|<[^>]+>")
_MODEL_DATE_RE = re.compile(r"-\d{8}$")


@dataclass(frozen=True)
class UsageEvent:
    ts: datetime
    model: str
    input: int
    output: int
    cache_read: int
    cache_write: int

    @property
    def burn(self) -> int:
        """Tokens that count against limits: everything except cache reads."""
        return self.input + self.output + self.cache_write


@dataclass
class SessionFacts:
    title: str = ""
    cwd: str = ""
    branch: str = ""
    model: str = ""
    last_kind: str = ""  # "user_prompt" | "tool_result" | "assistant" | ""
    last_stop_reason: str = ""
    context_tokens: int = 0
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    assistant_messages: int = 0
    session_id: str = ""


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def iter_entries(text: str) -> Iterator[dict]:
    """Yield parsed JSON objects, skipping blank or malformed lines."""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


def read_tail(path: Path, max_bytes: int = 256_000) -> str:
    """Return the last ``max_bytes`` of a file, starting at a line boundary."""
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > max_bytes:
            fh.seek(size - max_bytes)
            fh.readline()  # drop the partial first line
        return fh.read().decode("utf-8", errors="replace")


def read_head(path: Path, max_bytes: int = 64_000) -> str:
    """Return the first ``max_bytes`` of a file, cut at a line boundary."""
    with path.open("rb") as fh:
        data = fh.read(max_bytes)
    if len(data) == max_bytes:
        data = data[: data.rfind(b"\n") + 1]
    return data.decode("utf-8", errors="replace")


def read_transcript(path: Path, full_limit: int = 4_000_000, head_bytes: int = 64_000, tail_bytes: int = 512_000) -> str:
    """Whole file when small; otherwise its head (for the title) plus its tail (for status)."""
    size = path.stat().st_size
    if size <= full_limit:
        return path.read_text(encoding="utf-8", errors="replace")
    return read_head(path, head_bytes) + "\n" + read_tail(path, tail_bytes)


def short_model(name: str) -> str:
    """``claude-fable-5-1`` -> ``fable-5-1``; drops trailing date stamps."""
    if not name:
        return ""
    name = _MODEL_DATE_RE.sub("", name)
    if name.startswith("claude-"):
        name = name[len("claude-"):]
    return name


def clean_prompt(text: str, limit: int = TITLE_MAX) -> str:
    """First meaningful line of a prompt, tags removed, truncated."""
    text = _TAG_RE.sub("", text)
    for line in text.splitlines():
        line = line.strip()
        if line:
            text = line
            break
    else:
        text = ""
    text = re.sub(r"\s+", " ", text)
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _content_blocks(message: dict) -> list[dict]:
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _user_kind(entry: dict) -> str:
    blocks = _content_blocks(entry.get("message") or {})
    if any(b.get("type") == "tool_result" for b in blocks):
        return "tool_result"
    return "user_prompt"


def _prompt_text(entry: dict) -> str:
    parts = [b.get("text", "") for b in _content_blocks(entry.get("message") or {}) if b.get("type") == "text"]
    return "\n".join(p for p in parts if p)


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def usage_events(entries: Iterable[dict]) -> list[UsageEvent]:
    """Usage per assistant message, de-duplicated by message id.

    Claude Code writes the same assistant message several times while
    streaming; the last occurrence carries the final numbers.
    """
    by_id: dict[str, UsageEvent] = {}
    order: list[str] = []
    for entry in entries:
        if entry.get("type") != "assistant":
            continue
        message = entry.get("message") or {}
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        ts = parse_ts(entry.get("timestamp"))
        if ts is None:
            continue
        msg_id = message.get("id") or entry.get("requestId") or entry.get("uuid") or str(len(order))
        if msg_id not in by_id:
            order.append(msg_id)
        by_id[msg_id] = UsageEvent(
            ts=ts,
            model=message.get("model") or "",
            input=_int(usage.get("input_tokens")),
            output=_int(usage.get("output_tokens")),
            cache_read=_int(usage.get("cache_read_input_tokens")),
            cache_write=_int(usage.get("cache_creation_input_tokens")),
        )
    return [by_id[k] for k in order]


def session_facts(entries: Iterable[dict]) -> SessionFacts:
    facts = SessionFacts()
    first_prompt = ""
    summary = ""
    seen_ids: set[str] = set()
    for entry in entries:
        kind = entry.get("type")
        if kind == "summary":
            summary = str(entry.get("summary") or "")
            continue
        if kind not in ("user", "assistant"):
            continue
        if entry.get("isSidechain"):
            continue
        if entry.get("cwd"):
            facts.cwd = entry["cwd"]
        if entry.get("gitBranch"):
            facts.branch = entry["gitBranch"]
        if entry.get("sessionId"):
            facts.session_id = entry["sessionId"]
        ts = parse_ts(entry.get("timestamp"))
        if ts is not None:
            facts.first_ts = facts.first_ts or ts
            facts.last_ts = ts
        message = entry.get("message") or {}
        if kind == "user":
            if entry.get("isMeta"):
                continue
            facts.last_kind = _user_kind(entry)
            facts.last_stop_reason = ""
            if facts.last_kind == "user_prompt" and not first_prompt:
                first_prompt = clean_prompt(_prompt_text(entry))
        else:
            facts.last_kind = "assistant"
            facts.last_stop_reason = message.get("stop_reason") or ""
            if message.get("model"):
                facts.model = message["model"]
            usage = message.get("usage")
            if isinstance(usage, dict):
                facts.context_tokens = (
                    _int(usage.get("input_tokens"))
                    + _int(usage.get("cache_read_input_tokens"))
                    + _int(usage.get("cache_creation_input_tokens"))
                )
            msg_id = message.get("id") or entry.get("uuid")
            if msg_id and msg_id not in seen_ids:
                seen_ids.add(msg_id)
                facts.assistant_messages += 1
    facts.title = clean_prompt(summary) if summary else first_prompt
    return facts
