import json

import pytest

from rocket_critic.config import parse
from rocket_critic.github import PullRequest
from rocket_critic.runner import ReviewError, build_command, build_env, parse_envelope, review_prompt


def cfg(**review):
    return parse({"claude": {"model": "glm-5.3:cloud", "extra_args": ["--effort", "high"]}, "review": review})


def test_review_prompt():
    s = cfg()
    assert review_prompt(42, s.review) == "/code-review 42 medium --comment"
    assert review_prompt(42, s.review, comment=False) == "/code-review 42 medium"
    s = cfg(level="high", max_findings="all", comment=False)
    assert review_prompt(7, s.review) == "/code-review 7 high --max-findings all"


def test_build_command():
    s = cfg()
    cmd = build_command("/code-review 1 medium --comment", s.claude)
    assert cmd[:3] == ["claude", "-p", "/code-review 1 medium --comment"]
    assert cmd[cmd.index("--model") + 1] == "glm-5.3:cloud"
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[cmd.index("--setting-sources") + 1] == "user"
    assert "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    allowed = cmd[cmd.index("--allowedTools") + 1].split(",")
    assert "Bash(gh api:*)" in allowed and "Edit" not in allowed
    assert "Edit" in cmd[cmd.index("--disallowedTools") + 1].split(",")
    assert cmd[-2:] == ["--effort", "high"]


def test_build_env():
    s = parse({"claude": {"env": {"ANTHROPIC_BASE_URL": "http://localhost:11434", "GH_TOKEN": "${RC_TOK}"}}})
    env = build_env(s.claude, "o/r", base={"PATH": "/bin", "GH_TOKEN": "user"})
    assert env["ANTHROPIC_BASE_URL"] == "http://localhost:11434"
    assert env["GH_TOKEN"] == "user"  # unset reference falls back to the inherited value
    assert env["GH_REPO"] == "o/r"
    env = build_env(s.claude, "o/r", base={"PATH": "/bin", "RC_TOK": "bot"})
    assert env["GH_TOKEN"] == "bot"


def test_parse_envelope():
    r = parse_envelope(json.dumps({
        "type": "result", "subtype": "success", "is_error": False, "result": "done",
        "total_cost_usd": 0.5, "duration_ms": 10, "num_turns": 3,
        "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "gh pr comment 1"}}],
    }))
    assert r.text == "done" and not r.is_error and r.num_turns == 3
    assert r.permission_denials == ["Bash(gh pr comment 1)"]


def test_parse_envelope_error_and_event_list():
    events = [{"type": "system"}, {"type": "result", "subtype": "error_max_turns", "is_error": False}]
    assert parse_envelope(json.dumps(events)).is_error
    with pytest.raises(ReviewError):
        parse_envelope("not json")


def test_pull_request_from_rest():
    pr = PullRequest.from_rest("o/r", {
        "number": 3, "title": "x", "html_url": "u", "head": {"sha": "abc"}, "base": {"ref": "main"},
        "draft": True, "user": {"login": "bob"}, "labels": [{"name": "bug"}],
        "requested_reviewers": [{"login": "robot"}], "requested_teams": [{"name": "Core", "slug": "core"}],
    })
    assert pr.ref == "o/r#3" and pr.is_draft and pr.labels == ["bug"] and pr.author == "bob"
    assert pr.review_requests == ["robot", "core", "Core"]
    assert pr.size is None
    assert PullRequest.from_rest("o/r", {"number": 1, "head": {"sha": "a"}, "additions": 2, "deletions": 3}).size == 5
