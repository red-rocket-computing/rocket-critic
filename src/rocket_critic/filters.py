"""Decide whether a pull request should be reviewed now."""

from __future__ import annotations

import time

from .config import RepoConfig
from .github import PullRequest
from .state import ATTENTION, DONE, FAILED, PENDING, RUNNING, SKIPPED, State

NEW_HEAD = "new head"
SETTLING = "settling"
RETRY = "retry"
TOO_LARGE = "too large"


def matches(pr: PullRequest, repo: RepoConfig) -> tuple[bool, str]:
    """Static filters from the repo config (no history involved)."""
    if repo.skip_drafts and pr.is_draft:
        return False, "draft"
    if pr.author.lower() in {a.lower() for a in repo.ignore_authors}:
        return False, f"author {pr.author} ignored"
    if repo.base_branches and pr.base_ref not in repo.base_branches:
        return False, f"base {pr.base_ref} not watched"
    wanted_labels = {lbl.lower() for lbl in repo.labels}
    wanted_reviewers = {_team_key(r) for r in repo.reviewers}
    if wanted_labels or wanted_reviewers:
        has_label = bool(wanted_labels & {lbl.lower() for lbl in pr.labels})
        has_reviewer = bool(wanted_reviewers & {_team_key(r) for r in pr.review_requests})
        if not (has_label or has_reviewer):
            return False, "no matching label or review request"
    size = pr.size
    if repo.review.max_diff_lines and size is not None and size > repo.review.max_diff_lines:
        return False, f"{TOO_LARGE} ({size} changed lines > {repo.review.max_diff_lines})"
    return True, "ok"


def due(pr: PullRequest, repo: RepoConfig, state: State, retry_base: float, now: float | None = None) -> tuple[bool, str]:
    """History-based checks: already reviewed, settling, capped, in progress, or failed and backing off.

    Returns ``(False, SETTLING)`` for a head seen for the first time while a
    settle delay is configured; the caller records it as pending.
    """
    now = time.time() if now is None else now
    review = repo.review
    rec = state.get(pr.repo, pr.number, pr.head_sha)
    if rec is None or rec.status == PENDING:
        if not review.rereview_on_push and state.any_done(pr.repo, pr.number):
            return False, "already reviewed an earlier commit"
        if rec is None and review.settle_seconds:
            return False, SETTLING
        if rec is not None and now < rec.updated_at + review.settle_seconds:
            return False, f"settling ({int(rec.updated_at + review.settle_seconds - now)}s left)"
        return _capped(pr, repo, state) or (True, NEW_HEAD)
    if rec.status in (DONE, SKIPPED, ATTENTION):
        return False, f"already {rec.status} at {pr.head_sha[:7]}"
    if rec.status == RUNNING:
        return False, "review in progress"
    if rec.status == FAILED:
        if rec.attempts >= review.max_attempts:
            return False, f"gave up after {rec.attempts} attempts"
        wait = retry_base * (2 ** (rec.attempts - 1))
        if now < rec.updated_at + wait:
            return False, f"retry backoff ({int(rec.updated_at + wait - now)}s left)"
        return _capped(pr, repo, state) or (True, f"{RETRY} {rec.attempts + 1}/{review.max_attempts}")
    return True, rec.status


def _capped(pr: PullRequest, repo: RepoConfig, state: State) -> tuple[bool, str] | None:
    limit = repo.review.max_reviews_per_pr
    if limit and state.count_done(pr.repo, pr.number) >= limit:
        return False, f"review limit reached ({limit} per PR)"
    return None


def should_review(pr: PullRequest, repo: RepoConfig, state: State, retry_base: float) -> tuple[bool, str]:
    ok, why = matches(pr, repo)
    if not ok:
        return ok, why
    return due(pr, repo, state, retry_base)


def _team_key(name: str) -> str:
    # Accept "org/team-slug", "team-slug" or a user login; compare on the last part.
    return name.lower().rsplit("/", 1)[-1]
