"""Run Claude Code's built-in ``/code-review`` command in print mode."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import proc
from .config import ClaudeConfig, ReviewConfig
from .github import Thread

log = logging.getLogger(__name__)


class ReviewError(RuntimeError):
    pass


@dataclass
class ReviewResult:
    text: str
    is_error: bool = False
    cost_usd: float | None = None
    duration_ms: int | None = None
    num_turns: int | None = None
    permission_denials: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


MAX_THREADS_CHARS = 20_000


def review_prompt(number: int, review: ReviewConfig, *, comment: bool | None = None, context: str = "") -> str:
    """The ``/code-review`` command line.

    Re-review context goes after the arguments: /code-review reads its
    arguments as free text and follows it there, while it ignores
    ``--append-system-prompt``.
    """
    comment = review.comment if comment is None else comment
    parts = ["/code-review", str(number), review.level]
    if review.max_findings:
        parts += ["--max-findings", review.max_findings]
    if comment:
        parts.append("--comment")
    prompt = " ".join(parts)
    return f"{prompt}\n\n{context}" if context else prompt


def rereview_context(last_sha: str, mode: str, threads: list[Thread]) -> str:
    """Instructions that keep a re-review from repeating or contradicting earlier reviews.

    ``mode``: "range" (``last_sha`` is an ancestor of the head: only review
    what changed since), "rewritten" (history was rewritten: review it all)
    or "repeat" (the same commit is being reviewed again).
    """
    lines = ["## Re-review context (from rocket-critic)", ""]
    if mode == "range":
        lines.append(
            f"This pull request was already reviewed at commit {last_sha}. Only report findings on lines "
            f"changed since then (see `git diff {last_sha} HEAD`). Do not report anything in code that was "
            f"not changed after {last_sha}."
        )
    elif mode == "rewritten":
        lines.append(
            f"This pull request was already reviewed at commit {last_sha}, but that commit is not an "
            "ancestor of the current head (history was rewritten), so review the whole pull request."
        )
    else:
        lines.append(f"This commit ({last_sha}) was already reviewed; it is being reviewed again in full.")
    if threads:
        lines += [
            "",
            "Earlier review threads on this pull request. Do not repeat these findings or contradict them. "
            "If the author replied disagreeing or declining, treat the point as settled. Do not comment "
            "again on findings that have been fixed.",
            "",
        ]
        used = 0
        for t in threads:
            where = f"{t.path}:{t.line}" if t.line else f"{t.path} (outdated)"
            block = [f"- {where} ({t.author}): {_one_line(t.body)}"]
            block += [f"  - reply ({author}): {_one_line(body)}" for author, body in t.replies]
            text = "\n".join(block)
            if used + len(text) > MAX_THREADS_CHARS:
                lines.append(f"- ... {len(threads) - threads.index(t)} more thread(s) omitted")
                break
            lines.append(text)
            used += len(text)
    return "\n".join(lines)


def _one_line(text: str, limit: int = 600) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def build_command(prompt: str, claude: ClaudeConfig) -> list[str]:
    cmd = [claude.binary, "-p", prompt, "--output-format", "json", "--no-session-persistence"]
    if claude.model:
        cmd += ["--model", claude.model]
    if claude.permission_mode:
        cmd += ["--permission-mode", claude.permission_mode]
    # Project/local settings and MCP config come from the PR under review and
    # are untrusted, so only the user's own settings are honoured.
    cmd += ["--setting-sources", "user", "--strict-mcp-config"]
    if claude.allowed_tools:
        cmd += ["--allowedTools", ",".join(claude.allowed_tools)]
    if claude.disallowed_tools:
        cmd += ["--disallowedTools", ",".join(claude.disallowed_tools)]
    cmd += claude.extra_args
    return cmd


def build_env(claude: ClaudeConfig, repo: str, base: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    env.update(claude.resolved_env(dict(env)))
    env["GH_REPO"] = repo
    return env


def parse_envelope(stdout: str) -> ReviewResult:
    """Parse ``claude -p --output-format json`` output."""
    data = None
    text = stdout.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Some versions print a JSON array of events; take the final result event.
        for line in reversed(text.splitlines()):
            try:
                data = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    if isinstance(data, list):
        data = next((ev for ev in reversed(data) if isinstance(ev, dict) and ev.get("type") == "result"), None)
    if not isinstance(data, dict):
        raise ReviewError(f"could not parse claude output: {text[:200]!r}")

    denials = []
    for d in data.get("permission_denials") or []:
        name = d.get("tool_name") or "?"
        inp = d.get("tool_input") or {}
        detail = inp.get("command") or inp.get("file_path") or ""
        denials.append(f"{name}({detail})" if detail else name)

    return ReviewResult(
        text=data.get("result") or "",
        is_error=bool(data.get("is_error")) or data.get("subtype", "success") != "success",
        cost_usd=data.get("total_cost_usd"),
        duration_ms=data.get("duration_ms"),
        num_turns=data.get("num_turns"),
        permission_denials=denials,
        raw=data,
    )


def run_review(
    worktree: Path,
    repo: str,
    number: int,
    claude: ClaudeConfig,
    review: ReviewConfig,
    *,
    comment: bool | None = None,
    context: str = "",
) -> ReviewResult:
    prompt = review_prompt(number, review, comment=comment, context=context)
    cmd = build_command(prompt, claude)
    env = build_env(claude, repo)
    log.info("%s#%d: running `%s`%s (model=%s)", repo, number, prompt.split("\n", 1)[0],
             " with re-review context" if context else "", claude.model or "default")
    try:
        res = proc.run(cmd, cwd=worktree, env=env, timeout=claude.timeout, check=False)
    except proc.CommandError as exc:
        raise ReviewError(f"claude timed out after {claude.timeout}s") from exc
    except FileNotFoundError as exc:
        raise ReviewError(f"claude binary not found: {claude.binary}") from exc

    try:
        result = parse_envelope(res.stdout)
    except ReviewError:
        detail = (res.stderr or res.stdout).strip()[-500:]
        raise ReviewError(f"claude exited {res.returncode}: {detail}") from None
    if res.returncode != 0 and not result.is_error:
        result.is_error = True
    if result.permission_denials:
        log.warning(
            "%s#%d: claude was denied %d tool call(s): %s -- add them to claude.allowed_tools if needed",
            repo, number, len(result.permission_denials), "; ".join(result.permission_denials),
        )
    return result
