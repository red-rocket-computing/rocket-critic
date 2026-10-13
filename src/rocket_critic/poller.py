"""Poll configured repositories and review pull requests that need it."""

from __future__ import annotations

import fcntl
import json
import logging
import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import filters, proc
from .config import RepoConfig, Settings
from .github import GitHub, PullRequest
from .runner import ReviewError, build_env, rereview_context, run_review
from .state import ATTENTION, DONE, FAILED, PHASE_REVIEW, SKIPPED, Record, State
from .workspace import Workspace

log = logging.getLogger(__name__)


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
        self.stop = threading.Event()

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
        self.state.start(pr.repo, pr.number, pr.head_sha)
        worktree = None
        out_dir = self.settings.workdir / "runs" / pr.repo / str(pr.number)
        try:
            worktree = self.ws.checkout(pr.repo, pr.number, pr.head_sha, pr.base_ref)
            context = self.rereview_context(pr, repo, last.head_sha) if last else ""
            self.state.set_phase(pr.repo, pr.number, pr.head_sha, PHASE_REVIEW)
            result = run_review(worktree, pr.repo, pr.number, repo.claude, repo.review, comment=comment,
                                context=context)
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

        out_dir.mkdir(parents=True, exist_ok=True)
        output_path = out_dir / f"{pr.head_sha[:12]}.md"
        output_path.write_text(result.text + "\n")
        (out_dir / f"{pr.head_sha[:12]}.json").write_text(json.dumps(result.raw, indent=2) + "\n")

        status = FAILED if result.is_error else DONE
        error = (result.text.strip().splitlines() or ["claude reported an error"])[-1] if result.is_error else None
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
        else:
            log.error("%s: claude reported an error: %s", pr.ref, error)
        return Outcome(pr, status, error or "", output_path)

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
        if rec is None or rec.phase != PHASE_REVIEW or not repo.review.comment or rec.started_at is None:
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

    def rerequested_since(self, pr: PullRequest, repo: RepoConfig, last: Record) -> bool:
        """Whether a configured reviewer's review was requested after ``last`` review started.

        GitHub drops a review request once that reviewer posts, so a request that is
        open after our review is normally a re-request. Comparing against the timeline
        guards against a review that posted nothing and so never cleared the request.
        """
        names = filters.requested_reviewers(pr, repo)
        try:
            requested_at = self.gh.review_requested_at(pr.repo, pr.number, names)
        except (proc.CommandError, ValueError) as exc:
            log.warning("%s: could not read the PR timeline: %s", pr.ref, exc)
            return False
        since = last.started_at or last.updated_at
        return requested_at is not None and requested_at > since

    def candidates(self, repo: RepoConfig) -> Iterator[PullRequest]:
        for pr in self.gh.list_open_prs(repo.name):
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

    def watch(self, repos: list[RepoConfig], once: bool = False) -> None:
        reset = self.state.reset_running()
        if reset:
            log.info("marked %d interrupted review(s) for retry", reset)
        while not self.stop.is_set():
            self.poll_once(repos)
            if once:
                break
            self.stop.wait(self.settings.poll_interval)

    def install_signal_handlers(self) -> None:
        def handler(signum: int, _frame: object) -> None:
            if self.stop.is_set():
                raise KeyboardInterrupt
            log.info("received %s; stopping after the current review (repeat to abort it)",
                     signal.Signals(signum).name)
            self.stop.set()

        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)


@contextmanager
def lock(workdir: Path) -> Iterator[None]:
    workdir.mkdir(parents=True, exist_ok=True)
    path = workdir / "rocket-critic.lock"
    with open(path, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AlreadyRunning(f"another rocket-critic is using {workdir}") from None
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
