"""End-to-end runs of the CLI against a local git "remote", a fake gh and a fake claude."""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from rocket_critic.cli import cli, parse_pr_ref
from rocket_critic.config import load
from rocket_critic.state import ATTENTION, DONE, FAILED, PENDING, PHASE_REVIEW, SKIPPED, State

FAKES = Path(__file__).parent / "fakes"


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Env:
    def __init__(self, tmp: Path, monkeypatch):
        self.tmp = tmp
        self.src = tmp / "src"
        self.remote = tmp / "remotes" / "o" / "r.git"
        self.workdir = tmp / "work"
        self.gh_data = tmp / "gh.json"
        self.claude_log = tmp / "claude.log"

        self.src.mkdir()
        git("init", "-q", "-b", "main", cwd=self.src)
        git("config", "user.email", "t@example.com", cwd=self.src)
        git("config", "user.name", "t", cwd=self.src)
        (self.src / "app.py").write_text("print('hi')\n")
        git("add", ".", cwd=self.src)
        git("commit", "-qm", "base", cwd=self.src)
        self.remote.parent.mkdir(parents=True)
        git("clone", "-q", "--bare", str(self.src), str(self.remote), cwd=tmp)
        self.pr_sha = self.push_pr(1, "feature")

        monkeypatch.setenv("PATH", f"{FAKES}:{__import__('os').environ['PATH']}")
        monkeypatch.setenv("FAKE_GH_DATA", str(self.gh_data))
        monkeypatch.setenv("FAKE_CLAUDE_LOG", str(self.claude_log))
        # Point https://github.com/ at the local remotes directory.
        monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.file://{tmp / 'remotes'}/.insteadOf")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/")
        monkeypatch.setenv("GIT_CONFIG_KEY_1", "protocol.file.allow")
        monkeypatch.setenv("GIT_CONFIG_VALUE_1", "always")
        monkeypatch.setenv("RC_TOK", "bot-token")
        monkeypatch.delenv("GH_TOKEN", raising=False)

        self.prs = [self.pr(1, self.pr_sha), self.pr(2, self.pr_sha, draft=True)]
        self.reviews = []
        self.comments = []
        self.timeline = []
        self.write_gh()
        self.config = tmp / "config.toml"
        self.write_config()

    def write_config(self, review="", repo="", top=""):
        self.config.write_text(f"""
workdir = "{self.workdir}"
poll_interval = 60
{top}
[claude]
model = "glm-5.3:cloud"
[claude.env]
ANTHROPIC_BASE_URL = "http://localhost:11434"
GH_TOKEN = "${{RC_TOK}}"
[review]
{review}
[[repos]]
name = "o/r"
{repo}
""")

    def push_pr(self, number, branch, content="print('changed')\n"):
        git("checkout", "-q", "-B", branch, "main", cwd=self.src)
        (self.src / "app.py").write_text(content)
        git("commit", "-qam", f"change {content!r}", cwd=self.src)
        sha = git("rev-parse", "HEAD", cwd=self.src)
        git("push", "-q", "-f", str(self.remote), f"{sha}:refs/pull/{number}/head", cwd=self.src)
        return sha

    @staticmethod
    def pr(number, sha, **kw):
        data = {"number": number, "title": f"PR {number}", "html_url": f"https://github.com/o/r/pull/{number}",
                "head": {"sha": sha}, "base": {"ref": "main"}, "draft": False, "labels": [],
                "requested_reviewers": [], "requested_teams": [], "user": {"login": "alice"},
                "additions": 1, "deletions": 1}
        data.update(kw)
        return data

    def write_gh(self):
        self.gh_data.write_text(json.dumps({
            "prs": self.prs, "reviews": self.reviews, "comments": self.comments, "timeline": self.timeline,
            "issue_comments": getattr(self, "issue_comments", []),
            "reactions": getattr(self, "reactions", []), "removed_labels": getattr(self, "removed_labels", []),
            "login": "me", "logins": {"bot-token": "rc-bot"},
            "releases": getattr(self, "releases", []), "asset_files": getattr(self, "asset_files", {}),
        }))

    def new_head(self, content):
        sha = self.push_pr(1, "feature", content=content)
        self.prs[0]["head"]["sha"] = sha
        self.write_gh()
        return sha

    def reload_gh(self):
        """Pick up changes the fake gh made (removed labels, reactions)."""
        data = json.loads(self.gh_data.read_text())
        self.prs, self.reactions = data["prs"], data.get("reactions", [])
        self.removed_labels = data.get("removed_labels", [])
        return data

    def extend_head(self, content):
        """Add a commit on top of the PR's current head (a normal follow-up push)."""
        git("checkout", "-q", "feature", cwd=self.src)
        (self.src / "app.py").write_text(content)
        git("commit", "-qam", "follow-up", cwd=self.src)
        sha = git("rev-parse", "HEAD", cwd=self.src)
        git("push", "-q", "-f", str(self.remote), f"{sha}:refs/pull/1/head", cwd=self.src)
        self.prs[0]["head"]["sha"] = sha
        self.write_gh()
        return sha

    def age(self, sha, seconds):
        """Pretend the state row for ``sha`` was last touched ``seconds`` ago."""
        st = self.state()
        with st.db:
            st.db.execute("UPDATE reviews SET updated_at = updated_at - ?, started_at = started_at - ? "
                          "WHERE head_sha = ?", (seconds, seconds, sha))

    def gh_calls(self):
        log = Path(str(self.gh_data) + ".log")
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def run(self, *args):
        return CliRunner().invoke(cli, ["-c", str(self.config), *args], catch_exceptions=False)

    def calls(self):
        if not self.claude_log.exists():
            return []
        return [json.loads(line) for line in self.claude_log.read_text().splitlines()]

    def state(self):
        return State(self.workdir / "state.db")


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def test_parse_pr_ref():
    assert parse_pr_ref("o/r#5") == ("o/r", 5)
    assert parse_pr_ref("https://github.com/o/r.x/pull/12") == ("o/r.x", 12)


def test_review_one_pr(env):
    res = env.run("review", "o/r#1")
    assert res.exit_code == 0, res.output
    assert "posted 1 inline comment" in res.output

    (call,) = env.calls()
    assert call["argv"][0] == "-p" and call["argv"][1].splitlines()[0] == "/code-review 1 medium --comment"
    # claude may write only to this review's scratch directory, which is cleaned up afterwards
    scratch = Path(call["argv"][call["argv"].index("--add-dir") + 1])
    assert scratch.parent == env.workdir / "runs" / "o" / "r" / "1" and scratch.name.startswith("scratch-")
    assert f"Edit(/{scratch}/**)" in call["argv"][call["argv"].index("--allowedTools") + 1].split(",")
    assert f"Write tool to a new file in {scratch}/" in call["argv"][1]
    assert "`git diff origin/main...HEAD`" in call["argv"][1]
    assert not scratch.exists()
    assert call["head"] == env.pr_sha
    assert Path(call["cwd"]).is_relative_to(env.workdir / "prs" / "o" / "r")
    assert call["env"]["ANTHROPIC_BASE_URL"] == "http://localhost:11434"
    assert call["env"]["GH_TOKEN"] == "bot-token"
    assert call["env"]["GH_REPO"] == "o/r"
    assert not Path(call["cwd"]).exists(), "worktree should be removed after review"

    rec = env.state().get("o/r", 1, env.pr_sha)
    assert rec.status == DONE and rec.num_turns == 7
    assert Path(rec.output_path).read_text().startswith("Found 1 issue")

    # Already reviewed: refused without --force.
    res = env.run("review", "o/r#1")
    assert res.exit_code != 0 and "already reviewed at" in res.output
    res = env.run("review", "o/r#1", "--force", "--no-comment", "--level", "high")
    assert res.exit_code == 0, res.output
    prompt = env.calls()[-1]["argv"][1]
    assert prompt.splitlines()[0] == "/code-review 1 high"
    assert "already reviewed; it is being reviewed again in full" in prompt
    res = env.run("review", "o/r#1", "--force", "--full")
    prompt = env.calls()[-1]["argv"][1]
    assert prompt.splitlines()[0] == "/code-review 1 medium --comment" and "Re-review context" not in prompt


def test_watch_reviews_new_heads_once(env):
    assert env.run("watch", "--once").exit_code == 0
    assert [c["argv"][1].splitlines()[0] for c in env.calls()] == ["/code-review 1 medium --comment"]  # draft #2 skipped

    assert env.run("watch", "--once").exit_code == 0
    assert len(env.calls()) == 1

    new_sha = env.new_head("print('again')\n")
    assert env.run("watch", "--once").exit_code == 0
    assert len(env.calls()) == 2 and env.calls()[-1]["head"] == new_sha

    out = env.run("status").output
    assert out.count("o/r#1") == 2


def test_watch_skips_pr_already_reviewed_on_github(env):
    env.reviews = [{"user": {"login": "rc-bot"}, "commit_id": env.pr_sha}]
    env.write_gh()
    assert env.run("watch", "--once").exit_code == 0
    assert env.calls() == []
    assert env.state().get("o/r", 1, env.pr_sha).status == DONE


def test_failed_review_is_recorded(env, monkeypatch):
    result = env.tmp / "err.json"
    result.write_text(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                                  "result": "API Error: connection refused",
                                  "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "gh x"}}]}))
    monkeypatch.setenv("FAKE_CLAUDE_RESULT", str(result))
    monkeypatch.setenv("FAKE_CLAUDE_EXIT", "1")
    res = env.run("review", "o/r#1")
    assert res.exit_code != 0 and "connection refused" in res.output
    rec = env.state().get("o/r", 1, env.pr_sha)
    assert rec.status == FAILED and rec.attempts == 1


def test_large_pr_is_skipped_once_per_commit(env):
    env.prs[0]["additions"] = 9000
    env.write_gh()
    assert env.run("watch", "--once").exit_code == 0
    assert env.calls() == []
    rec = env.state().get("o/r", 1, env.pr_sha)
    assert rec.status == SKIPPED and "too large" in rec.error

    before = len(env.gh_calls())
    assert env.run("watch", "--once").exit_code == 0
    assert [c for c in env.gh_calls()[before:] if "pulls?" not in " ".join(c)] == []  # only the PR list


def test_rereview_gets_range_and_earlier_threads(env):
    first = env.pr_sha
    assert env.run("watch", "--once").exit_code == 0
    assert "Re-review" not in env.calls()[0]["argv"][1]

    env.comments = [
        {"id": 10, "path": "app.py", "line": 1, "user": {"login": "rc-bot"}, "body": "Prints the wrong thing."},
        {"id": 11, "in_reply_to_id": 10, "path": "app.py", "user": {"login": "alice"}, "body": "Intentional."},
        {"id": 12, "path": "app.py", "line": 1, "user": {"login": "carol"}, "body": "Human nit."},
    ]
    env.write_gh()
    env.extend_head("print('changed')\nprint('more')\n")
    assert env.run("watch", "--once").exit_code == 0
    prompt = env.calls()[-1]["argv"][1]
    assert prompt.splitlines()[0] == "/code-review 1 medium --comment"
    assert f"`git diff {first} HEAD`" in prompt
    assert "- app.py:1 (rc-bot): Prints the wrong thing." in prompt
    assert "  - reply (alice): Intentional." in prompt
    assert "Human nit" not in prompt  # only threads started by the posting identity


def test_rereview_after_force_push_reviews_everything(env):
    assert env.run("watch", "--once").exit_code == 0
    first = env.pr_sha
    env.new_head("print('rewritten')\n")  # push_pr rebuilds the branch from main: not a descendant
    assert env.run("watch", "--once").exit_code == 0
    prompt = env.calls()[-1]["argv"][1]
    assert "history was rewritten" in prompt and f"git diff {first}" not in prompt


def test_review_limit_per_pr(env):
    env.write_config("max_reviews_per_pr = 2")
    assert env.run("watch", "--once").exit_code == 0
    env.new_head("print(2)\n")
    assert env.run("watch", "--once").exit_code == 0
    third = env.new_head("print(3)\n")
    assert env.run("watch", "--once").exit_code == 0
    assert len(env.calls()) == 2
    res = env.run("review", "o/r#1")
    assert res.exit_code != 0 and "review limit reached" in res.output
    assert env.run("review", "o/r#1", "--force").exit_code == 0
    assert env.calls()[-1]["head"] == third


def test_settle_delay(env):
    env.write_config("settle_seconds = 120")
    assert env.run("watch", "--once").exit_code == 0
    assert env.calls() == []
    assert env.state().get("o/r", 1, env.pr_sha).status == PENDING
    assert env.run("watch", "--once").exit_code == 0
    assert env.calls() == []  # still settling
    newer = env.extend_head("print('newer')\n")  # a push while settling restarts the wait
    assert env.run("watch", "--once").exit_code == 0
    assert env.state().get("o/r", 1, env.pr_sha) is None
    env.age(newer, 121)
    assert env.run("watch", "--once").exit_code == 0
    assert [c["head"] for c in env.calls()] == [newer]


def failing_claude(env, monkeypatch, post):
    result = env.tmp / "err.json"
    result.write_text(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                                  "result": "API Error: stream closed"}))
    monkeypatch.setenv("FAKE_CLAUDE_RESULT", str(result))
    monkeypatch.setenv("FAKE_CLAUDE_EXIT", "1")
    if post:
        monkeypatch.setenv("FAKE_CLAUDE_POST", "1")


def test_failure_after_posting_is_not_retried(env, monkeypatch):
    failing_claude(env, monkeypatch, post=True)
    assert env.run("watch", "--once").exit_code == 0
    assert env.state().get("o/r", 1, env.pr_sha).status == FAILED
    env.age(env.pr_sha, 3600)
    assert env.run("watch", "--once").exit_code == 0
    assert len(env.calls()) == 1
    rec = env.state().get("o/r", 1, env.pr_sha)
    assert rec.status == ATTENTION and "posting" in rec.error
    res = env.run("review", "o/r#1")
    assert res.exit_code != 0 and "(attention at" in res.output


def test_failure_without_posting_is_retried(env, monkeypatch):
    failing_claude(env, monkeypatch, post=False)
    assert env.run("watch", "--once").exit_code == 0
    env.age(env.pr_sha, 3600)
    monkeypatch.delenv("FAKE_CLAUDE_RESULT")
    monkeypatch.delenv("FAKE_CLAUDE_EXIT")
    assert env.run("watch", "--once").exit_code == 0
    assert len(env.calls()) == 2
    rec = env.state().get("o/r", 1, env.pr_sha)
    assert rec.status == DONE and rec.attempts == 2


def test_interrupted_run_that_posted_needs_attention(env, monkeypatch):
    st = env.state()
    st.start("o/r", 1, env.pr_sha)
    st.set_phase("o/r", 1, env.pr_sha, PHASE_REVIEW)
    started = st.get("o/r", 1, env.pr_sha).started_at
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started + 5))
    env.reviews = [{"user": {"login": "rc-bot"}, "commit_id": env.pr_sha, "submitted_at": stamp}]
    env.write_gh()
    env.age(env.pr_sha, 3600)  # past the retry backoff once reset to failed
    assert env.run("watch", "--once").exit_code == 0
    assert env.calls() == []
    assert env.state().get("o/r", 1, env.pr_sha).status == ATTENTION


def test_init_workdir_then_watch_with_it(env, tmp_path):
    wd = tmp_path / "wd"
    run = lambda *a: CliRunner().invoke(cli, ["-w", str(wd), *a], catch_exceptions=False)  # noqa: E731
    res = run("init", "--repo", "o/r")
    assert res.exit_code == 0, res.output
    assert 'workdir = "."' in (wd / "config.toml").read_text()
    for d in ("repos/o", "prs/o/r", "runs/o/r"):
        assert (wd / d).is_dir(), d
    assert run("init", "--repo", "bad").exit_code != 0
    assert run("init").exit_code != 0  # exists

    assert run("watch", "--once").exit_code == 0
    assert len(env.calls()) == 1
    assert Path(env.calls()[0]["cwd"]).is_relative_to(wd / "prs" / "o" / "r")
    assert (wd / "repos" / "o" / "r.git" / "HEAD").exists()
    assert State(wd / "state.db").get("o/r", 1, env.pr_sha).status == DONE
    assert "o/r#1" in run("status").output

    monkey_env = {"ROCKET_CRITIC_WORKDIR": str(wd)}
    res = CliRunner().invoke(cli, ["status"], env=monkey_env, catch_exceptions=False)
    assert "o/r#1" in res.output


def request_review(env, login="rc-bot", at=None):
    """Request a review from ``login`` now (or at ``at``), as GitHub records it.

    A re-request made "now" ages earlier reviews by a few seconds first, since
    GitHub timestamps have one-second resolution.
    """
    if at is None:
        for rec in env.state().recent("o/r"):
            env.age(rec.head_sha, 5)
        at = time.time()
    env.prs[0]["requested_reviewers"] = [{"login": login}]
    env.timeline.append({"event": "review_requested", "requested_reviewer": {"login": login},
                         "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))})
    env.write_gh()


def clear_request(env):
    """What GitHub does when the requested reviewer posts a review."""
    env.prs[0]["requested_reviewers"] = []
    env.write_gh()


def test_reviewer_trigger_rereviews_only_when_rerequested(env):
    env.write_config(repo='reviewers = ["rc-bot"]')
    assert env.run("watch", "--once").exit_code == 0
    assert env.calls() == []  # nobody asked

    request_review(env, at=time.time() - 60)
    assert env.run("watch", "--once").exit_code == 0
    assert len(env.calls()) == 1
    clear_request(env)

    first = env.pr_sha
    env.extend_head("print('fix')\n")  # new commits alone don't trigger a review
    assert env.run("watch", "--once").exit_code == 0
    assert len(env.calls()) == 1

    request_review(env)  # author clicks "Re-request review"
    assert env.run("watch", "--once").exit_code == 0
    assert len(env.calls()) == 2
    assert f"`git diff {first} HEAD`" in env.calls()[-1]["argv"][1]


def test_rerequest_without_new_commits_reviews_again_once(env):
    env.write_config(repo='reviewers = ["rc-bot"]')
    request_review(env, at=time.time() - 60)
    assert env.run("watch", "--once").exit_code == 0
    clear_request(env)

    request_review(env)  # same commit, asked again
    assert env.run("watch", "--once").exit_code == 0
    assert len(env.calls()) == 2
    assert "being reviewed again in full" in env.calls()[-1]["argv"][1]
    rec = env.state().get("o/r", 1, env.pr_sha)
    assert rec.status == DONE and rec.done_count == 2 and rec.attempts == 1

    # The fake never clears the request, like a review that posted nothing:
    # the request predates the last review, so it must not loop.
    assert env.run("watch", "--once").exit_code == 0
    assert len(env.calls()) == 2


def test_uncleared_request_does_not_loop(env):
    env.write_config(repo='reviewers = ["rc-bot"]')
    request_review(env, at=time.time() - 60)
    for _ in range(3):
        assert env.run("watch", "--once").exit_code == 0
    assert len(env.calls()) == 1


def test_rerequests_count_toward_review_limit(env):
    env.write_config(review="max_reviews_per_pr = 2", repo='reviewers = ["rc-bot"]')
    request_review(env, at=time.time() - 60)
    assert env.run("watch", "--once").exit_code == 0
    request_review(env)
    assert env.run("watch", "--once").exit_code == 0
    request_review(env)
    assert env.run("watch", "--once").exit_code == 0
    assert len(env.calls()) == 2  # limit of 2 reached


def test_rerequest_with_rereview_on_push_disabled(env):
    env.write_config(review="rereview_on_push = false", repo='reviewers = ["rc-bot"]')
    request_review(env, at=time.time() - 60)
    assert env.run("watch", "--once").exit_code == 0
    env.extend_head("print('fix')\n")
    assert env.run("watch", "--once").exit_code == 0  # still requested, but not re-requested
    assert len(env.calls()) == 1
    request_review(env)
    assert env.run("watch", "--once").exit_code == 0
    assert len(env.calls()) == 2


def test_add_repo(env):
    res = env.run("add-repo", "o/two", "--reviewer", "rc-bot", "--label", "ai-review", "--drafts")
    assert res.exit_code == 0, res.output
    assert "reachable, 2 open PR(s)" in res.output
    text = env.config.read_text()
    assert text.index('name = "o/r"') < text.index('name = "o/two"')
    s = load(env.config)
    two = s.repo("o/two")
    assert two.reviewers == ["rc-bot"] and two.labels == ["ai-review"] and two.skip_drafts is False
    assert two.ignore_authors == ["dependabot[bot]", "renovate[bot]"]
    assert (env.workdir / "prs" / "o" / "two").is_dir()

    res = env.run("add-repo", "O/Two")
    assert res.exit_code != 0 and "already in" in res.output
    res = env.run("add-repo", "not-a-repo")
    assert res.exit_code != 0 and "owner/name" in res.output
    res = env.run("add-repo", "o/missing")
    assert res.exit_code != 0 and "Not Found" in res.output and "o/missing" not in env.config.read_text()
    assert env.run("add-repo", "o/missing", "--no-check").exit_code == 0
    assert [r.name for r in load(env.config).repos] == ["o/r", "o/two", "o/missing"]


def test_add_repo_replaces_sample_placeholder(env, tmp_path):
    wd = tmp_path / "wd"
    run = lambda *a: CliRunner().invoke(cli, ["-w", str(wd), *a], catch_exceptions=False)  # noqa: E731
    assert run("init").exit_code == 0
    res = run("add-repo", "o/r")
    assert res.exit_code == 0 and "replaced the owner/repo placeholder" in res.output
    text = (wd / "config.toml").read_text()
    assert "owner/repo" not in text.split("# Per-repo overrides")[0]
    assert "# Per-repo overrides" in text  # comments kept
    assert [r.name for r in load(wd / "config.toml").repos] == ["o/r"]
    assert run("add-repo", "o/two").exit_code == 0
    assert [r.name for r in load(wd / "config.toml").repos] == ["o/r", "o/two"]


def doctor_line(output, label):
    return next(line for line in output.splitlines() if label in line)


def test_doctor_reports_token_source(env, monkeypatch):
    out = env.run("doctor").output
    assert "[ok  ]" in doctor_line(out, "GitHub token") and "GH_TOKEN from [claude.env] (token)" in out
    assert doctor_line(out, "will post as").endswith("rc-bot")

    monkeypatch.delenv("RC_TOK")  # the configured ${RC_TOK} isn't exported
    out = env.run("doctor").output
    line = doctor_line(out, "GitHub token")
    assert "[FAIL]" in line and "$RC_TOK" in line and "not set" in line
    assert doctor_line(out, "will post as").endswith("me")

    monkeypatch.setenv("RC_TOK", "")  # exported but empty counts as unset
    assert "[FAIL]" in doctor_line(env.run("doctor").output, "GitHub token")


def test_doctor_flags_token_of_own_account(env, monkeypatch):
    monkeypatch.setenv("RC_TOK", "github_pat_mine")  # fake gh maps unknown tokens to the gh login
    out = env.run("doctor").output
    assert "(fine-grained token)" in doctor_line(out, "GitHub token")
    assert "belongs to your own account" in doctor_line(out, "will post as")


def test_watch_warns_about_unset_env_ref(env, monkeypatch, caplog):
    monkeypatch.delenv("RC_TOK")
    assert env.run("watch", "--once").exit_code == 0
    assert "[claude.env] GH_TOKEN skipped: $RC_TOK not set or empty" in caplog.text
    assert "GH_TOKEN" not in env.calls()[0]["env"]


@pytest.fixture
def app_api(tmp_path, monkeypatch):
    from fake_github_api import FakeGitHubAPI

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    api = FakeGitHubAPI(tmp_path)
    yield api
    api.close()


def use_app(env, app_api, monkeypatch, review="", **extra):
    monkeypatch.delenv("RC_TOK")  # the sample-style GH_TOKEN = "${RC_TOK}" no longer resolves
    lines = "\n".join(f"{k} = {json.dumps(v)}" for k, v in extra.items())
    env.write_config(review=review, top=f'[github_app]\napp_id = 123456\nprivate_key = "{app_api.key}"\n'
                         f'api_url = "{app_api.url}"\n{lines}\n')


def test_github_app_token_per_review(env, app_api, monkeypatch, caplog):
    use_app(env, app_api, monkeypatch)
    assert env.run("watch", "--once").exit_code == 0
    (call,) = env.calls()
    assert call["env"]["GH_TOKEN"] == "ghs_test1"
    assert app_api.tokens_issued == [{"path": "/app/installations/42/access_tokens",
                                      "body": {"repositories": ["r"]}, "token": "ghs_test1"}]
    assert "GH_TOKEN skipped" not in caplog.text  # expected when the app provides the token

    env.extend_head("print('again')\n")
    assert env.run("watch", "--once").exit_code == 0
    assert env.calls()[-1]["env"]["GH_TOKEN"] == "ghs_test2"  # a fresh token every review


def test_github_app_identity_used_for_dedupe_and_threads(env, app_api, monkeypatch):
    use_app(env, app_api, monkeypatch)
    env.reviews = [{"user": {"login": "rocket-critic[bot]"}, "commit_id": env.pr_sha}]
    env.write_gh()
    assert env.run("watch", "--once").exit_code == 0
    assert env.calls() == []  # recognised as already reviewed by the app
    assert app_api.tokens_issued == []


def test_explicit_token_beats_github_app(env, app_api, monkeypatch):
    use_app(env, app_api, monkeypatch)
    monkeypatch.setenv("RC_TOK", "bot-token")
    assert env.run("watch", "--once").exit_code == 0
    assert env.calls()[0]["env"]["GH_TOKEN"] == "bot-token" and app_api.tokens_issued == []


def test_github_app_not_installed_fails_safely(env, app_api, monkeypatch):
    use_app(env, app_api, monkeypatch)
    del app_api.installations["o/r"]
    assert env.run("watch", "--once").exit_code == 0
    assert env.calls() == []
    rec = env.state().get("o/r", 1, env.pr_sha)
    assert rec.status == FAILED and "not installed on o/r" in rec.error
    assert rec.phase == "checkout"  # nothing was posted, so a retry is safe


def test_doctor_with_github_app(env, app_api, monkeypatch):
    use_app(env, app_api, monkeypatch)
    out = env.run("doctor").output
    assert "[ok  ]" in doctor_line(out, "GitHub App 123456") and "rocket-critic[bot]" in out
    assert "installation 42, token issued" in doctor_line(out, "GitHub App on o/r")
    assert "[ok  ]" in doctor_line(out, "GitHub token for /code-review")
    assert doctor_line(out, "will post as").endswith("rocket-critic[bot]")

    app_api.slug, app_api.app_id = "x", "999"  # wrong app id: the JWT is rejected
    out = env.run("doctor").output
    assert "[FAIL]" in doctor_line(out, "GitHub App 123456") and "HTTP 401" in out


def test_doctor_flags_placeholder_repo(env, tmp_path):
    wd = tmp_path / "wd"
    CliRunner().invoke(cli, ["-w", str(wd), "init"], catch_exceptions=False)
    out = CliRunner().invoke(cli, ["-w", str(wd), "doctor"], catch_exceptions=False).output
    assert "sample placeholder" in doctor_line(out, "repo owner/repo") and "add-repo" in out
    assert "list PRs in owner/repo" not in out
    assert f"config: {wd.resolve() / 'config.toml'}" in out


def stamp(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def add_label(env, name="ai-review", at=None):
    """Label PR #1 now (or at ``at``), as GitHub records it; ages earlier reviews like request_review."""
    if at is None:
        for rec in env.state().recent("o/r"):
            env.age(rec.head_sha, 5)
        at = time.time()
    env.reload_gh()
    env.prs[0]["labels"] = [{"name": name}]
    env.timeline.append({"event": "labeled", "label": {"name": name}, "created_at": stamp(at)})
    env.write_gh()


def test_request_label_mode(env):
    env.write_config(repo='labels = ["ai-review"]\nlabel_mode = "request"')
    assert env.run("watch", "--once").exit_code == 0
    assert env.calls() == []  # not labelled

    add_label(env, at=time.time() - 60)
    assert env.run("watch", "--once").exit_code == 0
    assert len(env.calls()) == 1
    env.reload_gh()
    assert env.prs[0]["labels"] == []  # taken off after the review...
    assert env.removed_labels == [{"pr": 1, "label": "ai-review", "token": "bot-token"}]  # ...as the poster

    first = env.pr_sha
    env.extend_head("print('fix')\n")
    assert env.run("watch", "--once").exit_code == 0
    assert len(env.calls()) == 1  # a push alone doesn't ask for a review

    add_label(env)  # asking again after new commits
    assert env.run("watch", "--once").exit_code == 0
    assert len(env.calls()) == 2 and f"`git diff {first} HEAD`" in env.calls()[-1]["argv"][1]

    add_label(env)  # and again on the same commit
    assert env.run("watch", "--once").exit_code == 0
    assert len(env.calls()) == 3 and "being reviewed again in full" in env.calls()[-1]["argv"][1]


def test_request_label_left_behind_does_not_loop(env):
    env.write_config(repo='labels = ["ai-review"]\nlabel_mode = "request"')
    add_label(env, at=time.time() - 60)
    assert env.run("watch", "--once").exit_code == 0
    env.reload_gh()
    env.prs[0]["labels"] = [{"name": "ai-review"}]  # removal failed: label still there, no new labeled event
    env.write_gh()
    for _ in range(2):
        assert env.run("watch", "--once").exit_code == 0
    assert len(env.calls()) == 1


def comment(env, body, cid, login="alice", association="OWNER", at=None, pr=1):
    env.reload_gh()
    env.issue_comments = getattr(env, "issue_comments", []) + [{
        "id": cid, "body": body, "user": {"login": login}, "author_association": association,
        "issue_url": f"https://api.github.com/repos/o/r/issues/{pr}",
        "created_at": stamp(time.time() + 1 if at is None else at),
    }]
    env.write_gh()


def test_comment_command(env):
    env.write_config(repo='labels = ["never"]\ncommands = ["/review", "@rocket-critic review"]')
    comment(env, "/review", 100, at=time.time() - 3600)  # before rocket-critic was watching
    assert env.run("watch", "--once").exit_code == 0
    assert env.calls() == []

    comment(env, "Looks good to me.\n@Rocket-Critic Review please", 101)
    comment(env, "/review", 102, association="NONE")  # no write access
    comment(env, "/review", 103, login="rc-bot")  # the bot's own comment
    assert env.run("watch", "--once").exit_code == 0
    assert len(env.calls()) == 1
    env.reload_gh()
    assert env.reactions == [{"comment_id": 101, "content": "eyes", "token": "bot-token"}]

    assert env.run("watch", "--once").exit_code == 0
    assert len(env.calls()) == 1  # each comment is used once

    comment(env, "/review", 104)  # same commit, asked again
    assert env.run("watch", "--once").exit_code == 0
    assert len(env.calls()) == 2 and "being reviewed again in full" in env.calls()[-1]["argv"][1]


def test_comment_command_on_draft_and_over_limit(env):
    env.write_config(review="max_reviews_per_pr = 1", repo='labels = ["never"]\ncommands = ["/review"]')
    env.prs[1]["head"]["sha"] = env.push_pr(2, "draft-branch", content="print('draft')\n")
    env.write_gh()
    assert env.run("watch", "--once").exit_code == 0  # start watching comments
    comment(env, "/review", 200, pr=2)  # PR #2 is a draft: an explicit request still counts
    assert env.run("watch", "--once").exit_code == 0
    assert len(env.calls()) == 1 and env.calls()[0]["argv"][1].startswith("/code-review 2 ")

    comment(env, "/review", 201, pr=2)  # over max_reviews_per_pr
    assert env.run("watch", "--once").exit_code == 0
    assert len(env.calls()) == 1
    env.reload_gh()
    assert [r["content"] for r in env.reactions] == ["eyes", "confused"]


def test_label_and_reaction_as_github_app(env, app_api, monkeypatch):
    monkeypatch.delenv("RC_TOK")
    env.config.write_text(env.config.read_text().replace(
        "poll_interval = 60",
        f'poll_interval = 60\n[github_app]\napp_id = 123456\nprivate_key = "{app_api.key}"\napi_url = "{app_api.url}"'
    ).replace('name = "o/r"', 'name = "o/r"\nlabels = ["ai-review"]\nlabel_mode = "request"\ncommands = ["/review"]'))
    assert env.run("watch", "--once").exit_code == 0  # start watching comments
    add_label(env, at=time.time() - 60)
    assert env.run("watch", "--once").exit_code == 0
    env.reload_gh()
    assert env.removed_labels[0]["token"].startswith("ghs_test")  # removed as rocket-critic[bot]
    comment(env, "/review", 300)
    assert env.run("watch", "--once").exit_code == 0
    env.reload_gh()
    assert env.reactions[0]["content"] == "eyes" and env.reactions[0]["token"].startswith("ghs_test")


def test_add_repo_request_mode_and_commands(env):
    res = env.run("add-repo", "o/two", "--label", "ai-review", "--label-mode", "request", "--command", "/review")
    assert res.exit_code == 0, res.output
    two = load(env.config).repo("o/two")
    assert (two.labels, two.label_mode, two.commands) == (["ai-review"], "request", ["/review"])


@pytest.mark.parametrize("value, shown", [("90", "90s"), ("30s", "30s"), ("5m", "5m"), ("1h", "1h"), ("120", "2m")])
def test_watch_interval_option(env, caplog, value, shown):
    caplog.set_level("INFO")
    assert env.run("watch", "--once", "--interval", value).exit_code == 0
    assert f"watching o/r every {shown}" in caplog.text


def test_watch_interval_from_env_and_config(env, caplog, monkeypatch):
    caplog.set_level("INFO")
    assert env.run("watch", "--once").exit_code == 0
    assert "watching o/r every 1m" in caplog.text  # poll_interval = 60 in the test config
    monkeypatch.setenv("ROCKET_CRITIC_POLL_INTERVAL", "15m")
    assert env.run("watch", "--once").exit_code == 0
    assert "watching o/r every 15m" in caplog.text


@pytest.mark.parametrize("value", ["5", "0m", "soon", "1d", "-30"])
def test_watch_interval_rejects_bad_values(env, value):
    res = CliRunner().invoke(cli, ["-c", str(env.config), "watch", "--once", "-i", value])
    assert res.exit_code == 2 and "Invalid value for '-i' / '--interval'" in res.output


def wait_for(predicate, timeout=30.0, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def daemon_env(env):
    yield env
    from rocket_critic.daemon import stop
    stop(env.workdir, now=True, timeout=30)  # never leave a watcher behind


def test_daemon_start_reload_stop(daemon_env):
    env = daemon_env
    from rocket_critic.poller import running_pid

    res = env.run("start", "--interval", "10s")
    assert res.exit_code == 0 and "started rocket-critic (pid" in res.output, res.output
    pid = running_pid(env.workdir)
    assert pid and pid != os.getpid()
    assert env.run("start").exit_code != 0  # only one per workdir
    wait_for(lambda: len(env.calls()) == 1, what="first review")
    assert f"watcher: running (pid {pid})" in env.run("status").output

    env.write_config(review='level = "high"')  # config update...
    env.extend_head("print('more')\n")
    res = env.run("reload")  # ...picked up immediately, followed by a poll
    assert res.exit_code == 0 and f"pid {pid}" in res.output
    wait_for(lambda: len(env.calls()) == 2, what="review after reload")
    assert env.calls()[-1]["argv"][1].startswith("/code-review 1 high")

    res = env.run("stop")
    assert res.exit_code == 0 and "stopped" in res.output
    assert running_pid(env.workdir) is None
    assert "watcher: not running" in env.run("status").output
    log = (env.workdir / "rocket-critic.log").read_text()
    assert "received SIGHUP" in log and "config reloaded" in log and "received SIGTERM" in log
    assert env.run("stop").output.startswith("not running")
    assert env.run("reload").exit_code != 0


def test_daemon_start_rejects_bad_config(daemon_env):
    env = daemon_env
    env.config.write_text(env.config.read_text().replace('name = "o/r"', 'name = "o/r"\n[repos.claude]\nbinary = 7'))
    res = env.run("start")
    assert res.exit_code != 0 and "binary must be a string" in res.output


def test_daemon_start_failure_shows_log(tmp_path):
    from rocket_critic import daemon

    with pytest.raises(RuntimeError, match="the watcher exited") as exc:
        daemon.start(["--no-such-option"], tmp_path)
    assert "No such option" in str(exc.value)  # the end of the log is included


def test_watch_reloads_config_between_polls(env, monkeypatch):
    from rocket_critic.config import load as load_config
    from rocket_critic.daemon import ConfigWatcher
    from rocket_critic.poller import Critic

    s = load_config(env.config)
    watcher = ConfigWatcher(s)
    critic = Critic(s)
    polls = []

    def reload(forced):  # runs before each poll
        if len(polls) == 1:  # i.e. after the first poll: edit the config and push
            env.write_config(review='level = "low"')
            os.utime(env.config, ns=(env.config.stat().st_mtime_ns + 10**9,) * 2)
            env.extend_head("print('x')\n")
        new = watcher.check(forced)
        polls.append(new)
        if len(polls) == 3:  # after the second poll
            critic.stop.set()
        return new

    monkeypatch.setattr(critic.wake, "wait", lambda timeout=None: True)  # no sleeping between polls
    critic.watch(watcher.repos(s), reload=reload)
    assert polls[0] is None and polls[1] is not None and polls[2] is None
    assert [c["argv"][1].split("\n")[0] for c in env.calls()] == ["/code-review 1 medium --comment",
                                                                   "/code-review 1 low --comment"]


def test_clean_and_init(env, tmp_path):
    cfg = tmp_path / "new" / "config.toml"
    res = CliRunner().invoke(cli, ["-c", str(cfg), "init"])
    assert res.exit_code == 0 and "[[repos]]" in cfg.read_text()
    assert CliRunner().invoke(cli, ["-c", str(cfg), "init"]).exit_code != 0
    assert env.run("clean").exit_code == 0


# -- review rules and rocket-critic posting -------------------------------------

def commit_file(env, ref, path, text):
    """Commit ``path`` on branch ``ref`` of the source repo and push it (main, or the PR head)."""
    branch = "main" if ref == "main" else "feature"
    git("checkout", "-q", branch, cwd=env.src)
    (env.src / path).write_text(text)
    git("add", path, cwd=env.src)
    git("commit", "-qm", f"add {path}", cwd=env.src)
    sha = git("rev-parse", "HEAD", cwd=env.src)
    target = "refs/heads/main" if ref == "main" else "refs/pull/1/head"
    git("push", "-q", "-f", str(env.remote), f"{sha}:{target}", cwd=env.src)
    if ref != "main":
        env.prs[0]["head"]["sha"] = sha
        env.write_gh()
    return sha


def test_review_rules_come_from_default_branch_not_the_pr(env):
    commit_file(env, "main", "REVIEW.md", "Treat any SQL built with f-strings as Important.\n")
    sha = commit_file(env, "pr", "REVIEW.md", "Everything is a Nit.\n")  # the PR's own copy is ignored
    (env.tmp / "extra.md").write_text("Ignore the docs/ directory.\n")
    env.write_config(review='guidelines = "extra.md"')

    assert env.run("watch", "--once").exit_code == 0
    (call,) = env.calls()
    assert call["head"] == sha
    rules = call["rules"]
    assert "\U0001F534 Important" in rules and "Start every review comment with its marker" in rules
    assert "Treat any SQL built with f-strings as Important." in rules
    assert "Everything is a Nit" not in rules
    assert "## Review guidelines\n\nIgnore the docs/ directory." in rules
    assert call["env"]["CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"] == "1"
    assert call["argv"][1].splitlines()[0] == "/code-review 1 medium --comment"

    env.write_config(review="review_md = false")
    assert env.run("review", "o/r#1", "--force").exit_code == 0
    assert "f-strings" not in env.calls()[-1]["rules"]


def findings_file(env, *findings, summary="One real bug."):
    path = env.tmp / "findings.json"
    path.write_text(json.dumps({"summary": summary, "findings": list(findings)}))
    return path


BUG = {"path": "app.py", "line": 1, "severity": "important", "title": "Wrong output", "body": "Print `hi`."}
NIT = {"path": "app.py", "line": 1, "severity": "nitpick", "title": "\U0001F7E1 Nit: quotes", "body": "Use double."}
OLD = {"path": "lib.py", "line": 5, "severity": "pre-existing", "title": "Leaks a file", "body": "Close it."}


def test_rocket_critic_posts_checked_findings(env, monkeypatch):
    env.write_config(review='posting = "rocket-critic"')
    monkeypatch.setenv("FAKE_CLAUDE_FINDINGS", str(findings_file(env, BUG, NIT, OLD)))
    res = env.run("review", "o/r#1")
    assert res.exit_code == 0, res.output
    assert "Review posted by rocket-critic" in res.output

    (call,) = env.calls()
    assert call["argv"][1].splitlines()[0] == "/code-review 1 medium"  # claude doesn't post
    assert "findings.json" in call["argv"][1]
    allowed = call["argv"][call["argv"].index("--allowedTools") + 1].split(",")
    assert not any(t.startswith("Bash(gh api") for t in allowed)
    assert "Record it as the `severity`" in call["rules"]

    (posted,) = env.reload_gh()["posted_reviews"]
    payload = posted["payload"]
    assert posted["pr"] == 1 and posted["token"] == "bot-token" and payload["commit_id"] == env.pr_sha
    assert [c["body"] for c in payload["comments"]] == [
        "\U0001F534 **Important**: Wrong output\n\nPrint `hi`.",
        "\U0001F7E1 **Nit**: quotes\n\nUse double.",
    ]
    assert all(c["path"] == "app.py" and c["line"] == 1 and c["side"] == "RIGHT" for c in payload["comments"])
    assert "1 \U0001F534 Important · 1 \U0001F7E1 Nit · 1 \U0001F7E3 Pre-existing" in payload["body"]
    assert "One real bug." in payload["body"]
    assert "- \U0001F7E3 **Pre-existing** `lib.py:5`: Leaks a file" in payload["body"]  # not in the diff

    rec = env.state().get("o/r", 1, env.pr_sha)
    assert rec.status == DONE
    saved = json.loads(Path(rec.output_path).with_suffix(".review.json").read_text())
    assert saved["payload"] == payload and len(saved["outside"]) == 1

    # The posted review counts as ours at this commit.
    assert env.run("watch", "--once").exit_code == 0
    assert len(env.calls()) == 1


def test_bad_severity_posts_nothing_and_is_retried(env, monkeypatch):
    env.write_config(review='posting = "rocket-critic"')
    monkeypatch.setenv("FAKE_CLAUDE_FINDINGS", str(findings_file(env, BUG, dict(NIT, severity="medium"))))
    assert env.run("watch", "--once").exit_code == 0
    rec = env.state().get("o/r", 1, env.pr_sha)
    assert rec.status == FAILED and "severity 'medium' is not one of" in rec.error
    assert "posted_reviews" not in env.reload_gh()
    assert Path(rec.output_path).with_suffix(".findings-rejected.json").exists()

    # Nothing was posted, so the retry goes ahead; this time the findings are fine.
    monkeypatch.setenv("FAKE_CLAUDE_FINDINGS", str(findings_file(env, BUG, NIT)))
    env.age(env.pr_sha, 3600)
    assert env.run("watch", "--once").exit_code == 0
    assert env.state().get("o/r", 1, env.pr_sha).status == DONE
    assert len(env.reload_gh()["posted_reviews"]) == 1


def test_missing_findings_file_fails(env):
    env.write_config(review='posting = "rocket-critic"')
    res = env.run("review", "o/r#1")
    assert res.exit_code != 0 and "claude wrote no findings.json" in res.output
    assert "posted_reviews" not in env.reload_gh()


def test_findings_printed_instead_of_written(env, monkeypatch):
    env.write_config(review='posting = "rocket-critic"')
    result = env.tmp / "result.json"
    reply = "Review done.\n```json\n" + json.dumps({"summary": "s", "findings": [BUG]}) + "\n```"
    result.write_text(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": reply}))
    monkeypatch.setenv("FAKE_CLAUDE_RESULT", str(result))
    assert env.run("review", "o/r#1").exit_code == 0
    (posted,) = env.reload_gh()["posted_reviews"]
    assert len(posted["payload"]["comments"]) == 1


def test_nits_held_back_and_rejected_positions(env, monkeypatch):
    env.write_config(review='posting = "rocket-critic"\nmin_severity = "important"')
    monkeypatch.setenv("FAKE_CLAUDE_FINDINGS", str(findings_file(env, BUG, NIT)))
    monkeypatch.setenv("FAKE_GH_REJECT_INLINE", "1")
    assert env.run("review", "o/r#1").exit_code == 0
    (posted,) = env.reload_gh()["posted_reviews"]
    body = posted["payload"]["body"]
    # GitHub refused the inline comment, so it went into the body; the nit wasn't posted at all.
    assert posted["payload"]["comments"] == []
    assert "### Findings\n" in body and "\U0001F534 **Important** `app.py:1`: Wrong output" in body
    assert "quotes" not in body and "1 finding(s) less severe than \U0001F534 Important not posted." in body


def test_no_comment_saves_findings_without_posting(env, monkeypatch):
    env.write_config(review='posting = "rocket-critic"')
    monkeypatch.setenv("FAKE_CLAUDE_FINDINGS", str(findings_file(env, BUG)))
    res = env.run("review", "o/r#1", "--no-comment")
    assert res.exit_code == 0 and "\U0001F534 **Important**: Wrong output" in res.output
    assert "posted_reviews" not in env.reload_gh()


def test_claude_posting_itself_needs_attention(env, monkeypatch):
    env.write_config(review='posting = "rocket-critic"')
    monkeypatch.setenv("FAKE_CLAUDE_FINDINGS", str(findings_file(env, BUG)))
    monkeypatch.setenv("FAKE_CLAUDE_POST", "1")
    assert env.run("watch", "--once").exit_code == 0
    rec = env.state().get("o/r", 1, env.pr_sha)
    assert rec.status == ATTENTION and "claude posted to the PR itself" in rec.error
    assert "posted_reviews" not in env.reload_gh()


def test_rocket_critic_posting_as_github_app(env, app_api, monkeypatch):
    use_app(env, app_api, monkeypatch, review='posting = "rocket-critic"')
    monkeypatch.setenv("FAKE_CLAUDE_FINDINGS", str(findings_file(env, BUG)))
    assert env.run("watch", "--once").exit_code == 0
    claude_token, post_token = app_api.tokens_issued
    # claude gets a read-only token; rocket-critic posts with a normal one.
    assert claude_token["body"]["permissions"] == {"contents": "read", "pull_requests": "read", "metadata": "read"}
    assert env.calls()[0]["env"]["GH_TOKEN"] == claude_token["token"]
    assert "permissions" not in post_token["body"]
    (posted,) = env.reload_gh()["posted_reviews"]
    assert posted["token"] == post_token["token"]
    assert env.state().get("o/r", 1, env.pr_sha).status == DONE


def test_traditional_severities(env, monkeypatch):
    env.write_config(review='posting = "rocket-critic"\nseverities = "traditional"\nmin_severity = "low"')
    monkeypatch.setenv("FAKE_CLAUDE_FINDINGS", str(findings_file(
        env, dict(BUG, severity="critical"), dict(NIT, severity="info"),
        dict(OLD, severity="medium", pre_existing=True))))
    assert env.run("review", "o/r#1").exit_code == 0
    call = env.calls()[-1]
    assert "\U0001F7E0 High:" in call["rules"] and '"critical", "high", "medium", "low" or "info"' in call["argv"][1]
    (posted,) = env.reload_gh()["posted_reviews"]
    payload = posted["payload"]
    assert [c["body"].split("\n")[0] for c in payload["comments"]] == ["\U0001F534 **Critical**: Wrong output"]
    assert "- \U0001F7E1 **Medium** (pre-existing) `lib.py:5`: Leaks a file" in payload["body"]
    assert "1 finding(s) less severe than \U0001F535 Low not posted." in payload["body"]
    assert "1 pre-existing" in payload["body"]


def test_failed_post_is_not_retried_without_inline_comments(env, monkeypatch):
    # Only GitHub refusing a comment's position (HTTP 422) means nothing was posted; any
    # other error may have posted, so the review is not sent again in another form.
    env.write_config(review='posting = "rocket-critic"')
    monkeypatch.setenv("FAKE_CLAUDE_FINDINGS", str(findings_file(env, BUG)))
    monkeypatch.setenv("FAKE_GH_POST_ERROR", "gh: Bad Gateway (HTTP 502)")
    assert env.run("watch", "--once").exit_code == 0
    rec = env.state().get("o/r", 1, env.pr_sha)
    assert rec.status == FAILED and "could not post the review" in rec.error and "HTTP 502" in rec.error
    assert rec.phase == "post"  # the retry first checks whether it was posted after all
    posts = [c for c in env.gh_calls() if "POST" in c and c[3].endswith("/reviews")]
    assert len(posts) == 1


def test_non_json_reply_after_posting_is_fine(env, monkeypatch):
    env.write_config(review='posting = "rocket-critic"')
    monkeypatch.setenv("FAKE_CLAUDE_FINDINGS", str(findings_file(env, BUG)))
    monkeypatch.setenv("FAKE_GH_POST_STDOUT", "not json")
    assert env.run("review", "o/r#1").exit_code == 0
    assert env.state().get("o/r", 1, env.pr_sha).status == DONE


# -- update ---------------------------------------------------------------------

@pytest.fixture
def upd(env, monkeypatch):
    """A fake release feed for `update`, and a fake pip that records what it installs."""
    from rocket_critic import updater

    env.releases, env.asset_files = [], {}
    env.pip_log = env.tmp / "pip.log"
    monkeypatch.setenv("FAKE_PIP_LOG", str(env.pip_log))
    monkeypatch.setattr(updater, "pip_command", lambda: [str(FAKES / "pip")])
    monkeypatch.setattr(updater, "installed_from_source", lambda: None)
    monkeypatch.setattr(updater, "in_pipx", lambda: False)

    def add_release(version, sums="good"):
        """Add a release; the newest one added is "latest"."""
        import hashlib

        wheel = env.tmp / f"rocket_critic-{version}-py3-none-any.whl"
        wheel.write_bytes(f"wheel {version}".encode())
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest() if sums == "good" else "0" * 64
        base = 100 + 10 * len(env.releases)
        assets = [{"name": wheel.name, "id": base}]
        env.asset_files[str(base)] = str(wheel)
        if sums:
            sums_file = env.tmp / f"SHA256SUMS-{version}"
            sums_file.write_text(f"{digest}  {wheel.name}\n{'1' * 64}  rocket-critic.tar.gz\n")
            assets.append({"name": "SHA256SUMS", "id": base + 1})
            env.asset_files[str(base + 1)] = str(sums_file)
        env.releases.insert(0, {"tag_name": f"v{version}", "assets": assets})
        env.write_gh()
        return digest

    env.add_release = add_release
    env.pip_calls = lambda: ([json.loads(x) for x in env.pip_log.read_text().splitlines()]
                             if env.pip_log.exists() else [])
    return env


def test_update_installs_newer_release(upd):
    from rocket_critic import __version__

    digest = upd.add_release("9.0.0")
    res = upd.run("update", "--check")
    assert res.exit_code == 0 and f"rocket-critic 9.0.0 is available (installed: {__version__})" in res.output
    assert upd.pip_calls() == []

    res = upd.run("update")
    assert res.exit_code == 0, res.output
    assert "updated rocket-critic to 9.0.0" in res.output
    (call,) = upd.pip_calls()
    assert call["args"][:2] == ["install", "--upgrade"] and call["args"][-1].endswith("rocket_critic-9.0.0-py3-none-any.whl")
    assert call["sha256"] == digest  # the downloaded wheel, checked against SHA256SUMS
    # Downloads go through the REST asset endpoint, not `gh release download` (GraphQL).
    assert not any(c[:2] == ["release", "download"] for c in upd.gh_calls())


def test_update_when_up_to_date_or_ahead(upd):
    from rocket_critic import __version__

    upd.add_release("0.0.1")
    res = upd.run("update")
    assert "is newer than the latest release (0.0.1); nothing to do" in res.output and upd.pip_calls() == []
    res = upd.run("update", "--version", "0.0.1")  # asked for by name: a downgrade is fine
    assert res.exit_code == 0 and "updated rocket-critic to 0.0.1" in res.output

    upd.add_release(__version__)
    assert f"rocket-critic {__version__} is up to date" in upd.run("update").output
    assert len(upd.pip_calls()) == 1
    assert upd.run("update", "--force").exit_code == 0
    assert upd.pip_calls()[-1]["args"][:4] == ["install", "--upgrade", "--force-reinstall", "--no-deps"]


def test_update_refuses_bad_checksum_and_source_checkout(upd, monkeypatch):
    from rocket_critic import updater

    upd.add_release("9.0.0", sums="bad")
    res = upd.run("update")
    assert res.exit_code != 0 and "does not match SHA256SUMS" in res.output and upd.pip_calls() == []

    upd.add_release("9.0.1")
    monkeypatch.setattr(updater, "installed_from_source", lambda: "/src/rocket-critic")
    res = upd.run("update")
    assert res.exit_code != 0 and "installed from a source checkout (/src/rocket-critic)" in res.output
    assert upd.pip_calls() == []

    res = upd.run("update", "--version", "4.5.6")
    assert res.exit_code != 0 and "could not find release v4.5.6 of sgstreet/rocket-critic" in res.output


def test_update_install_failure_and_unverified_release(upd, monkeypatch):
    upd.add_release("9.0.0", sums=None)  # an old release without SHA256SUMS still installs
    monkeypatch.setenv("FAKE_PIP_EXIT", "1")
    res = upd.run("update")
    assert res.exit_code != 0 and "installing rocket_critic-9.0.0-py3-none-any.whl failed" in res.output


def test_install_command_uses_pipx_for_a_pipx_install(monkeypatch, tmp_path):
    from rocket_critic import updater

    wheel = tmp_path / "rocket_critic-1.0.0-py3-none-any.whl"
    monkeypatch.setattr(updater, "in_pipx", lambda: True)
    monkeypatch.setattr(updater.shutil, "which", lambda name: "/usr/bin/pipx" if name == "pipx" else None)
    assert updater.install_command(wheel) == ["/usr/bin/pipx", "install", "--force", str(wheel)]
    monkeypatch.setattr(updater, "in_pipx", lambda: False)
    assert updater.install_command(wheel)[1:4] == ["-m", "pip", "install"]
    assert updater.tag_version("v1.2.3-rc1") == "1.2.3rc1"
    assert updater.version_key("1.2.3rc1") < updater.version_key("1.2.3") < updater.version_key("1.10.0")


def test_update_restarts_the_watcher(daemon_env, upd):
    from rocket_critic.poller import running_pid

    env = daemon_env
    assert env.run("start", "--interval", "10s").exit_code == 0
    old = running_pid(env.workdir)
    wait_for(lambda: len(env.calls()) == 1, what="first review")

    upd.add_release("9.0.0")
    res = env.run("update")
    assert res.exit_code == 0 and f"the background watcher (pid {old}) still runs" in res.output
    assert running_pid(env.workdir) == old  # only told how to restart

    upd.add_release("9.0.1")
    res = env.run("update", "--restart")
    assert res.exit_code == 0, res.output
    new = running_pid(env.workdir)
    assert new and new != old and f"started rocket-critic (pid {new})" in res.output
    from rocket_critic.updater import started_by_start
    assert started_by_start(new)[-3:] == ["watch", "--interval", "10"]  # started the same way as before


def test_update_leaves_a_watcher_it_did_not_start(upd, tmp_path):
    """A watcher run some other way (systemd unit, terminal) is not restarted by `update`."""
    import subprocess
    import sys

    from rocket_critic.poller import running_pid

    env = upd
    log = open(tmp_path / "fg.log", "w")
    # Like the systemd unit: the rocket-critic script in the foreground, no ROCKET_CRITIC_DAEMON.
    script = [sys.executable, "-c", "import sys; from rocket_critic.cli import main; sys.argv[0] = 'rocket-critic'; main()"]
    child_env = {k: v for k, v in os.environ.items() if k != "ROCKET_CRITIC_DAEMON"}
    child = subprocess.Popen([*script, "-c", str(env.config), "watch", "--interval", "10s"], stdout=log,
                             stderr=subprocess.STDOUT, env=child_env)
    try:
        wait_for(lambda: running_pid(env.workdir) == child.pid, what="foreground watcher")
        upd.add_release("9.0.0")
        res = env.run("update")
        assert res.exit_code == 0 and "restart it the way it was started" in res.output
        upd.add_release("9.0.1")
        res = env.run("update", "--restart")
        assert res.exit_code != 0 and "isn't restarted here" in res.output
        assert running_pid(env.workdir) == child.pid and child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=30)


# -- service ----------------------------------------------------------------------

@pytest.fixture
def svc(env, monkeypatch, tmp_path):
    """Fake systemctl/loginctl, and a private XDG config dir for the unit file."""
    env.systemd_log = tmp_path / "systemd.log"
    monkeypatch.setenv("FAKE_SYSTEMD_LOG", str(env.systemd_log))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    env.unit = tmp_path / "xdg" / "systemd" / "user" / "rocket-critic.service"
    env.systemd_calls = lambda: ([json.loads(x) for x in env.systemd_log.read_text().splitlines()]
                                 if env.systemd_log.exists() else [])
    return env


def test_service_install_status_remove(svc, monkeypatch):
    from rocket_critic import service

    res = svc.run("service", "install", "--interval", "2m")
    assert res.exit_code == 0, res.output
    text = svc.unit.read_text()
    assert text.startswith(service.MARKER)
    exec_start = next(line for line in text.splitlines() if line.startswith("ExecStart="))
    assert exec_start.endswith(f"-c {svc.config} watch --interval 120")
    assert exec_start.split("=", 1)[1].split()[0].endswith("rocket-critic")
    assert f"WorkingDirectory={svc.workdir}" in text
    assert f"Environment={service.quote('PATH=' + os.environ['PATH'], command=False)}" in text
    assert "KillMode=mixed" in text and "WantedBy=default.target" in text
    calls = [c for c in svc.systemd_calls() if c[0] == "systemctl"]
    assert ["systemctl", "--user", "daemon-reload"] in calls
    assert calls.index(["systemctl", "--user", "enable", "rocket-critic.service"]) < \
        calls.index(["systemctl", "--user", "restart", "rocket-critic.service"])
    assert "stops when you log out" in res.output  # not lingering

    monkeypatch.setenv("FAKE_SYSTEMD_MAINPID", "4242")
    monkeypatch.setenv("FAKE_LINGER", "yes")
    out = svc.run("service", "status").output
    assert "rocket-critic: active (pid 4242), enabled" in out and "linger: yes" in out

    res = svc.run("service", "remove")
    assert res.exit_code == 0 and not svc.unit.exists()
    assert ["systemctl", "--user", "disable", "--now", "rocket-critic.service"] in svc.systemd_calls()
    assert "nothing to remove" in svc.run("service", "remove").output
    assert "not installed" in svc.run("service", "status").output


def test_service_install_options_and_foreign_units(svc):
    res = svc.run("service", "install", "--name", "critic-work", "--no-start", "--linger", "--repo", "o/r")
    assert res.exit_code == 0, res.output
    unit = svc.unit.with_name("critic-work.service")
    assert "watch --repo o/r" in unit.read_text()
    calls = svc.systemd_calls()
    assert ["loginctl", "enable-linger", __import__("getpass").getuser()] in calls
    assert not any(c[:3] == ["systemctl", "--user", "restart"] for c in calls)

    svc.unit.parent.mkdir(parents=True, exist_ok=True)
    svc.unit.write_text("[Service]\nExecStart=/bin/true\n")  # someone else's unit
    res = svc.run("service", "install")
    assert res.exit_code != 0 and "wasn't written by `service install`" in res.output
    assert svc.run("service", "remove").exit_code != 0 and svc.unit.exists()
    assert svc.run("service", "install", "--force").exit_code == 0
    assert svc.run("service", "install", "--name", "bad name").exit_code != 0


def test_service_quoting():
    from rocket_critic.service import quote

    assert quote("/usr/bin/rocket-critic") == "/usr/bin/rocket-critic"
    assert quote("/a b/c") == '"/a b/c"'
    assert quote("100%") == "100%%" and quote("$HOME") == "$$HOME"
    assert quote("PATH=/x:$y", command=False) == "PATH=/x:$y"
    assert quote('say "hi"') == '"say \\"hi\\""'


def test_service_install_takes_over_from_start(daemon_env, svc):
    from rocket_critic.poller import running_pid

    env = daemon_env
    assert env.run("start", "--interval", "10s").exit_code == 0
    pid = running_pid(env.workdir)
    res = env.run("service", "install")
    assert res.exit_code == 0, res.output
    assert f"stopping the background watcher (pid {pid})" in res.output
    assert running_pid(env.workdir) is None  # (the fake systemctl doesn't start anything)


def test_update_restarts_the_service(upd, svc, tmp_path, monkeypatch):
    """A watcher run by a unit from `service install` is restarted through systemctl."""
    import subprocess
    import sys

    from rocket_critic.poller import running_pid

    env = upd
    log = open(tmp_path / "svc.log", "w")
    script = [sys.executable, "-c", "import sys; from rocket_critic.cli import main; sys.argv[0] = 'rocket-critic'; main()"]
    child = subprocess.Popen([*script, "-c", str(env.config), "watch", "--interval", "10s"], stdout=log,
                             stderr=subprocess.STDOUT, env={k: v for k, v in os.environ.items()
                                                            if k != "ROCKET_CRITIC_DAEMON"})
    try:
        wait_for(lambda: running_pid(env.workdir) == child.pid, what="service watcher")
        monkeypatch.setenv("FAKE_SYSTEMD_MAINPID", str(child.pid))
        assert env.run("service", "install", "--no-start").exit_code == 0  # it's the service: not stopped
        assert child.poll() is None
        upd.add_release("9.0.0")
        res = env.run("update")
        assert "the rocket-critic service (pid" in res.output and "systemctl --user restart rocket-critic" in res.output
        upd.add_release("9.0.1")
        res = env.run("update", "--restart")
        assert res.exit_code == 0, res.output
        assert ["systemctl", "--user", "restart", "rocket-critic.service"] in env.systemd_calls()
    finally:
        child.terminate()
        child.wait(timeout=30)
