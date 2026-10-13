import pytest

from rocket_critic.config import parse
from rocket_critic.filters import due, matches
from rocket_critic.github import PullRequest
from rocket_critic.state import ATTENTION, DONE, FAILED, SKIPPED, State


def make_pr(**kw):
    base = dict(repo="o/r", number=1, title="t", url="", head_sha="a" * 40, base_ref="main", is_draft=False,
                author="alice", additions=10, deletions=5, labels=[], review_requests=[])
    base.update(kw)
    return PullRequest(**base)


def repo_cfg(**kw):
    review = kw.pop("review", {})
    return parse({"review": review, "repos": [{"name": "o/r", **kw}]}).repos[0]


@pytest.fixture
def state(tmp_path):
    s = State(tmp_path / "state.db")
    yield s
    s.close()


def test_matches_basic():
    assert matches(make_pr(), repo_cfg())[0]


@pytest.mark.parametrize("pr_kw, cfg_kw, ok", [
    ({"is_draft": True}, {}, False),
    ({"is_draft": True}, {"skip_drafts": False}, True),
    ({"author": "Dependabot[bot]"}, {"ignore_authors": ["dependabot[bot]"]}, False),
    ({"base_ref": "dev"}, {"base_branches": ["main"]}, False),
    ({"labels": ["Needs-Review"]}, {"labels": ["needs-review"]}, True),
    ({"labels": ["other"]}, {"labels": ["needs-review"]}, False),
    ({"review_requests": ["robot"]}, {"reviewers": ["robot"]}, True),
    ({"review_requests": ["core-team", "Core Team"]}, {"reviewers": ["myorg/core-team"]}, True),
    ({"labels": ["x"], "review_requests": ["robot"]}, {"labels": ["y"], "reviewers": ["robot"]}, True),
    ({"additions": 4000, "deletions": 2000}, {}, False),
    ({"additions": 4000, "deletions": 2000}, {"review": {"max_diff_lines": 0}}, True),
])
def test_matches(pr_kw, cfg_kw, ok):
    assert matches(make_pr(**pr_kw), repo_cfg(**cfg_kw))[0] is ok


def test_due_new_and_done(state):
    pr, cfg = make_pr(), repo_cfg()
    assert due(pr, cfg, state, 60) == (True, "new head")
    state.start("o/r", 1, pr.head_sha)
    assert due(pr, cfg, state, 60)[0] is False  # running
    state.finish("o/r", 1, pr.head_sha, DONE)
    assert due(pr, cfg, state, 60)[0] is False
    # A new push is reviewed again...
    assert due(make_pr(head_sha="b" * 40), cfg, state, 60)[0] is True
    # ...unless re-review is off.
    assert due(make_pr(head_sha="b" * 40), repo_cfg(review={"rereview_on_push": False}), state, 60)[0] is False


def test_due_retry_backoff(state):
    pr, cfg = make_pr(), repo_cfg(review={"max_attempts": 2})
    state.start("o/r", 1, pr.head_sha)
    state.finish("o/r", 1, pr.head_sha, FAILED, error="boom")
    t = state.get("o/r", 1, pr.head_sha).updated_at
    assert due(pr, cfg, state, 60, now=t + 10)[0] is False
    assert due(pr, cfg, state, 60, now=t + 61)[0] is True
    state.start("o/r", 1, pr.head_sha)
    state.finish("o/r", 1, pr.head_sha, FAILED, error="boom")
    ok, why = due(pr, cfg, state, 60, now=t + 10_000)
    assert not ok and "gave up" in why


def test_reset_running(state):
    state.start("o/r", 1, "c" * 40)
    assert state.reset_running() == 1
    assert state.get("o/r", 1, "c" * 40).status == FAILED


def test_due_settle_and_cap(state):
    from rocket_critic.filters import NEW_HEAD, SETTLING

    cfg = repo_cfg(review={"settle_seconds": 60, "max_reviews_per_pr": 1})
    pr = make_pr()
    assert due(pr, cfg, state, 60) == (False, SETTLING)
    state.mark_pending("o/r", 1, pr.head_sha)
    t = state.get("o/r", 1, pr.head_sha).updated_at
    assert due(pr, cfg, state, 60, now=t + 30)[1].startswith("settling (")
    assert due(pr, cfg, state, 60, now=t + 61) == (True, NEW_HEAD)
    state.start("o/r", 1, pr.head_sha)
    state.finish("o/r", 1, pr.head_sha, DONE)
    ok, why = due(make_pr(head_sha="b" * 40), repo_cfg(review={"max_reviews_per_pr": 1}), state, 60)
    assert not ok and "limit" in why
    assert due(make_pr(head_sha="b" * 40), repo_cfg(review={"max_reviews_per_pr": 0}), state, 60)[0]


@pytest.mark.parametrize("status", [SKIPPED, ATTENTION])
def test_due_final_statuses(state, status):
    pr = make_pr()
    state.finish("o/r", 1, pr.head_sha, status, error="x")
    assert due(pr, repo_cfg(), state, 60)[0] is False


def test_state_migrates_old_schema(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE reviews (repo TEXT NOT NULL, pr INTEGER NOT NULL, head_sha TEXT NOT NULL,
                  status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, error TEXT, cost_usd REAL,
                  duration_ms INTEGER, num_turns INTEGER, output_path TEXT, updated_at REAL NOT NULL,
                  PRIMARY KEY (repo, pr, head_sha))""")
    db.execute("INSERT INTO reviews (repo, pr, head_sha, status, updated_at) VALUES ('o/r', 1, 'a', 'done', 1)")
    db.commit()
    db.close()
    st = State(path)
    assert st.get("o/r", 1, "a").phase is None
    assert st.count_done("o/r", 1) == 1
    st.start("o/r", 1, "b")
    assert st.get("o/r", 1, "b").phase == "checkout"
