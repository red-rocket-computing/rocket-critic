"""End-to-end runs of the CLI against a local git "remote", a fake gh and a fake claude."""

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from rocket_critic.cli import cli, parse_pr_ref
from rocket_critic.state import DONE, FAILED, State

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
        self.write_gh()
        self.config = tmp / "config.toml"
        self.config.write_text(f"""
workdir = "{self.workdir}"
poll_interval = 60
[claude]
model = "glm-5.3:cloud"
[claude.env]
ANTHROPIC_BASE_URL = "http://localhost:11434"
GH_TOKEN = "${{RC_TOK}}"
[[repos]]
name = "o/r"
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
            "prs": self.prs, "reviews": self.reviews, "login": "me", "logins": {"bot-token": "rc-bot"},
        }))

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
    assert res.exit_code != 0 and "already done" in res.output
    res = env.run("review", "o/r#1", "--force", "--no-comment", "--level", "high")
    assert res.exit_code == 0, res.output
    assert env.calls()[-1]["argv"][1] == "/code-review 1 high"


def test_watch_reviews_new_heads_once(env):
    assert env.run("watch", "--once").exit_code == 0
    assert [c["argv"][1] for c in env.calls()] == ["/code-review 1 medium --comment"]  # draft #2 skipped

    assert env.run("watch", "--once").exit_code == 0
    assert len(env.calls()) == 1

    new_sha = env.push_pr(1, "feature", content="print('again')\n")
    env.prs[0]["head"]["sha"] = new_sha
    env.write_gh()
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


def test_watch_skips_large_pr_after_fetching_size(env):
    env.prs[0]["additions"] = 9000
    env.write_gh()
    assert env.run("watch", "--once").exit_code == 0
    assert env.calls() == []
    assert env.state().get("o/r", 1, env.pr_sha) is None


def test_clean_and_init(env, tmp_path):
    cfg = tmp_path / "new" / "config.toml"
    res = CliRunner().invoke(cli, ["-c", str(cfg), "init"])
    assert res.exit_code == 0 and "[[repos]]" in cfg.read_text()
    assert CliRunner().invoke(cli, ["-c", str(cfg), "init"]).exit_code != 0
    assert env.run("clean").exit_code == 0
