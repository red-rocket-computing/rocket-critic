"""Local checkouts: one bare clone per repo, one detached worktree per PR head."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from . import proc

log = logging.getLogger(__name__)

DEFAULT_REF = "refs/rocket-critic/default"


class Workspace:
    def __init__(self, workdir: Path, git: str = "git", remote_base: str = "https://github.com/"):
        self.workdir = Path(workdir)
        self.git = git
        self.remote_base = remote_base

    def ensure_layout(self, repos: list[str] = ()) -> list[Path]:
        """Create the workdir and its repos/ prs/ runs/ directories, plus per-repo ones for ``repos``.

        Returns the directories that did not exist before.
        """
        dirs = [self.workdir, self.workdir / "repos", self.workdir / "prs", self.workdir / "runs"]
        for repo in repos:
            dirs += [self.bare_path(repo).parent, self.workdir / "prs" / repo, self.workdir / "runs" / repo]
        created = [d for d in dirs if not d.exists()]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)
        return created

    def bare_path(self, repo: str) -> Path:
        return self.workdir / "repos" / f"{repo}.git"

    def worktree_path(self, repo: str, number: int, head_sha: str) -> Path:
        return self.workdir / "prs" / repo / f"{number}-{head_sha[:12]}"

    def _git(self, *args: str, cwd: Path | None = None, timeout: float = 900) -> str:
        return proc.run([self.git, *args], cwd=cwd, timeout=timeout).stdout

    def ensure_clone(self, repo: str) -> Path:
        bare = self.bare_path(repo)
        if not (bare / "HEAD").exists():
            bare.parent.mkdir(parents=True, exist_ok=True)
            url = f"{self.remote_base}{repo}.git"
            log.info("cloning %s into %s", url, bare)
            # The credential helper goes in via -c so the initial clone of a
            # private repo can authenticate, then is persisted for fetches.
            self._git("-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
                      "clone", "--bare", "--filter=blob:none", url, str(bare))
            self._git("config", "credential.helper", "", cwd=bare)
            self._git("config", "--add", "credential.helper", "!gh auth git-credential", cwd=bare)
        return bare

    def checkout(self, repo: str, number: int, head_sha: str, base_ref: str) -> Path:
        """Fetch PR ``number`` and its base branch, and add a worktree at ``head_sha``."""
        bare = self.ensure_clone(repo)
        # The default branch too: REVIEW.md is read from there, never from the PR.
        refspecs = [f"+refs/pull/{number}/head:refs/rocket-critic/pr/{number}", f"+HEAD:{DEFAULT_REF}"]
        if base_ref:
            refspecs.append(f"+refs/heads/{base_ref}:refs/remotes/origin/{base_ref}")
        self._git("fetch", "--quiet", "origin", *refspecs, cwd=bare)

        path = self.worktree_path(repo, number, head_sha)
        if path.exists():
            self.remove(repo, path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._git("worktree", "add", "--force", "--detach", str(path), head_sha, cwd=bare)
        return path

    def default_file(self, repo: str, path: str) -> str | None:
        """``path`` as it is on the default branch (as of the last checkout), or None if absent."""
        res = proc.run([self.git, "show", f"{DEFAULT_REF}:{path}"], cwd=self.bare_path(repo), timeout=300,
                       check=False)
        return res.stdout if res.returncode == 0 else None

    def diff(self, worktree: Path, base_ref: str) -> str:
        """The pull request's diff as GitHub shows it: the merge base with ``base_ref`` to HEAD."""
        return self._git("-c", "core.quotePath=false", "diff", "--no-color", "--no-ext-diff", "-U3",
                         f"refs/remotes/origin/{base_ref}...HEAD", cwd=worktree, timeout=300)

    def mark_reviewed(self, repo: str, number: int, head_sha: str) -> None:
        """Keep the last reviewed commit reachable so later reviews can diff against it."""
        self._git("update-ref", f"refs/rocket-critic/reviewed/{number}", head_sha, cwd=self.bare_path(repo))

    def is_ancestor(self, repo: str, old: str, new: str) -> bool:
        """Whether ``old`` is an ancestor of ``new`` (False if ``old`` no longer exists upstream)."""
        bare = self.bare_path(repo)
        present = proc.run([self.git, "cat-file", "-e", f"{old}^{{commit}}"], cwd=bare, timeout=60, check=False)
        if present.returncode != 0:
            # Not fetched by this clone (e.g. the state DB outlived it); GitHub
            # serves reachable commits by SHA.
            proc.run([self.git, "fetch", "--quiet", "origin", old], cwd=bare, timeout=300, check=False)
        res = proc.run([self.git, "merge-base", "--is-ancestor", old, new], cwd=bare, timeout=120, check=False)
        return res.returncode == 0

    def remove(self, repo: str, path: Path) -> None:
        bare = self.bare_path(repo)
        try:
            self._git("worktree", "remove", "--force", str(path), cwd=bare, timeout=120)
        except proc.CommandError as exc:
            log.debug("worktree remove failed (%s); deleting directory", exc)
            shutil.rmtree(path, ignore_errors=True)
            self._git("worktree", "prune", cwd=bare, timeout=120)

    def clean(self) -> list[Path]:
        """Remove every PR worktree; returns the paths removed."""
        removed: list[Path] = []
        prs = self.workdir / "prs"
        if not prs.exists():
            return removed
        for owner in sorted(p for p in prs.iterdir() if p.is_dir()):
            for name in sorted(p for p in owner.iterdir() if p.is_dir()):
                repo = f"{owner.name}/{name.name}"
                for wt in sorted(p for p in name.iterdir() if p.is_dir()):
                    if self.bare_path(repo).exists():
                        self.remove(repo, wt)
                    else:
                        shutil.rmtree(wt, ignore_errors=True)
                    removed.append(wt)
        return removed
