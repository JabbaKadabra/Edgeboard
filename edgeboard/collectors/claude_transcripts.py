"""Pure parsing of Claude Code transcript files (JSONL, one per session).

Nothing in here touches the network or D-Bus; the only I/O helpers read
parts of a file (``read_tail``, ``read_head``, ``read_transcript`` and
``read_new_lines`` for append-only parsing).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

TITLE_MAX = 60
PROMPT_MAX = 300
TOOL_HINT_MAX = 40
# Paired tags Claude Code injects into prompts (<system-reminder>…</system-reminder>,
# <command-name>…</command-name>, …). Requires a matching closing tag so that
# code like ``x < 5 and y > 3`` or ``List<String>`` is left alone.
_TAG_RE = re.compile(r"<([a-zA-Z][\w-]*)>[\s\S]*?</\1>")
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
    last_tool: str = ""  # name of the last tool_use block in the last assistant message
    last_tool_hint: str = ""  # short description of its input, see ``tool_hint``
    last_prompt: str = ""  # most recent user prompt, cleaned, up to PROMPT_MAX chars
    last_reply: str = ""  # most recent assistant text block, cleaned, up to PROMPT_MAX chars
    permission_mode: str = ""  # ``permissionMode`` of the latest user prompt (plan, default, acceptEdits, ...)
    # An AskUserQuestion Claude is waiting on (see ``flatten_question``): set by the
    # tool_use block, cleared by the tool_result that answers it.
    question: dict | None = None
    # ``system`` / ``compact_boundary`` lines: how often the conversation was
    # compacted, when the last one happened and what triggered it (auto, manual).
    compactions: int = 0
    last_compact_ts: datetime | None = None
    last_compact_trigger: str = ""


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
    return read_tail_bytes(path, max_bytes).decode("utf-8", errors="replace")


def read_tail_bytes(path: Path, max_bytes: int = 256_000) -> bytes:
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > max_bytes:
            fh.seek(size - max_bytes)
            fh.readline()  # drop the partial first line
        return fh.read()


def read_head(path: Path, max_bytes: int = 64_000) -> str:
    """Return the first ``max_bytes`` of a file, cut at a line boundary."""
    return read_head_bytes(path, max_bytes).decode("utf-8", errors="replace")


def read_head_bytes(path: Path, max_bytes: int = 64_000) -> bytes:
    with path.open("rb") as fh:
        data = fh.read(max_bytes)
    if len(data) == max_bytes:
        data = data[: data.rfind(b"\n") + 1]
    return data


def read_transcript(path: Path, full_limit: int = 4_000_000, head_bytes: int = 64_000, tail_bytes: int = 512_000) -> str:
    """Whole file when small; otherwise its head (for the title) plus its tail (for status)."""
    return read_transcript_bytes(path, full_limit, head_bytes, tail_bytes).decode("utf-8", errors="replace")


def read_transcript_bytes(path: Path, full_limit: int = 4_000_000, head_bytes: int = 64_000, tail_bytes: int = 512_000) -> bytes:
    size = path.stat().st_size
    if size <= full_limit:
        return path.read_bytes()
    return read_head_bytes(path, head_bytes) + b"\n" + read_tail_bytes(path, tail_bytes)


def read_new_lines(path: Path, start: int, end: int) -> tuple[str, int]:
    """The complete lines in ``[start, end)`` of an append-only file and the offset after them.

    Transcripts only grow, so a poll parses just the bytes written since the
    last one. A trailing partial line (the writer is mid-line) is left for the
    next call: the returned offset always sits on a line boundary.
    """
    with path.open("rb") as fh:
        fh.seek(start)
        data = fh.read(end - start)
    cut = data.rfind(b"\n") + 1
    if cut == 0:
        return "", start
    return data[:cut].decode("utf-8", errors="replace"), start + cut


def context_window_for(model: str, default: int) -> int:
    """The model's context window: 1M for the ``[1m]`` variants Claude Code names that way, else ``default``."""
    return 1_000_000 if "[1m]" in (model or "").lower() else default


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


def clean_text(text: str, limit: int) -> str:
    """Whole prompt with tags removed and whitespace collapsed, truncated."""
    text = re.sub(r"\s+", " ", _TAG_RE.sub("", text)).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def tool_hint(name: str, tool_input) -> str:
    """A short, human-readable summary of a tool call's input.

    Bash: the command; Read/Edit/Write: the file's basename; Grep/Glob: the
    pattern; Agent: its description. Empty when nothing recognisable is there.
    """
    if not isinstance(tool_input, dict):
        return ""

    def field(key: str) -> str:
        value = tool_input.get(key)
        return value.strip() if isinstance(value, str) else ""

    if name == "Bash":
        hint = re.sub(r"\s+", " ", field("command"))
        return hint[: TOOL_HINT_MAX - 1].rstrip() + "…" if len(hint) > TOOL_HINT_MAX else hint
    if name in ("Read", "Edit", "Write", "NotebookEdit"):
        path = field("file_path") or field("notebook_path")
        return Path(path).name if path else ""
    if name in ("Grep", "Glob"):
        pattern = field("pattern")
        return f'"{pattern}"' if pattern else ""
    if name in ("Agent", "Task"):
        return field("description")
    return ""


def flatten_question(tool_use_id: str, tool_input) -> dict | None:
    """An AskUserQuestion input flattened for the page.

    ``{tool_use_id, title, questions: [{question, header, options: [label, …], multi}]}``,
    or None without a ``tool_use_id`` or a usable ``questions`` list.
    """
    if not isinstance(tool_use_id, str) or not tool_use_id or not isinstance(tool_input, dict):
        return None
    raw = tool_input.get("questions")
    if not isinstance(raw, list):
        return None
    questions = []
    for q in raw:
        if not isinstance(q, dict) or not isinstance(q.get("question"), str) or not q["question"]:
            continue
        options = q.get("options") if isinstance(q.get("options"), list) else []
        labels = [o["label"] for o in options if isinstance(o, dict) and isinstance(o.get("label"), str) and o["label"]]
        header = q.get("header") if isinstance(q.get("header"), str) else ""
        questions.append({"question": q["question"], "header": header, "options": labels, "multi": bool(q.get("multiSelect"))})
    if not questions:
        return None
    title = tool_input.get("title") if isinstance(tool_input.get("title"), str) else ""
    return {"tool_use_id": tool_use_id, "title": title, "questions": questions}


def _message(entry: dict) -> dict:
    """The ``message`` object of an entry, or ``{}`` when missing or mis-shaped."""
    message = entry.get("message")
    return message if isinstance(message, dict) else {}


def _content_blocks(message: dict) -> list[dict]:
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _user_kind(entry: dict) -> str:
    blocks = _content_blocks(_message(entry))
    if any(b.get("type") == "tool_result" for b in blocks):
        return "tool_result"
    return "user_prompt"


def _prompt_text(entry: dict) -> str:
    parts = [b.get("text", "") for b in _content_blocks(_message(entry)) if b.get("type") == "text"]
    return "\n".join(p for p in parts if isinstance(p, str) and p)


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class UsageParser:
    """Incremental ``UsageEvent`` collector: feed entries as the transcript grows.

    Claude Code writes the same assistant message several times while
    streaming; the last occurrence carries the final numbers, so events are
    de-duplicated by message id and keep the position of their first sighting.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, UsageEvent] = {}
        self._order: list[str] = []
        self._index = 0

    @property
    def events(self) -> list[UsageEvent]:
        return [self._by_id[k] for k in self._order]

    def feed(self, entries: Iterable[dict]) -> list[UsageEvent]:
        for entry in entries:
            index = self._index
            self._index += 1
            try:
                if entry.get("type") != "assistant":
                    continue
                message = _message(entry)
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue
                ts = parse_ts(entry.get("timestamp"))
                if ts is None:
                    continue
                msg_id = str(message.get("id") or entry.get("requestId") or entry.get("uuid") or f"#{index}")
                if msg_id not in self._by_id:
                    self._order.append(msg_id)
                self._by_id[msg_id] = UsageEvent(
                    ts=ts,
                    model=str(message.get("model") or ""),
                    input=_int(usage.get("input_tokens")),
                    output=_int(usage.get("output_tokens")),
                    cache_read=_int(usage.get("cache_read_input_tokens")),
                    cache_write=_int(usage.get("cache_creation_input_tokens")),
                )
            except (AttributeError, TypeError, ValueError):
                continue  # one mis-shaped line must not take the whole file down
        return self.events


def usage_events(entries: Iterable[dict]) -> list[UsageEvent]:
    """Usage per assistant message, de-duplicated by message id (see ``UsageParser``)."""
    return UsageParser().feed(entries)


class SessionParser:
    """Incremental ``SessionFacts`` builder: feed entries as the transcript grows."""

    def __init__(self) -> None:
        self.facts = SessionFacts()
        self._first_prompt = ""
        self._summary = ""
        self._seen_ids: set[str] = set()
        self._index = 0

    def feed(self, entries: Iterable[dict]) -> SessionFacts:
        facts = self.facts
        for entry in entries:
            index = self._index
            self._index += 1
            try:
                kind = entry.get("type")
                if kind == "summary":
                    self._summary = str(entry.get("summary") or "")
                    continue
                if kind == "system":
                    if entry.get("subtype") == "compact_boundary":
                        facts.compactions += 1
                        facts.last_compact_ts = parse_ts(entry.get("timestamp")) or facts.last_compact_ts
                        meta = entry.get("compactMetadata")
                        trigger = meta.get("trigger") if isinstance(meta, dict) else ""
                        facts.last_compact_trigger = trigger if isinstance(trigger, str) else ""
                    continue
                if kind not in ("user", "assistant"):
                    continue
                if entry.get("isSidechain") or (kind == "user" and entry.get("isMeta")):
                    continue
                if isinstance(entry.get("cwd"), str) and entry["cwd"]:
                    facts.cwd = entry["cwd"]
                if isinstance(entry.get("gitBranch"), str) and entry["gitBranch"]:
                    facts.branch = entry["gitBranch"]
                if isinstance(entry.get("sessionId"), str) and entry["sessionId"]:
                    facts.session_id = entry["sessionId"]
                ts = parse_ts(entry.get("timestamp"))
                if ts is not None:
                    facts.first_ts = facts.first_ts or ts
                    facts.last_ts = ts
                message = _message(entry)
                if kind == "assistant" and not message:
                    continue  # an assistant line without a message object carries nothing usable
                if kind == "user":
                    facts.last_kind = _user_kind(entry)
                    facts.last_stop_reason = ""
                    facts.question = None  # the tool_result answers it (or the user moved on)
                    if facts.last_kind == "user_prompt":
                        if isinstance(entry.get("permissionMode"), str) and entry["permissionMode"]:
                            facts.permission_mode = entry["permissionMode"]
                        prompt = _prompt_text(entry)
                        if not self._first_prompt:
                            self._first_prompt = clean_prompt(prompt)
                        facts.last_prompt = clean_text(prompt, PROMPT_MAX)
                else:
                    facts.last_kind = "assistant"
                    facts.last_stop_reason = str(message.get("stop_reason") or "")
                    facts.last_tool, facts.last_tool_hint = "", ""
                    facts.question = None
                    for block in _content_blocks(message):
                        if block.get("type") == "tool_use" and isinstance(block.get("name"), str):
                            facts.last_tool = block["name"]
                            facts.last_tool_hint = tool_hint(block["name"], block.get("input"))
                            if block["name"] == "AskUserQuestion":
                                facts.question = flatten_question(block.get("id"), block.get("input"))
                        # Streaming writes one content block per line, so only a
                        # non-empty text block replaces the reply.
                        elif block.get("type") == "text" and isinstance(block.get("text"), str):
                            reply = clean_text(block["text"], PROMPT_MAX)
                            if reply:
                                facts.last_reply = reply
                    if isinstance(message.get("model"), str) and message["model"]:
                        facts.model = message["model"]
                    usage = message.get("usage")
                    if isinstance(usage, dict):
                        facts.context_tokens = (
                            _int(usage.get("input_tokens"))
                            + _int(usage.get("cache_read_input_tokens"))
                            + _int(usage.get("cache_creation_input_tokens"))
                        )
                    msg_id = str(message.get("id") or entry.get("uuid") or f"#{index}")
                    if msg_id not in self._seen_ids:
                        self._seen_ids.add(msg_id)
                        facts.assistant_messages += 1
            except (AttributeError, TypeError, ValueError):
                continue  # skip mis-shaped entries instead of failing the whole session
        facts.title = clean_prompt(self._summary) if self._summary else self._first_prompt
        return facts


def session_facts(entries: Iterable[dict]) -> SessionFacts:
    return SessionParser().feed(entries)
