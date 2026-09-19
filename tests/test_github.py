from datetime import datetime, timedelta, timezone

import httpx
import pytest

from edgeboard.collectors.github import (
    API_URL,
    GitHubClient,
    GitHubError,
    Run,
    collect_runs,
    discover_repos,
    load_token,
    parse_remote,
    parse_runs,
    summarize_runs,
)

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


def iso(minutes_ago: float = 0.0) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def run(id: int = 1, repo: str = "me/ci", name: str = "ci", branch: str = "main", status: str = "completed", conclusion: str = "failure", started: str = "", updated: str = "") -> Run:
    return Run(
        id=id,
        repo=repo,
        name=name,
        title="",
        branch=branch,
        status=status,
        conclusion=conclusion,
        url="",
        number=1,
        started_at=started or iso(30),
        updated_at=updated or iso(20),
    )


def test_parse_remote_accepts_the_shapes_git_uses():
    assert parse_remote("https://github.com/JabbaKadabra/Edgeboard.git") == "JabbaKadabra/Edgeboard"
    assert parse_remote("https://github.com/JabbaKadabra/Edgeboard") == "JabbaKadabra/Edgeboard"
    assert parse_remote("https://github.com/JabbaKadabra/Edgeboard/") == "JabbaKadabra/Edgeboard"
    assert parse_remote("git@github.com:JabbaKadabra/Edgeboard.git") == "JabbaKadabra/Edgeboard"
    assert parse_remote("ssh://git@github.com/JabbaKadabra/Edgeboard.git") == "JabbaKadabra/Edgeboard"
    assert parse_remote("git://github.com/owner/repo.git") == "owner/repo"
    assert parse_remote("git@gitlab.com:owner/repo.git") is None
    assert parse_remote("https://example.com/owner/repo") is None
    assert parse_remote("") is None and parse_remote("github.com") is None


def test_parse_runs_flattens_the_payload_and_skips_junk():
    payload = {
        "total_count": 3,
        "workflow_runs": [
            {
                "id": 7,
                "name": "ci",
                "display_title": "fix things",
                "head_branch": "main",
                "status": "in_progress",
                "conclusion": None,
                "html_url": "https://github.com/me/ci/actions/runs/7",
                "run_number": 7,
                "run_started_at": "2026-09-19T11:00:00Z",
                "updated_at": "2026-09-19T11:05:00Z",
            },
            "junk",
            {"id": "8", "name": None, "status": "queued", "created_at": "2026-09-19T11:30:00Z", "head_branch": "dev"},
        ],
    }
    runs = parse_runs(payload, "me/ci")
    assert [r.id for r in runs] == [7, 8]
    assert runs[0].to_dict() == {
        "id": 7,
        "repo": "me/ci",
        "name": "ci",
        "title": "fix things",
        "branch": "main",
        "status": "in_progress",
        "conclusion": "",
        "url": "https://github.com/me/ci/actions/runs/7",
        "number": 7,
        "started_at": "2026-09-19T11:00:00Z",
        "updated_at": "2026-09-19T11:05:00Z",
    }
    # queued runs without run_started_at fall back to created_at for both stamps
    assert runs[1].status == "queued" and runs[1].started_at == "2026-09-19T11:30:00Z" and runs[1].updated_at == runs[1].started_at
    assert parse_runs({}, "me/ci") == [] and parse_runs({"workflow_runs": "junk"}, "me/ci") == []


def test_summarize_shows_running_first_then_newest_failures():
    runs = [
        run(id=1, branch="a", status="in_progress", conclusion="", started=iso(5), updated=iso(1)),
        run(id=2, branch="b", status="in_progress", conclusion="", started=iso(12), updated=iso(1)),
        run(id=3, branch="c", conclusion="failure", updated=iso(3)),
        run(id=4, branch="d", conclusion="failure", updated=iso(40)),
    ]
    summary = summarize_runs(runs, NOW)
    assert [r["id"] for r in summary["runs"]] == [2, 1, 3, 4]  # longest-running first, then newest failure first
    assert summary["running"] == 2 and summary["failed"] == 2


def test_summarize_hides_superseded_failures_and_respects_the_window():
    runs = [
        run(id=1, branch="main", conclusion="failure", updated=iso(1500)),  # older than the default day
        run(id=2, branch="dev", status="in_progress", conclusion="", started=iso(2)),  # retrying the branch
        run(id=3, branch="dev", conclusion="failure", updated=iso(30)),  # superseded by id 2
        run(id=4, branch="feat", conclusion="failure", updated=iso(30)),
        run(id=5, branch="feat", conclusion="success", updated=iso(20)),  # supersedes id 4
        run(id=6, name="release", conclusion="cancelled", updated=iso(10)),  # cancelled is not a failure
    ]
    summary = summarize_runs(runs, NOW)
    assert [r["id"] for r in summary["runs"]] == [2]
    assert summarize_runs([run(id=7, conclusion="failure", updated=iso(60))], NOW, failed_seconds=3600)["failed"] == 1
    assert summarize_runs([run(id=8, conclusion="failure", updated=iso(120))], NOW, failed_seconds=3600)["failed"] == 0


def test_summarize_counts_only_the_rows_it_shows():
    runs = [run(id=i, branch=f"b{i}", conclusion="failure", updated=iso(i)) for i in range(1, 12)]
    summary = summarize_runs(runs, NOW, limit=3)
    assert [r["id"] for r in summary["runs"]] == [1, 2, 3] and summary["failed"] == 3


def test_load_token_prefers_the_environment_then_reads_ghs_file(tmp_path):
    (tmp_path / "hosts.yml").write_text(
        "github.com:\n"
        "    users:\n"
        "        me:\n"
        "            oauth_token: old-token\n"
        "    git_protocol: https\n"
        "    oauth_token: active-token\n"
        "    user: me\n"
        "gitlab.com:\n"
        "    oauth_token: other\n"
    )
    assert load_token(explicit="from-env", env={}, config_dir=tmp_path) == "from-env"
    assert load_token(env={"GH_TOKEN": "gh-env"}, config_dir=tmp_path) == "gh-env"
    assert load_token(env={}, config_dir=tmp_path) == "active-token"  # the last github.com token wins
    assert load_token(env={}, config_dir=tmp_path / "nope") == ""
    other = tmp_path / "no-github"
    other.mkdir()
    (other / "hosts.yml").write_text("gitlab.com:\n    oauth_token: other\n")
    assert load_token(env={}, config_dir=other) == ""


def test_load_token_follows_gh_config_dir(tmp_path):
    (tmp_path / "hosts.yml").write_text("github.com:\n  oauth_token: t\n")
    assert load_token(env={"GH_CONFIG_DIR": str(tmp_path)}) == "t"


def test_client_fetches_runs_with_the_token():
    def handler(request: httpx.Request):
        assert request.headers["Authorization"] == "Bearer tok"
        assert str(request.url).startswith(f"{API_URL}/repos/me/ci/actions/runs")
        assert "per_page=30" in str(request.url)
        return httpx.Response(200, json={"workflow_runs": [{"id": 1, "name": "ci", "head_branch": "main", "status": "completed", "conclusion": "failure", "run_started_at": "2026-09-19T11:00:00Z", "updated_at": "2026-09-19T11:05:00Z"}]})

    client = GitHubClient("tok", http=httpx.Client(transport=httpx.MockTransport(handler)))
    runs = client.runs("me/ci")
    assert [r.id for r in runs] == [1] and runs[0].repo == "me/ci"


def test_client_maps_status_codes_to_github_errors():
    for code, match in ((401, "gh auth login"), (403, "403"), (404, "404")):
        client = GitHubClient("tok", http=httpx.Client(transport=httpx.MockTransport(lambda r, c=code: httpx.Response(c))))
        with pytest.raises(GitHubError, match=match):
            client.runs("me/ci")


def test_collect_runs_keeps_the_repositories_that_work():
    class Client:
        def runs(self, repo):
            if repo == "me/broken":
                raise GitHubError("HTTP 404 (no access, or the repository was renamed)")
            return [run(id=1, repo=repo)]

    runs, errors = collect_runs(Client(), ["me/ok", "me/broken", "me/ok2"])
    assert [r.repo for r in runs] == ["me/ok", "me/ok2"]
    assert errors == ["me/broken: HTTP 404 (no access, or the repository was renamed)"]


def test_discover_repos_reads_the_origin_remote():
    remotes = {"/work/edge": "git@github.com:JabbaKadabra/Edgeboard.git", "/work/blog": "https://gitlab.com/me/blog.git"}

    def runner(args):
        cwd = args[2]
        return (0, remotes.get(cwd, "")) if cwd in remotes else (1, "")

    repos = discover_repos(["/work/edge", "/work/blog", "/work/none"], ("me/extra", "me/extra"), runner)
    assert repos == ["me/extra", "JabbaKadabra/Edgeboard"]
