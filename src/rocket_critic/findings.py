"""Structured review findings: parse and check what claude wrote, and build the GitHub review.

Used when rocket-critic posts the review itself (``posting = "rocket-critic"``):
claude writes its findings to a JSON file instead of posting them, and every
finding must carry a recognised severity before anything is posted.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any

FINDINGS_FILE = "findings.json"

IMPORTANT = "important"
NIT = "nit"
PRE_EXISTING = "pre-existing"
# Severity -> (marker, name), in the order they are listed.
SEVERITIES = {
    IMPORTANT: ("\U0001F534", "Important"),
    NIT: ("\U0001F7E1", "Nit"),
    PRE_EXISTING: ("\U0001F7E3", "Pre-existing"),
}
# Other words models use for the same levels. Anything else is rejected rather than guessed.
_ALIASES = {
    IMPORTANT: {"important", "critical", "high", "major", "blocker", "blocking", "bug", "error", "severe",
                "security"},
    NIT: {"nit", "nitpick", "minor", "low", "style", "suggestion", "trivial", "info", "optional"},
    PRE_EXISTING: {"pre-existing", "preexisting", "pre existing", "existing", "legacy"},
}
_ALIAS = {alias: sev for sev, aliases in _ALIASES.items() for alias in aliases}

# A severity label the model put at the start of a title or body; rocket-critic adds its own.
_LEADING_LABEL = re.compile(
    r"^\s*(?:[\U0001F534\U0001F7E1\U0001F7E3]\s*)?(?:\*\*)?\s*(?:important|nit|pre-existing)\s*(?:\*\*)?"
    r"\s*(?:[:\-–—]\s*)?(?:\*\*)?\s*",
    re.I,
)
_FENCED_JSON = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.S)


class FindingsError(ValueError):
    pass


@dataclass
class Finding:
    path: str
    line: int
    severity: str
    title: str
    body: str
    start_line: int | None = None

    @property
    def label(self) -> str:
        marker, name = SEVERITIES[self.severity]
        return f"{marker} **{name}**"

    def where(self) -> str:
        lines = f"{self.start_line}-{self.line}" if self.start_line else str(self.line)
        return f"`{self.path}:{lines}`"


def normalize_severity(value: Any) -> str | None:
    """The severity ``value`` names (important, nit or pre-existing), or None if unrecognised."""
    if not isinstance(value, str):
        return None
    key = re.sub(r"[\s_]+", " ", value.strip().lower()).replace("pre existing", "pre-existing")
    return _ALIAS.get(key) or _ALIAS.get(key.replace("-", " "))


def extract_json(text: str) -> str | None:
    """The findings object from claude's final reply, when it printed it instead of writing the file."""
    for block in reversed(_FENCED_JSON.findall(text)):
        if '"findings"' in block:
            return block
    return None


def parse_findings(text: str) -> tuple[str, list[Finding]]:
    """Parse and check a findings document; returns (summary, findings).

    Raises :class:`FindingsError` listing every problem, so nothing is posted
    from a document that is only partly right.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FindingsError(f"findings are not valid JSON: {exc}") from None
    if isinstance(data, list):
        data = {"findings": data}
    if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
        raise FindingsError('findings must be an object with a "findings" list')
    summary = data.get("summary") or ""
    if not isinstance(summary, str):
        raise FindingsError('"summary" must be a string')

    problems: list[str] = []
    findings: list[Finding] = []
    for i, raw in enumerate(data["findings"], 1):
        if not isinstance(raw, dict):
            problems.append(f"finding {i} is not an object")
            continue
        errs: list[str] = []
        path = raw.get("path")
        if not isinstance(path, str) or not path.strip():
            errs.append("no path")
        line = _int(raw.get("line"))
        if line is None:
            errs.append(f"line {raw.get('line')!r} is not a positive number")
        start = raw.get("start_line")
        start_line = _int(start) if start not in (None, "") else None
        if start not in (None, "") and start_line is None:
            errs.append(f"start_line {start!r} is not a positive number")
        severity = normalize_severity(raw.get("severity"))
        if severity is None:
            errs.append(f"severity {raw.get('severity')!r} is not one of {', '.join(SEVERITIES)}")
        title = raw.get("title") or ""
        body = raw.get("body") or ""
        if not isinstance(title, str) or not isinstance(body, str):
            errs.append("title and body must be strings")
        elif not (title.strip() or body.strip()):
            errs.append("no title or body")
        if errs:
            problems.append(f"finding {i} ({path or '?'}): {'; '.join(errs)}")
            continue
        if start_line is not None and start_line >= line:
            start_line = None
        findings.append(Finding(path.strip().removeprefix("./"), line, severity, _strip_label(title),
                                _strip_label(body), start_line))
    if problems:
        raise FindingsError("; ".join(problems))
    return summary.strip(), findings


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value)
    return value if isinstance(value, int) and value > 0 else None


def _strip_label(text: str) -> str:
    return _LEADING_LABEL.sub("", text.strip(), count=1).strip()


def commentable_lines(diff: str) -> dict[str, set[int]]:
    """Lines of each file that a review comment can be attached to: the new-side
    lines (added or context) shown in the diff's hunks."""
    lines: dict[str, set[int]] = {}
    current: set[int] | None = None
    new_line = 0
    for row in diff.splitlines():
        if row.startswith("diff --git "):
            current = None
        elif row.startswith("+++ "):
            target = row[4:].strip()
            current = None if target == "/dev/null" else lines.setdefault(target.removeprefix("b/"), set())
        elif row.startswith("@@"):
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", row)
            new_line = int(m.group(1)) if m else 0
        elif current is not None and new_line:
            if row.startswith("+"):
                current.add(new_line)
                new_line += 1
            elif row.startswith(" ") or row == "":
                current.add(new_line)
                new_line += 1
            # "-" rows and "\ No newline at end of file" don't advance the new side.
    return lines


@dataclass
class Review:
    """A GitHub review ready to post, and what went where."""

    payload: dict[str, Any]
    inline: list[Finding]
    outside: list[Finding]  # not on a line of the diff: listed in the review body
    nits_held: int  # Nit findings left out (post_nits = false)

    def to_json(self) -> dict[str, Any]:
        return {"payload": self.payload, "inline": [asdict(f) for f in self.inline],
                "outside": [asdict(f) for f in self.outside], "nits_held": self.nits_held}


def counts(findings: list[Finding]) -> str:
    parts = []
    for sev, (marker, name) in SEVERITIES.items():
        parts.append(f"{sum(f.severity == sev for f in findings)} {marker} {name}")
    return " · ".join(parts)


def comment_body(f: Finding) -> str:
    head = f"{f.label}: {f.title}" if f.title else f.label
    return f"{head}\n\n{f.body}" if f.body else head


def build_review(summary: str, findings: list[Finding], commentable: dict[str, set[int]], commit_id: str,
                 post_nits: bool = True, header: str = "", inline: bool = True) -> Review:
    """One review (event COMMENT) with an inline comment per finding on a diff line; the
    rest are listed in the body, so GitHub never rejects the review over a line number.
    ``inline=False`` lists every finding in the body (the fallback if GitHub still refuses one)."""
    held = [f for f in findings if f.severity == NIT and not post_nits]
    shown = [f for f in findings if f not in held]
    placed, outside, comments = [], [], []
    for f in shown:
        lines = commentable.get(f.path, set()) if inline else set()
        if f.line not in lines:
            outside.append(f)
            continue
        comment: dict[str, Any] = {"path": f.path, "line": f.line, "side": "RIGHT", "body": comment_body(f)}
        if f.start_line and all(n in lines for n in range(f.start_line, f.line)):
            comment.update(start_line=f.start_line, start_side="RIGHT")
        comments.append(comment)
        placed.append(f)

    body = [header or "**Code review**", "", counts(shown) if shown else "No issues found."]
    if summary:
        body += ["", summary]
    if outside:
        body += ["", "### Findings outside the diff" if inline else "### Findings", ""]
        for f in outside:
            text = f"- {f.label} {f.where()}" + (f": {f.title}" if f.title else "")
            if f.body:
                text += "\n\n" + "\n".join("  " + row if row else "" for row in f.body.splitlines())
            body.append(text)
    if held:
        body += ["", f"{len(held)} {SEVERITIES[NIT][0]} Nit finding(s) not posted."]
    payload = {"commit_id": commit_id, "event": "COMMENT", "body": "\n".join(body), "comments": comments}
    return Review(payload, placed, outside, len(held))

