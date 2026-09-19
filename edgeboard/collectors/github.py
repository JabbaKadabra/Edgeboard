"""CI runs from the GitHub Actions API for the repositories the sessions work in.

The GitHub pane answers "is anything running, and is anything red?" without
opening a browser: runs that are still going, and failures that a newer run of
the same workflow on the same branch has not cleared.  The token is the gh
CLI's own (``hosts.yml``) unless ``EDGEBOARD_GITHUB_TOKEN`` overrides it, so
when ``gh auth login`` has been done there is nothing new to configure.

``parse_remote``, ``parse_runs`` and ``summarize_runs`` are pure.
``GitHubClient`` does the HTTP and takes an injectable ``httpx.Client`` (the
same shape as the Spotify queue client), so tests never touch the network.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping

import httpx

from edgeboard.collectors import git

log = logging.getLogger("edgeboard.github")

API_URL = "https://api.github.com"
RUNS_PER_REPO = 30  # newest runs read per repository, before the page's filtering
RUNS_SHOWN = 8  # rows in the snapshot; the page scrolls if there are more
FAILED_WINDOW_S = 24 * 3600.0  # how long a failure stays on the panel
# Not finished yet: shown as "running" (queued/requested/pending/waiting included).
RUNNING_STATUSES = frozenset({"queued", "requested", "waiting", "pending", "in_progress"})
# Conclusions worth a red row; cancelled/neutral/skipped are not failures.
FAILED_CONCLUSIONS = frozenset({"failure", "startup_failure", "timed_out"})

_TOKEN_LINE = re.compile(r"^\s+oauth_token:\s*(\S+)")
_URL_REMOTE = re.compile(r"^(?:https?|ssh|git)://(?:[^@/]+@)?github\.com/(?P<path>.+)$", re.IGNORECASE)
_SCP_REMOTE = re.compile(r"^(?:[^@/]+@)?github\.com:(?P<path>.+)$", re.IGNORECASE)


@dataclass(frozen=True)
class Run:
    id: int
    repo: str  # owner/repo
    name: str  # workflow name
    title: str  # display title (commit / PR subject)
    branch: str
    status: str  # queued | in_progress | … | completed
    conclusion: str  # "" until the run is done
    url: str  # html_url
    number: int  # run_number
    started_at: str  # ISO 8601
    updated_at: str

    def to_dict(self) -> dict:
        return asdict(self)


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _epoch(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def parse_remote(url: str) -> str | None:
    """``git@github.com:owner/repo.git`` / ``https://github.com/owner/repo`` -> ``owner/repo``.

    Other hosts (and anything that does not name a repository) return None.
    """
    text = url.strip()
    match = _URL_REMOTE.match(text) or _SCP_REMOTE.match(text)
    if not match:
        return None
    path = match.group("path").strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [part for part in path.split("/") if part]
    return f"{parts[-2]}/{parts[-1]}" if len(parts) >= 2 else None


def parse_runs(payload: dict, repo: str) -> list[Run]:
    """Flatten a ``/repos/{repo}/actions/runs`` payload into ``Run`` records, skipping junk."""
    runs: list[Run] = []
    items = payload.get("workflow_runs") if isinstance(payload, dict) else None
    for item in items or []:
        if not isinstance(item, dict):
            continue
        started = item.get("run_started_at") or item.get("created_at") or ""
        runs.append(
            Run(
                id=_int(item.get("id")),
                repo=repo,
                name=str(item.get("name") or ""),
                title=str(item.get("display_title") or ""),
                branch=str(item.get("head_branch") or ""),
                status=str(item.get("status") or ""),
                conclusion=str(item.get("conclusion") or ""),
                url=str(item.get("html_url") or ""),
                number=_int(item.get("run_number")),
                started_at=str(started),
                updated_at=str(item.get("updated_at") or started),
            )
        )
    return runs


def summarize_runs(runs: Iterable[Run], now: datetime, failed_seconds: float = FAILED_WINDOW_S, limit: int = RUNS_SHOWN) -> dict:
    """The snapshot's ``github`` rows: runs in progress first, then failures still standing.

    Only the newest run of each (repo, workflow, branch) group counts: a later
    run, running or successful, supersedes an older failure.  A failure older
    than ``failed_seconds`` (by ``updated_at``) drops off too, so a red branch
    nobody touched does not stay on the panel forever.
    """
    latest: dict[tuple[str, str, str], Run] = {}
    for run in runs:
        key = (run.repo, run.name, run.branch)
        old = latest.get(key)
        if old is None or _epoch(run.started_at) >= _epoch(old.started_at):
            latest[key] = run
    running, failed = [], []
    for run in latest.values():
        if run.status in RUNNING_STATUSES:
            running.append(run)
        elif run.status == "completed" and run.conclusion in FAILED_CONCLUSIONS:
            age = now.timestamp() - _epoch(run.updated_at)
            if age <= failed_seconds:
                failed.append(run)
    running.sort(key=lambda r: _epoch(r.started_at))  # longest-running first
    failed.sort(key=lambda r: _epoch(r.updated_at), reverse=True)  # newest failure first
    shown = (running + failed)[:limit]
    return {
        "runs": [r.to_dict() for r in shown],
        "running": sum(1 for r in shown if r.status in RUNNING_STATUSES),
        "failed": sum(1 for r in shown if r.conclusion in FAILED_CONCLUSIONS),
    }


def _hosts_token(path: Path) -> str:
    """The active ``github.com`` token in gh's ``hosts.yml``, or "" (no file / enterprise-only)."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    inside, token = False, ""
    for line in lines:
        if not line[:1].isspace():  # a top-level key starts (or leaves) a host block
            inside = line.split(":", 1)[0].strip().lower() == "github.com"
            continue
        if inside:
            match = _TOKEN_LINE.match(line)
            if match:
                token = match.group(1).strip("\"'")  # the last one is the active token
    return token


def load_token(explicit: str = "", env: Mapping[str, str] | None = None, config_dir: Path | None = None) -> str:
    """The Actions API token: ``EDGEBOARD_GITHUB_TOKEN`` (``explicit``), ``GH_TOKEN``, else gh's file."""
    if explicit.strip():
        return explicit.strip()
    env = os.environ if env is None else env
    if (env.get("GH_TOKEN") or "").strip():
        return env["GH_TOKEN"].strip()
    if config_dir is None:
        base = env.get("GH_CONFIG_DIR") or Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "gh"
        config_dir = Path(base)
    return _hosts_token(config_dir / "hosts.yml")


class GitHubError(RuntimeError):
    """One repository's runs could not be read (token, access, rate limit, network)."""


class GitHubClient:
    """Fetches workflow runs; ``http`` is injectable so tests never touch the network."""

    def __init__(self, token: str, api_url: str = API_URL, http: httpx.Client | None = None):
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.http = http or httpx.Client(timeout=10)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "edgeboard",
        }

    def runs(self, repo: str) -> list[Run]:
        """The newest ``RUNS_PER_REPO`` runs of ``repo``; raises ``GitHubError`` when it cannot be read."""
        response = self.http.get(f"{self.api_url}/repos/{repo}/actions/runs", params={"per_page": RUNS_PER_REPO}, headers=self._headers())
        if response.status_code == 401:
            raise GitHubError("token rejected; run `gh auth login` or set EDGEBOARD_GITHUB_TOKEN")
        if response.status_code == 403:
            raise GitHubError("HTTP 403 (rate limited, or the token cannot read this repository)")
        if response.status_code == 404:
            raise GitHubError("HTTP 404 (no access, or the repository was renamed)")
        response.raise_for_status()
        payload = response.json()
        return parse_runs(payload, repo)


def collect_runs(client: GitHubClient, repos: Iterable[str]) -> tuple[list[Run], list[str]]:
    """Every repository's runs plus one message per repository that failed; the rest still count."""
    runs: list[Run] = []
    errors: list[str] = []
    for repo in repos:
        try:
            runs.extend(client.runs(repo))
        except (GitHubError, httpx.HTTPError) as exc:
            log.debug("%s: %s", repo, exc)
            errors.append(f"{repo}: {exc}")
    return runs, errors


def repo_remote(path: str, runner: git.Runner = git.default_runner) -> str | None:
    """``owner/repo`` from ``path``'s ``origin`` remote, or None (not a repo / not GitHub)."""
    code, out = runner(["git", "-C", path, "remote", "get-url", "origin"])
    return parse_remote(out.strip()) if code == 0 else None


def discover_repos(cwds: Iterable[str], configured: Iterable[str] = (), runner: git.Runner = git.default_runner) -> list[str]:
    """Configured ``owner/repo`` entries plus the GitHub repositories behind ``cwds``, in order, no duplicates."""
    repos: list[str] = []
    for repo in configured:
        if repo and repo not in repos:
            repos.append(repo)
    for cwd in cwds:
        if not cwd:
            continue
        repo = repo_remote(str(cwd), runner)
        if repo and repo not in repos:
            repos.append(repo)
    return repos
