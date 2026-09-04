from datetime import datetime, timezone

from edgeboard.collectors.git import FORMAT, Commit, collect_git, commits_since, parse_log, summarize

SAMPLE = (
    "\x1ea3f91c2\x1fa3f91c2deadbeef\x1f2026-09-04T09:41:17+02:00\x1fJabba\x1ffix: session card flash restarts\n"
    "\n 3 files changed, 40 insertions(+), 8 deletions(-)\n"
    "\x1e7be04d8\x1f7be04d8cafebabe\x1f2026-09-04T09:12:00+02:00\x1fJabba\x1ffeat: pace projection on limits\n"
    "\n 1 file changed, 12 insertions(+)\n"
    "\x1e09cc31e\x1f09cc31e00000000\x1f2026-09-04T08:00:00+02:00\x1fJabba\x1fchore: empty commit\n"
)
BLOG = "\x1ed114f7a\x1fd114f7a0000000\x1f2026-09-04T10:00:00+02:00\x1fJabba\x1fpost: notes\n\n 1 file changed, 5 insertions(+), 1 deletion(-)\n"


def test_parse_log_reads_records_and_shortstat():
    commits = parse_log(SAMPLE, "/home/me/Dashboard")
    assert [c.hash for c in commits] == ["a3f91c2", "7be04d8", "09cc31e"]
    first = commits[0]
    assert first.repo == "Dashboard" and first.path == "/home/me/Dashboard"
    assert first.message == "fix: session card flash restarts" and first.author == "Jabba"
    assert first.ts == "2026-09-04T09:41:17+02:00"
    assert (first.added, first.deleted) == (40, 8)
    assert (commits[1].added, commits[1].deleted) == (12, 0)
    assert (commits[2].added, commits[2].deleted) == (0, 0)  # no shortstat line at all
    assert parse_log("", "/x") == []
    assert parse_log("\x1egarbage without fields\n", "/x") == []


def test_collect_git_maps_cwds_to_repo_roots_and_merges_logs():
    calls = []

    def runner(args):
        calls.append(args)
        if args[3] == "rev-parse":
            cwd = args[2]
            if cwd.startswith("/home/me/Dashboard"):
                return 0, "/home/me/Dashboard\n"
            if cwd == "/home/me/blog":
                return 0, "/home/me/blog\n"
            return 128, ""
        return 0, SAMPLE if args[2] == "/home/me/Dashboard" else BLOG

    since = datetime(2026, 9, 4, 0, 0, tzinfo=timezone.utc)
    commits = collect_git(["/home/me/Dashboard/edgeboard", "/home/me/Dashboard", "/home/me/blog", "/tmp/not-a-repo", ""], since, runner)
    # newest first across repositories; every repository root is logged once
    assert [c.hash for c in commits] == ["d114f7a", "a3f91c2", "7be04d8", "09cc31e"]
    assert [c.repo for c in commits] == ["blog", "Dashboard", "Dashboard", "Dashboard"]
    logs = [a for a in calls if a[3] == "log"]
    assert [a[2] for a in logs] == ["/home/me/Dashboard", "/home/me/blog"]
    assert all("--since=2026-09-04T00:00:00+00:00" in a and "--no-merges" in a and "--shortstat" in a for a in logs)
    assert all(f"--format={FORMAT}" in a for a in logs)
    assert not any(a[2] == "" for a in calls)


def test_collect_git_survives_a_missing_git_binary():
    assert collect_git(["/x"], datetime.now(timezone.utc), lambda args: (127, "")) == []


def test_summarize_caps_the_list_but_counts_everything():
    commits = [Commit(hash=f"{i:07x}", repo="r", path="/r", message=f"c{i}", ts=f"2026-09-04T0{i}:00:00+00:00", author="a", added=10, deleted=i) for i in range(5)]
    s = summarize(commits, limit=3)
    assert s["count"] == 5 and s["added"] == 50 and s["deleted"] == 10
    assert len(s["commits"]) == 3 and s["commits"][0] == commits[0].to_dict()
    assert summarize([], limit=3) == {"commits": [], "count": 0, "added": 0, "deleted": 0}


def test_commits_since_counts_a_sessions_repository_from_its_start():
    commits = [
        Commit("a", "Dashboard", "/home/me/Dashboard", "m", "2026-09-04T09:00:00+00:00", "x", 1, 0),
        Commit("b", "Dashboard", "/home/me/Dashboard", "m", "2026-09-04T10:00:00+00:00", "x", 1, 0),
        Commit("c", "blog", "/home/me/blog", "m", "2026-09-04T12:30:00+02:00", "x", 1, 0),
    ]
    assert commits_since(commits, "/home/me/Dashboard/edgeboard", "2026-09-04T09:30:00+00:00") == 1  # a subdirectory of the repo
    assert commits_since(commits, "/home/me/Dashboard", "2026-09-04T08:00:00+00:00") == 2
    assert commits_since(commits, "/home/me/Dashboards", "2026-09-04T08:00:00+00:00") == 0  # a prefix is not a subdirectory
    assert commits_since(commits, "/home/me/blog", "2026-09-04T10:00:00Z") == 1  # offsets compare as instants
    assert commits_since(commits, "/home/me/blog", None) == 1
    assert commits_since(commits, "", "2026-09-04T08:00:00+00:00") == 0
