"""Today's commits in the repositories the sessions work in, through ``git log``.

``parse_log``, ``summarize`` and ``commits_since`` are pure; ``collect_git``
runs git through an injectable runner (the same shape as the Spotify one), so
tests never spawn a process.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

Runner = Callable[[list[str]], tuple[int, str]]
log = logging.getLogger("edgeboard.git")

# One record per commit: a record separator, then the fields split by a unit
# separator, then git's own ``--shortstat`` line (absent for empty commits).
RECORD = "\x1e"
SEP = "\x1f"
FORMAT = RECORD + SEP.join(["%h", "%H", "%cI", "%an", "%s"])
COMMITS_SHOWN = 20  # rows in the snapshot; the counts cover every commit
GIT_TIMEOUT = 10.0
_INSERTIONS = re.compile(r"(\d+) insertion")
_DELETIONS = re.compile(r"(\d+) deletion")


@dataclass(frozen=True)
class Commit:
    hash: str  # abbreviated
    repo: str  # last path component of the repository root
    path: str  # the repository root
    message: str  # subject line
    ts: str  # committer date, ISO 8601 with offset
    author: str
    added: int = 0
    deleted: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def default_runner(args: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    if proc.returncode != 0 and proc.stderr:
        log.debug("%s: %s", " ".join(args), proc.stderr.strip())
    return proc.returncode, proc.stdout


def _stat(pattern: re.Pattern, text: str) -> int:
    m = pattern.search(text)
    return int(m.group(1)) if m else 0


def _epoch(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def parse_log(text: str, root: str) -> list[Commit]:
    """Commits of one repository from ``git log --format=FORMAT --shortstat`` output, in git's order (newest first)."""
    repo = Path(root).name or root
    commits = []
    for record in text.split(RECORD):
        head, _, stat = record.partition("\n")
        parts = head.split(SEP)
        if len(parts) < 5 or not parts[0]:
            continue
        short, _full, ts, author = parts[:4]
        subject = SEP.join(parts[4:]).strip()
        commits.append(Commit(short, repo, root, subject, ts, author, _stat(_INSERTIONS, stat), _stat(_DELETIONS, stat)))
    return commits


def repo_root(cwd: str, runner: Runner = default_runner) -> str | None:
    """The repository containing ``cwd``, or None when it is not inside one (or git is missing)."""
    code, out = runner(["git", "-C", cwd, "rev-parse", "--show-toplevel"])
    root = out.strip()
    return root if code == 0 and root else None


def read_commits(root: str, since: datetime, runner: Runner = default_runner) -> list[Commit]:
    """Non-merge commits of ``root`` since ``since`` (committer date), newest first."""
    code, out = runner(["git", "-C", root, "log", f"--since={since.isoformat()}", "--no-merges", "--shortstat", f"--format={FORMAT}"])
    if code != 0:
        return []
    return parse_log(out, root)


def collect_git(cwds: Iterable[str], since: datetime, runner: Runner = default_runner) -> list[Commit]:
    """Every commit since ``since`` in the repositories the ``cwds`` lie in, newest first.

    Paths outside a repository (or gone) are skipped; a repository reached
    from several working directories is read once.
    """
    roots: list[str] = []
    for cwd in cwds:
        if not cwd:
            continue
        root = repo_root(cwd, runner)
        if root and root not in roots:
            roots.append(root)
    commits: list[Commit] = []
    for root in roots:
        commits.extend(read_commits(root, since, runner))
    commits.sort(key=lambda c: _epoch(c.ts), reverse=True)
    return commits


def summarize(commits: list[Commit], limit: int = COMMITS_SHOWN) -> dict:
    """The snapshot's ``git`` block: the first ``limit`` commits plus the totals over all of them."""
    return {
        "commits": [c.to_dict() for c in commits[:limit]],
        "count": len(commits),
        "added": sum(c.added for c in commits),
        "deleted": sum(c.deleted for c in commits),
    }


def commits_since(commits: Iterable[Commit], cwd: str, started_at: str | None) -> int:
    """How many of ``commits`` belong to the repository of ``cwd`` and are not older than ``started_at``."""
    if not cwd:
        return 0
    start = _epoch(started_at)
    return sum(1 for c in commits if (cwd == c.path or cwd.startswith(c.path.rstrip("/") + "/")) and _epoch(c.ts) >= start)
