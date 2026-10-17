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
    assert "Edit" not in cmd[cmd.index("--disallowedTools") + 1].split(",")
    assert "--add-dir" not in cmd  # no scratch directory: no writes at all
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


def test_review_prompt_with_context():
    s = cfg()
    assert review_prompt(4, s.review, context="CTX") == "/code-review 4 medium --comment\n\nCTX"


def test_rereview_context_modes_and_cap():
    from rocket_critic.github import Thread
    from rocket_critic.runner import MAX_THREADS_CHARS, rereview_context

    threads = [Thread("a.py", 3, "bot", "Bug here.\nDetails.", [("alice", "Won't fix.")]),
               Thread("b.py", None, "bot", "Old.")]
    text = rereview_context("abc", "range", threads)
    assert "`git diff abc HEAD`" in text
    assert "- a.py:3 (bot): Bug here. Details." in text
    assert "  - reply (alice): Won't fix." in text
    assert "- b.py (outdated) (bot): Old." in text
    assert "history was rewritten" in rereview_context("abc", "rewritten", [])
    assert "Earlier review threads" not in rereview_context("abc", "repeat", [])

    many = [Thread("x.py", i, "bot", "y" * 500) for i in range(200)]
    text = rereview_context("abc", "range", many)
    assert len(text) < MAX_THREADS_CHARS + 2000 and "more thread(s) omitted" in text


def test_parse_pages():
    from rocket_critic.github import parse_pages

    assert parse_pages("") == []
    assert parse_pages("[]\n") == [[]]
    assert parse_pages('[1][2, 3]') == [[1], [2, 3]]
    assert parse_pages('[{"a": "]["}]\n[\n{"b": 2}]\n') == [[{"a": "]["}], [{"b": 2}]]


def test_command_error_reports_first_line():
    from rocket_critic.proc import CommandError

    err = CommandError(["gh", "api"], 1, "", "unknown flag: --slurp\n\nUsage:  gh api\n  --verbose  x\n")
    assert str(err).endswith(": unknown flag: --slurp")


def test_build_command_with_scratch(tmp_path):
    from rocket_critic.config import parse as parse_cfg

    c = parse_cfg({"claude": {"disallowed_tools": ["Edit", "Write", "WebFetch"]}}).claude
    cmd = build_command("p", c, scratch=tmp_path)
    allowed = cmd[cmd.index("--allowedTools") + 1].split(",")
    assert f"Edit(/{tmp_path.resolve()}/**)" in allowed and f"Edit(//{str(tmp_path.resolve())[1:]}/**)" in allowed
    assert cmd[cmd.index("--disallowedTools") + 1] == "WebFetch"  # a bare deny would override the scoped allow
    assert cmd[cmd.index("--add-dir") + 1] == str(tmp_path.resolve())


def test_review_prompt_posting_notes(tmp_path):
    s = cfg()
    text = review_prompt(5, s.review, scratch=tmp_path, context="CTX")
    first, *rest = text.split("\n\n")
    assert first == "/code-review 5 medium --comment"
    assert "without `git -C`" in text and f"{tmp_path}/comment-1.json" in text and "--input <file>" in text
    assert "Use the Read, Grep and Glob tools" in text and "Don't build, test or run the project" in text
    assert text.endswith("CTX")
    assert "Bash(echo:*)" in s.claude.allowed_tools and "Bash(git remote -v)" in s.claude.allowed_tools


def test_rocket_critic_posting_prompt_and_command(tmp_path):
    from rocket_critic.runner import FINDINGS, NO_POST, POST, posting_mode

    s = cfg(posting="rocket-critic")
    assert posting_mode(s.review) == FINDINGS and posting_mode(s.review, comment=False) == FINDINGS
    assert posting_mode(cfg().review) == POST and posting_mode(cfg().review, comment=False) == NO_POST
    text = review_prompt(3, s.review, scratch=tmp_path)
    assert text.splitlines()[0] == "/code-review 3 medium"  # never --comment: rocket-critic posts
    assert f"{tmp_path}/findings.json" in text and "Do not post anything" in text
    assert "gh api --method POST" not in text
    assert f"review rules in {tmp_path}/CLAUDE.md" in text

    cmd = build_command("p", s.claude, scratch=tmp_path, read_only_github=True)
    allowed = cmd[cmd.index("--allowedTools") + 1].split(",")
    assert not any(t.startswith("Bash(gh api") for t in allowed) and "Bash(gh pr diff:*)" in allowed


def test_review_rules():
    from rocket_critic.runner import FINDINGS, MAX_GUIDELINES_CHARS, POST, review_rules

    text = review_rules(POST, repo_rules="Only report security bugs as Important.\n", guidelines="Skip docs/.")
    assert "\U0001F534 Important" in text and "\U0001F7E3 Pre-existing" in text
    assert "Start every review comment with its marker" in text
    assert "## Repository review guidelines (REVIEW.md on the default branch)\n\nOnly report security" in text
    assert "## Review guidelines\n\nSkip docs/." in text
    assert "Do not post Nit findings" not in text
    assert "Do not post Nit findings." in review_rules(POST, post_nits=False)

    text = review_rules(FINDINGS, post_nits=False)
    assert 'Record it as the `severity`' in text and "Nit findings are not posted" in text
    assert "Repository review guidelines" not in text  # no REVIEW.md, no section

    long = review_rules(POST, guidelines="line\n" * MAX_GUIDELINES_CHARS)
    assert "(... truncated by rocket-critic)" in long and len(long) < MAX_GUIDELINES_CHARS + 3000
