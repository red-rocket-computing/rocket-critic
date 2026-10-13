"""Thin wrapper over the ``gh`` CLI."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
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


@dataclass
class Thread:
    """An inline review comment and its replies."""

    path: str
    line: int | None  # None when the comment is outdated (its code changed since)
    author: str
    body: str
    replies: list[tuple[str, str]] = field(default_factory=list)  # (author, body)


class GitHub:
    def __init__(self, gh: str = "gh", env: Mapping[str, str] | None = None):
        self.gh = gh
        self.env = env

    def _run(self, *args: str, env: Mapping[str, str] | None = None) -> str:
        return proc.run([self.gh, *args], env=env if env is not None else self.env, timeout=120).stdout

    def _json(self, *args: str, env: Mapping[str, str] | None = None) -> Any:
        out = self._run(*args, env=env)
        return json.loads(out) if out.strip() else None

    def _paginate(self, path: str, env: Mapping[str, str] | None = None) -> list[Any]:
        """All items of a paginated list endpoint.

        Avoids ``--slurp``, which only exists in gh 2.48+: without it gh
        prints one JSON array per page, back to back.
        """
        return [item for page in parse_pages(self._run("api", "--paginate", path, env=env)) for item in page]

    # REST rather than GraphQL (`gh pr list --json`): cheaper on rate limits and
    # usable with tokens and proxies that only allow the REST API.

    def list_open_prs(self, repo: str) -> list[PullRequest]:
        return [PullRequest.from_rest(repo, item) for item in self._paginate(f"repos/{repo}/pulls?state=open&per_page=100")]

    def get_pr(self, repo: str, number: int) -> PullRequest:
        return PullRequest.from_rest(repo, self._json("api", f"repos/{repo}/pulls/{number}"))

    def reviewed_shas(self, repo: str, number: int, login: str, env: Mapping[str, str] | None = None) -> set[str]:
        """Head SHAs ``login`` has already left a review (or review comment) on."""
        shas: set[str] = set()
        for review in self._paginate(f"repos/{repo}/pulls/{number}/reviews?per_page=100", env=env):
            if (review.get("user") or {}).get("login", "").lower() == login.lower() and review.get("commit_id"):
                shas.add(review["commit_id"])
        return shas

    def posted_since(self, repo: str, number: int, login: str, since: float) -> bool:
        """Whether ``login`` posted a review, review comment or PR comment at or after ``since``."""
        login = login.lower()
        stamp = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        sources = (
            (f"repos/{repo}/pulls/{number}/reviews?per_page=100", "submitted_at"),
            (f"repos/{repo}/pulls/{number}/comments?per_page=100&since={stamp}", "created_at"),
            (f"repos/{repo}/issues/{number}/comments?per_page=100&since={stamp}", "created_at"),
        )
        for path, time_key in sources:
            for item in self._paginate(path):
                if (item.get("user") or {}).get("login", "").lower() != login:
                    continue
                if _ts(item.get(time_key)) >= int(since):
                    return True
        return False

    def review_threads(self, repo: str, number: int) -> list[Thread]:
        """Inline review threads on the PR, oldest first."""
        threads: dict[int, Thread] = {}
        replies: list[dict[str, Any]] = []
        for c in self._paginate(f"repos/{repo}/pulls/{number}/comments?per_page=100"):
            if c.get("in_reply_to_id"):
                replies.append(c)
            else:
                threads[c["id"]] = Thread(
                    path=c.get("path") or "",
                    line=c.get("line"),
                    author=(c.get("user") or {}).get("login") or "",
                    body=c.get("body") or "",
                )
        for c in replies:
            thread = threads.get(c["in_reply_to_id"])
            if thread is not None:
                thread.replies.append(((c.get("user") or {}).get("login") or "", c.get("body") or ""))
        return list(threads.values())

    def whoami(self, env: Mapping[str, str] | None = None) -> str | None:
        """Login of the authenticated identity, or None (e.g. an app token without /user access)."""
        try:
            return self._run("api", "user", "--jq", ".login", env=env).strip() or None
        except proc.CommandError:
            return None

    def auth_status(self) -> tuple[bool, str]:
        res = proc.run([self.gh, "auth", "status"], env=self.env, timeout=60, check=False)
        return res.returncode == 0, (res.stdout + res.stderr).strip()


def parse_pages(text: str) -> list[Any]:
    """Decode back-to-back JSON values (one per page) from ``gh api --paginate`` output."""
    decoder = json.JSONDecoder()
    pages: list[Any] = []
    pos = 0
    while True:
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            return pages
        page, pos = decoder.raw_decode(text, pos)
        pages.append(page)


def _ts(value: str | None) -> int:
    """Seconds since the epoch for a GitHub ISO-8601 timestamp (0 if missing)."""
    if not value:
        return 0
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
