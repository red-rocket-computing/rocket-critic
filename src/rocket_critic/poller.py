"""Poll configured repositories and review pull requests that need it."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
import signal
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import filters, proc
from .config import RepoConfig, Settings
from .ghapp import GitHubApp, GitHubAppError
from .findings import (FINDINGS_FILE, FindingsError, Review, build_review, commentable_lines, counts,
                       extract_json, parse_findings)
from .github import GitHub, PullRequest
from .runner import (FINDINGS, ReviewError, ReviewResult, build_env, posting_mode, rereview_context, review_rules,
                     run_review)
from .github import _ts
from .state import ATTENTION, DONE, FAILED, PHASE_POST, PHASE_REVIEW, SKIPPED, Record, State
from .workspace import Workspace

log = logging.getLogger(__name__)


# Who may request a review with a comment: people with write access.
COMMAND_AUTHORS = {"OWNER", "MEMBER", "COLLABORATOR"}
# Re-read this many seconds of comments each poll, in case one was slow to appear.
COMMENT_OVERLAP = 300


def is_command(body: str, commands: list[str]) -> bool:
    """Whether a line of ``body`` is, or starts with, one of ``commands`` (case-insensitive)."""
    wanted = [c.strip().lower() for c in commands if c.strip()]
    for line in body.splitlines():
        line = line.strip().lower()
        if any(line == c or line.startswith(c + " ") for c in wanted):
            return True
    return False


class AlreadyRunning(RuntimeError):
    pass


@dataclass
class Outcome:
    pr: PullRequest
    status: str
    detail: str = ""
    output_path: Path | None = None


class Critic:
    def __init__(self, settings: Settings, gh: GitHub | None = None, workspace: Workspace | None = None,
                 state: State | None = None):
        self.settings = settings
        self.gh = gh or GitHub()
        self.ws = workspace or Workspace(settings.workdir)
        self.state = state or State(settings.workdir / "state.db")
        self._identity: dict[str, str | None] = {}
        self.app = GitHubApp(settings.github_app) if settings.github_app else None
        self.stop = threading.Event()
        self.wake = threading.Event()  # ends the sleep between polls early
        self.reload_requested = threading.Event()

    def apply_settings(self, settings: Settings) -> None:
        """Switch to a reloaded config (same workdir, so state and checkouts carry over)."""
        if settings.github_app != self.settings.github_app:
            self.app = GitHubApp(settings.github_app) if settings.github_app else None
        self.settings = settings
        self._identity.clear()

    def uses_app(self, repo: RepoConfig) -> bool:
        """Whether /code-review posts as the GitHub App for ``repo`` (a token in [claude.env] wins)."""
        explicit = repo.claude.resolved_env()
        return self.app is not None and "GH_TOKEN" not in explicit and "GITHUB_TOKEN" not in explicit

    def poster(self, repo: RepoConfig) -> GitHub:
        """``gh`` acting as the identity /code-review posts as (for reactions and label removal)."""
        if self.uses_app(repo):
            return GitHub(self.gh.gh, env={**os.environ, "GH_TOKEN": self.app.token(repo.name)})
        return GitHub(self.gh.gh, env=build_env(repo.claude, repo.name))

    def app_env(self, repo: RepoConfig, read_only: bool = False) -> dict[str, str]:
        """GH_TOKEN for the claude subprocess: a fresh installation token limited to the repo
        (and to reading, when rocket-critic posts the review)."""
        if not self.uses_app(repo):
            return {}
        try:
            return {"GH_TOKEN": self.app.token(repo.name, read_only=read_only)}
        except GitHubAppError as exc:
            raise ReviewError(f"GitHub App token: {exc}") from None

    # -- one PR ---------------------------------------------------------------

    def review(self, pr: PullRequest, repo: RepoConfig, *, comment: bool | None = None,
               incremental: bool | None = None) -> Outcome:
        """Check out ``pr`` and run /code-review on it, recording the result.

        ``incremental`` (default from config) adds re-review context when the
        PR was reviewed before: the commit range to focus on and the earlier
        review threads not to repeat.
        """
        incremental = repo.review.incremental if incremental is None else incremental
        last = self.state.last_done(pr.repo, pr.number) if incremental else None
        mode = posting_mode(repo.review, comment)
        self.state.start(pr.repo, pr.number, pr.head_sha)
        worktree = scratch = None
        findings_text = diff = None
        out_dir = self.settings.workdir / "runs" / pr.repo / str(pr.number)
        try:
            worktree = self.ws.checkout(pr.repo, pr.number, pr.head_sha, pr.base_ref)
            context = self.rereview_context(pr, repo, last.head_sha) if last else ""
            extra_env = self.app_env(repo, read_only=mode == FINDINGS)
            # The only place claude may write: the review rules (CLAUDE.md), and its
            # findings or payload files for gh api --input.
            scratch = out_dir / f"scratch-{pr.head_sha[:12]}"
            shutil.rmtree(scratch, ignore_errors=True)
            scratch.mkdir(parents=True)
            rules = self.review_rules(pr, repo, mode)
            self.state.set_phase(pr.repo, pr.number, pr.head_sha, PHASE_REVIEW)
            result = run_review(worktree, pr.repo, pr.number, repo.claude, repo.review, comment=comment,
                                context=context, extra_env=extra_env, scratch=scratch, rules=rules)
            if mode == FINDINGS and not result.is_error:
                findings_text = read_findings(scratch, result)
                try:
                    diff = self.ws.diff(worktree, pr.base_ref)
                except proc.CommandError as exc:
                    # Not worth losing the review over: every finding goes in the review body.
                    log.warning("%s: could not diff against %s: %s", pr.ref, pr.base_ref, exc)
        except (ReviewError, proc.CommandError) as exc:
            self.state.finish(pr.repo, pr.number, pr.head_sha, FAILED, error=str(exc))
            log.error("%s: review failed: %s", pr.ref, exc)
            return Outcome(pr, FAILED, str(exc))
        except BaseException as exc:
            self.state.finish(pr.repo, pr.number, pr.head_sha, FAILED, error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            if worktree is not None and not repo.review.keep_worktrees:
                try:
                    self.ws.remove(pr.repo, worktree)
                except proc.CommandError as exc:
                    log.warning("%s: could not remove worktree %s: %s", pr.ref, worktree, exc)
            if scratch is not None and not repo.review.keep_worktrees:
                shutil.rmtree(scratch, ignore_errors=True)

        out_dir.mkdir(parents=True, exist_ok=True)
        output_path = out_dir / f"{pr.head_sha[:12]}.md"
        output_path.write_text(result.text + "\n")
        (out_dir / f"{pr.head_sha[:12]}.json").write_text(json.dumps(result.raw, indent=2) + "\n")

        status = FAILED if result.is_error else DONE
        error = (result.text.strip().splitlines() or ["claude reported an error"])[-1] if result.is_error else None
        if status == DONE and mode == FINDINGS:
            post = repo.review.comment if comment is None else comment
            status, error = self.publish(pr, repo, findings_text, diff or "", output_path, post)
        self.state.finish(
            pr.repo, pr.number, pr.head_sha, status, error=error, cost_usd=result.cost_usd,
            duration_ms=result.duration_ms, num_turns=result.num_turns, output_path=str(output_path),
        )
        if status == DONE:
            log.info("%s: reviewed %s (%s turns, %.1fs)", pr.ref, pr.head_sha[:7], result.num_turns,
                     (result.duration_ms or 0) / 1000)
            try:
                self.ws.mark_reviewed(pr.repo, pr.number, pr.head_sha)
            except proc.CommandError as exc:
                log.warning("%s: could not record reviewed commit: %s", pr.ref, exc)
            self.clear_request_labels(pr, repo)
        elif mode == FINDINGS and not result.is_error:
            log.error("%s: %s", pr.ref, error)
        else:
            log.error("%s: claude reported an error: %s", pr.ref, error)
        return Outcome(pr, status, error or "", output_path)

    def review_rules(self, pr: PullRequest, repo: RepoConfig, mode: str) -> str:
        """The review's CLAUDE.md: severities, REVIEW.md from the default branch, configured guidelines."""
        repo_rules = self.ws.default_file(pr.repo, "REVIEW.md") if repo.review.review_md else None
        guidelines = ""
        if repo.review.guidelines:
            try:
                guidelines = Path(repo.review.guidelines).read_text()
            except OSError as exc:
                raise ReviewError(f"review guidelines: {exc}") from None
        if repo_rules:
            log.info("%s: following REVIEW.md from the default branch", pr.ref)
        return review_rules(mode, post_nits=repo.review.post_nits, repo_rules=repo_rules or "",
                            guidelines=guidelines)

    def publish(self, pr: PullRequest, repo: RepoConfig, text: str | None, diff: str, output_path: Path,
                post: bool) -> tuple[str, str | None]:
        """Check claude's findings and (if ``post``) post them as one review; returns (status, error).

        Nothing is posted unless every finding has a path, a line and a recognised severity.
        """
        stem = output_path.with_suffix("")
        if text is None:
            return FAILED, f"claude wrote no {FINDINGS_FILE} (and no findings JSON in its reply); nothing posted"
        try:
            summary, findings = parse_findings(text)
        except FindingsError as exc:
            stem.with_name(stem.name + ".findings-rejected.json").write_text(text)
            return FAILED, f"findings rejected, nothing posted: {exc}"
        review = build_review(summary, findings, commentable_lines(diff), pr.head_sha, repo.review.post_nits)
        self.save_review(output_path, review)
        what = counts(review.inline + review.outside) if findings else "no findings"
        if not post:
            log.info("%s: %s (not posted)", pr.ref, what)
            return DONE, None
        if self.claude_posted(pr, repo):
            return ATTENTION, ("claude posted to the PR itself although posting = \"rocket-critic\"; not posting "
                               "its findings as well (see the run output), use `review --force` to redo")
        self.state.set_phase(pr.repo, pr.number, pr.head_sha, PHASE_POST)
        try:
            poster = self.poster(repo)
            try:
                poster.post_review(pr.repo, pr.number, review.payload)
            except proc.CommandError as exc:
                if not review.payload["comments"]:
                    raise
                # GitHub rejects the whole review if it can't place one comment; nothing was posted.
                log.warning("%s: GitHub refused the inline comments (%s); listing them in the review body",
                            pr.ref, exc)
                review = build_review(summary, findings, {}, pr.head_sha, repo.review.post_nits, inline=False)
                self.save_review(output_path, review)
                poster.post_review(pr.repo, pr.number, review.payload)
        except (proc.CommandError, GitHubAppError) as exc:
            return FAILED, f"could not post the review: {exc}"
        log.info("%s: posted %d inline comment(s), %d in the review body: %s", pr.ref, len(review.inline),
                 len(review.outside), what)
        return DONE, None

    @staticmethod
    def save_review(output_path: Path, review: Review) -> None:
        """Keep the review next to claude's output: as JSON, and appended to the readable output."""
        stem = output_path.with_suffix("")
        stem.with_name(stem.name + ".review.json").write_text(json.dumps(review.to_json(), indent=2) + "\n")
        text = output_path.read_text().split("\n\n---\n\n## Review posted by rocket-critic", 1)[0]
        parts = [text.rstrip(), "## Review posted by rocket-critic", review.payload["body"]]
        for c in review.payload["comments"]:
            lines = f"{c['start_line']}-{c['line']}" if "start_line" in c else str(c["line"])
            parts.append(f"`{c['path']}:{lines}`\n\n{c['body']}")
        output_path.write_text(parts[0] + "\n\n---\n\n" + "\n\n".join(parts[1:]) + "\n")

    def claude_posted(self, pr: PullRequest, repo: RepoConfig) -> bool:
        """Whether claude posted to the PR during this review although rocket-critic posts.

        With a GitHub App its token is read-only, so it can't have; a user token can write,
        and claude posts as the same identity rocket-critic would.
        """
        if self.uses_app(repo):
            return False
        rec = self.state.get(pr.repo, pr.number, pr.head_sha)
        login = self.identity(repo)
        if rec is None or rec.started_at is None or not login:
            return False
        try:
            return self.gh.posted_since(pr.repo, pr.number, login, rec.started_at)
        except (proc.CommandError, ValueError) as exc:
            log.warning("%s: could not check whether claude posted: %s", pr.ref, exc)
            return False

    def rereview_context(self, pr: PullRequest, repo: RepoConfig, last_sha: str) -> str:
        """Context for re-reviewing ``pr`` after an earlier review at ``last_sha``."""
        if last_sha == pr.head_sha:
            mode = "repeat"
        elif self.ws.is_ancestor(pr.repo, last_sha, pr.head_sha):
            mode = "range"
        else:
            mode = "rewritten"
        try:
            threads = self.gh.review_threads(pr.repo, pr.number)
        except (proc.CommandError, ValueError) as exc:
            log.warning("%s: could not load earlier review threads: %s", pr.ref, exc)
            threads = []
        login = self.identity(repo)
        if login:
            threads = [t for t in threads if t.author.lower() == login.lower()]
        log.info("%s: re-review since %s (%s, %d earlier thread(s))", pr.ref, last_sha[:7], mode, len(threads))
        return rereview_context(last_sha, mode, threads)

    # -- polling --------------------------------------------------------------

    def identity(self, repo: RepoConfig) -> str | None:
        """GitHub login the claude subprocess posts as (cached per distinct env)."""
        if self.uses_app(repo):
            try:
                return self.app.login
            except GitHubAppError as exc:
                log.warning("could not look up the GitHub App: %s", exc)
                return None
        env = build_env(repo.claude, repo.name)
        key = env.get("GH_TOKEN", "") + "\0" + env.get("GITHUB_TOKEN", "")
        if key not in self._identity:
            self._identity[key] = self.gh.whoami(env=env)
        return self._identity[key]

    def already_on_github(self, pr: PullRequest, repo: RepoConfig) -> bool:
        """Fallback dedupe for a lost state DB: a review by our identity already exists at this SHA."""
        login = self.identity(repo)
        if not login:
            return False
        try:
            return pr.head_sha in self.gh.reviewed_shas(pr.repo, pr.number, login)
        except (proc.CommandError, ValueError) as exc:
            log.debug("%s: could not list reviews: %s", pr.ref, exc)
            return False

    def retry_is_safe(self, pr: PullRequest, repo: RepoConfig) -> bool | None:
        """Whether a failed review can be retried without re-posting comments.

        True: safe. False: it may have posted, so it was marked for attention.
        None: could not tell right now; try again next poll.
        """
        rec = self.state.get(pr.repo, pr.number, pr.head_sha)
        if rec is None or rec.phase not in (PHASE_REVIEW, PHASE_POST) or not repo.review.comment \
                or rec.started_at is None:
            return True
        login = self.identity(repo)
        posted = None
        if login:
            try:
                posted = self.gh.posted_since(pr.repo, pr.number, login, rec.started_at)
            except (proc.CommandError, ValueError) as exc:
                log.warning("%s: could not check for partial review: %s", pr.ref, exc)
                return None
        if posted is False:
            return True
        reason = ("failed after posting to the PR" if posted else
                  "failed during review and the posting identity is unknown")
        self.state.finish(pr.repo, pr.number, pr.head_sha, ATTENTION,
                          error=f"{reason} ({rec.error}); not retrying, use `review --force` to redo")
        log.warning("%s: %s; not retrying to avoid duplicate comments", pr.ref, reason)
        return False

    def clear_request_labels(self, pr: PullRequest, repo: RepoConfig) -> None:
        """In request label mode, take the trigger labels off after a review, so adding one asks again."""
        labels = filters.request_labels(pr, repo)
        if not labels:
            return
        try:
            poster = self.poster(repo)
            for label in labels:
                poster.remove_label(pr.repo, pr.number, label)
            log.info("%s: removed label(s) %s", pr.ref, ", ".join(labels))
        except (proc.CommandError, GitHubAppError) as exc:
            log.warning("%s: could not remove label(s) %s: %s", pr.ref, ", ".join(labels), exc)

    def rerequested_since(self, pr: PullRequest, repo: RepoConfig, last: Record) -> bool:
        """Whether a review was asked for again after ``last`` review started: a configured
        reviewer requested, or (request label mode) a configured label added.

        GitHub drops a review request once that reviewer posts, and request-mode labels are
        removed after a review, so either one being present is normally a new request.
        Comparing against the timeline guards against one left over because a review posted
        nothing or the label could not be removed.
        """
        names = filters.requested_reviewers(pr, repo)
        labels = filters.request_labels(pr, repo)
        try:
            requested_at = self.gh.requested_at(pr.repo, pr.number, names, labels)
        except (proc.CommandError, ValueError) as exc:
            log.warning("%s: could not read the PR timeline: %s", pr.ref, exc)
            return False
        since = last.started_at or last.updated_at
        return requested_at is not None and requested_at > since

    def comment_requests(self, repo: RepoConfig, prs: list[PullRequest]) -> dict[int, dict]:
        """New review commands in PR comments, by PR number (the newest comment per PR).

        Only lines starting with one of ``repo.commands``, by people with write access, posted
        after rocket-critic started watching comments for this repo; each comment is used once.
        """
        if not repo.commands:
            return {}
        started, last_poll = self.state.comment_watch(repo.name)
        polled = time.time()
        try:
            comments = self.gh.issue_comments_since(repo.name, max(started, last_poll - COMMENT_OVERLAP))
        except (proc.CommandError, ValueError) as exc:
            log.warning("%s: could not read PR comments: %s", repo.name, exc)
            return {}
        open_prs = {pr.number for pr in prs}
        me = (self.identity(repo) or "").lower()
        found: dict[int, dict] = {}
        for c in comments:
            number = int(str(c.get("issue_url", "")).rsplit("/", 1)[-1] or 0)
            login = ((c.get("user") or {}).get("login") or "").lower()
            if (number not in open_prs or self.state.comment_handled(repo.name, c["id"])
                    or _ts(c.get("created_at")) < int(started) or login == me
                    or c.get("author_association") not in COMMAND_AUTHORS
                    or not is_command(c.get("body") or "", repo.commands)):
                continue
            found[number] = c
        self.state.set_comment_poll(repo.name, polled)
        return found

    def answer_comment(self, repo: RepoConfig, comment: dict, content: str) -> None:
        self.state.mark_comment_handled(repo.name, comment["id"])
        try:
            self.poster(repo).react(repo.name, comment["id"], content)
        except (proc.CommandError, GitHubAppError) as exc:
            log.warning("%s: could not react to comment %s: %s", repo.name, comment["id"], exc)

    def candidates(self, repo: RepoConfig) -> Iterator[PullRequest]:
        prs = self.gh.list_open_prs(repo.name)
        commanded = self.comment_requests(repo, prs)
        for pr in prs:
            comment = commanded.get(pr.number)
            if comment is not None:
                who = (comment.get("user") or {}).get("login") or "?"
                pr = self.gh.get_pr(pr.repo, pr.number)
                ok, why = filters.command_check(pr, repo, self.state)
                if not ok:
                    log.info("%s: %s asked for a review in a comment, but not reviewing (%s)", pr.ref, who, why)
                    self.answer_comment(repo, comment, "confused")
                    continue
                self.answer_comment(repo, comment, "eyes")
                log.info("%s: queued (%s by %s) %s", pr.ref, why, who, pr.title)
                yield pr
                continue
            ok, why = filters.should_review(pr, repo, self.state, retry_base=self.settings.poll_interval)
            if why == filters.SETTLING:
                log.info("%s: new head %s; waiting %ss for it to settle", pr.ref, pr.head_sha[:7],
                         repo.review.settle_seconds)
                self.state.mark_pending(pr.repo, pr.number, pr.head_sha)
                continue
            if not ok:
                last = filters.rerequest_candidate(pr, repo, self.state, why)
                if last is None or not self.rerequested_since(pr, repo, last):
                    log.debug("%s: skip (%s)", pr.ref, why)
                    continue
                why = filters.REREQUESTED
            if why.startswith(filters.RETRY) and not self.retry_is_safe(pr, repo):
                continue
            if pr.size is None:
                # The list endpoint omits diff size; fetch the PR to apply max_diff_lines.
                pr = self.gh.get_pr(pr.repo, pr.number)
                ok, size_why = filters.matches(pr, repo)
                if not ok:
                    log.info("%s: skip (%s)", pr.ref, size_why)
                    if size_why.startswith(filters.TOO_LARGE):
                        # Final for this commit: no more API calls until the next push.
                        self.state.finish(pr.repo, pr.number, pr.head_sha, SKIPPED, error=size_why)
                    continue
            if why == filters.NEW_HEAD and self.already_on_github(pr, repo):
                log.info("%s: already reviewed %s on GitHub; recording", pr.ref, pr.head_sha[:7])
                self.state.finish(pr.repo, pr.number, pr.head_sha, DONE, error="found existing review")
                continue
            log.info("%s: queued (%s) %s", pr.ref, why, pr.title)
            yield pr

    def poll_once(self, repos: list[RepoConfig]) -> list[Outcome]:
        outcomes: list[Outcome] = []
        for repo in repos:
            if self.stop.is_set():
                break
            try:
                for pr in self.candidates(repo):
                    if self.stop.is_set():
                        break
                    outcomes.append(self.review(pr, repo))
            except (proc.CommandError, ValueError) as exc:
                log.error("%s: poll failed: %s", repo.name, exc)
        return outcomes

    def watch(
        self,
        repos: list[RepoConfig],
        once: bool = False,
        reload: Callable[[bool], tuple[Settings, list[RepoConfig]] | None] | None = None,
    ) -> None:
        """Poll until stopped. Before each poll, ``reload(forced)`` may return a new config
        (forced after SIGHUP); between polls, SIGHUP or a stop request ends the wait early."""
        reset = self.state.reset_running()
        if reset:
            log.info("marked %d interrupted review(s) for retry", reset)
        while not self.stop.is_set():
            if reload is not None:
                forced = self.reload_requested.is_set()
                self.reload_requested.clear()
                new = reload(forced)
                if new is not None:
                    settings, repos = new
                    self.apply_settings(settings)
            self.poll_once(repos)
            if once:
                break
            self.wake.wait(self.settings.poll_interval)
            self.wake.clear()

    def install_signal_handlers(self) -> None:
        def stop(signum: int, _frame: object) -> None:
            if self.stop.is_set():
                raise KeyboardInterrupt
            log.info("received %s; stopping after the current review (repeat to abort it)",
                     signal.Signals(signum).name)
            self.stop.set()
            self.wake.set()

        def reload(_signum: int, _frame: object) -> None:
            log.info("received SIGHUP; reloading the config before the next poll")
            self.reload_requested.set()
            self.wake.set()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGHUP, reload)


LOCK_NAME = "rocket-critic.lock"


@contextmanager
def lock(workdir: Path) -> Iterator[None]:
    """Hold the workdir lock; the lock file records the holder's PID (see :func:`running_pid`)."""
    workdir.mkdir(parents=True, exist_ok=True)
    path = workdir / LOCK_NAME
    # Opened without truncating, so a failed attempt never wipes the holder's PID.
    with open(path, "a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pid = running_pid(workdir)
            holder = f" (pid {pid})" if pid else ""
            raise AlreadyRunning(f"another rocket-critic is using {workdir}{holder}") from None
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(f"{os.getpid()}\n")
            fh.flush()
            yield
        finally:
            fh.seek(0)
            fh.truncate()
            fcntl.flock(fh, fcntl.LOCK_UN)


def running_pid(workdir: Path) -> int | None:
    """PID of the process holding ``workdir``'s lock, or None if nobody holds it."""
    path = workdir / LOCK_NAME
    try:
        fh = open(path)
    except FileNotFoundError:
        return None
    with fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            text = fh.read().strip()
            return int(text) if text.isdigit() else -1  # held, PID not written yet
        fcntl.flock(fh, fcntl.LOCK_UN)
        return None


def read_findings(scratch: Path, result: ReviewResult) -> str | None:
    """What claude wrote to the findings file, or the findings JSON in its reply if it printed it."""
    try:
        return (scratch / FINDINGS_FILE).read_text()
    except OSError:
        return extract_json(result.text)
