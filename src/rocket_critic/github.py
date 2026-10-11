"""Thin wrapper over the ``gh`` CLI."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from . import proc


@dataclass
class PullRequest:
    repo: str
    number: int
    title: str
    url: str
    head_sha: str
    base_ref: str
    is_draft: bool
    author: str
    # Only the single-PR endpoint reports these; None when listed in bulk.
    additions: int | None = None
    deletions: int | None = None
    labels: list[str] = field(default_factory=list)
    # Requested reviewers: user logins and team names/slugs.
    review_requests: list[str] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return f"{self.repo}#{self.number}"

    @property
    def size(self) -> int | None:
        if self.additions is None or self.deletions is None:
            return None
        return self.additions + self.deletions

    @classmethod
    def from_rest(cls, repo: str, data: Mapping[str, Any]) -> PullRequest:
        """Build from a REST ``pulls`` object (list or single-PR form)."""
        requests: list[str] = [u["login"] for u in data.get("requested_reviewers") or [] if u.get("login")]
        for team in data.get("requested_teams") or []:
            for key in ("slug", "name"):
                value = team.get(key)
                if value and value not in requests:
                    requests.append(value)
        return cls(
            repo=repo,
            number=int(data["number"]),
            title=data.get("title") or "",
            url=data.get("html_url") or "",
            head_sha=data["head"]["sha"],
            base_ref=(data.get("base") or {}).get("ref") or "",
            is_draft=bool(data.get("draft")),
            author=(data.get("user") or {}).get("login") or "",
            additions=data.get("additions"),
            deletions=data.get("deletions"),
            labels=[lbl["name"] for lbl in data.get("labels") or [] if lbl.get("name")],
            review_requests=requests,
        )


class GitHub:
    def __init__(self, gh: str = "gh", env: Mapping[str, str] | None = None):
        self.gh = gh
        self.env = env

    def _run(self, *args: str, env: Mapping[str, str] | None = None) -> str:
        return proc.run([self.gh, *args], env=env if env is not None else self.env, timeout=120).stdout

    def _json(self, *args: str, env: Mapping[str, str] | None = None) -> Any:
        out = self._run(*args, env=env)
        return json.loads(out) if out.strip() else None

    # REST rather than GraphQL (`gh pr list --json`): cheaper on rate limits and
    # usable with tokens and proxies that only allow the REST API.

    def list_open_prs(self, repo: str) -> list[PullRequest]:
        pages = self._json("api", "--paginate", "--slurp", f"repos/{repo}/pulls?state=open&per_page=100")
        return [PullRequest.from_rest(repo, item) for page in pages or [] for item in page]

    def get_pr(self, repo: str, number: int) -> PullRequest:
        return PullRequest.from_rest(repo, self._json("api", f"repos/{repo}/pulls/{number}"))

    def reviewed_shas(self, repo: str, number: int, login: str, env: Mapping[str, str] | None = None) -> set[str]:
        """Head SHAs ``login`` has already left a review (or review comment) on."""
        pages = self._json(
            "api", "--paginate", "--slurp", f"repos/{repo}/pulls/{number}/reviews", env=env
        )
        shas: set[str] = set()
        for page in pages or []:
            for review in page:
                if (review.get("user") or {}).get("login", "").lower() == login.lower() and review.get("commit_id"):
                    shas.add(review["commit_id"])
        return shas

    def whoami(self, env: Mapping[str, str] | None = None) -> str | None:
        """Login of the authenticated identity, or None (e.g. an app token without /user access)."""
        try:
            return self._run("api", "user", "--jq", ".login", env=env).strip() or None
        except proc.CommandError:
            return None

    def auth_status(self) -> tuple[bool, str]:
        res = proc.run([self.gh, "auth", "status"], env=self.env, timeout=60, check=False)
        return res.returncode == 0, (res.stdout + res.stderr).strip()

