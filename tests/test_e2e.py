"""End-to-end runs of the CLI against a local git "remote", a fake gh and a fake claude."""

import json
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
            "login": "me", "logins": {"bot-token": "rc-bot"},
        }))

    def new_head(self, content):
        sha = self.push_pr(1, "feature", content=content)
        self.prs[0]["head"]["sha"] = sha
        self.write_gh()
        return sha

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
    assert call["argv"][:2] == ["-p", "/code-review 1 medium --comment"]
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
    assert env.calls()[-1]["argv"][1] == "/code-review 1 medium --comment"


def test_watch_reviews_new_heads_once(env):
    assert env.run("watch", "--once").exit_code == 0
    assert [c["argv"][1] for c in env.calls()] == ["/code-review 1 medium --comment"]  # draft #2 skipped

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


def use_app(env, app_api, monkeypatch, **extra):
    monkeypatch.delenv("RC_TOK")  # the sample-style GH_TOKEN = "${RC_TOK}" no longer resolves
    lines = "\n".join(f"{k} = {json.dumps(v)}" for k, v in extra.items())
    env.write_config(top=f'[github_app]\napp_id = 123456\nprivate_key = "{app_api.key}"\n'
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


def test_clean_and_init(env, tmp_path):
    cfg = tmp_path / "new" / "config.toml"
    res = CliRunner().invoke(cli, ["-c", str(cfg), "init"])
    assert res.exit_code == 0 and "[[repos]]" in cfg.read_text()
    assert CliRunner().invoke(cli, ["-c", str(cfg), "init"]).exit_code != 0
    assert env.run("clean").exit_code == 0
