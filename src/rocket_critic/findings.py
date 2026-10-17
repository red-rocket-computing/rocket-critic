"""Structured review findings: parse and check what claude wrote, and build the GitHub review.

Used when rocket-critic posts the review itself (``posting = "rocket-critic"``):
claude writes its findings to a JSON file instead of posting them, and every
finding must carry a recognised severity before anything is posted.
"""

from __future__ import annotations

import codecs
import json
import re
from dataclasses import asdict, dataclass
from typing import Any

FINDINGS_FILE = "findings.json"


@dataclass(frozen=True)
class Level:
    key: str
    marker: str
    name: str
    meaning: str
    aliases: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Scale:
    """A set of severity levels, most severe first.

    With ``pre_existing_flag``, "pre-existing" is not a level but a flag on a
    finding that also has a level; otherwise it is a level of its own.
    """

    name: str
    levels: tuple[Level, ...]
    pre_existing_flag: bool = False

    @property
    def keys(self) -> list[str]:
        return [lvl.key for lvl in self.levels]

    def level(self, key: str) -> Level:
        return next(lvl for lvl in self.levels if lvl.key == key)

    def rank(self, key: str) -> int:
        return self.keys.index(key)

    def normalize(self, value: Any) -> str | None:
        """The level ``value`` names, by key or an unambiguous synonym, or None."""
        if not isinstance(value, str):
            return None
        key = re.sub(r"[\s_]+", " ", value.strip().lower()).replace("pre existing", "pre-existing")
        for lvl in self.levels:
            if key == lvl.key or key in lvl.aliases or key.replace("-", " ") in lvl.aliases:
                return lvl.key
        return None


def _level(key: str, marker: str, name: str, meaning: str, *aliases: str) -> Level:
    return Level(key, marker, name, meaning, frozenset(aliases))


PRE_EXISTING_ALIASES = ("preexisting", "pre existing", "existing", "legacy")

# GitHub's hosted Claude Code Review uses these three.
CODE_REVIEW = Scale("code-review", (
    _level("important", "\U0001F534", "Important",
           "a bug, security problem or regression that should be fixed before merging",
           "critical", "high", "major", "blocker", "blocking", "bug", "error", "severe", "security"),
    _level("nit", "\U0001F7E1", "Nit",
           "a minor issue worth fixing but not blocking (naming, style, small clean-ups, unclear comments "
           "or messages)",
           "nitpick", "minor", "low", "style", "suggestion", "trivial", "info", "optional"),
    _level("pre-existing", "\U0001F7E3", "Pre-existing",
           "a real problem in code this pull request did not introduce", *PRE_EXISTING_ALIASES),
))

# The usual five-level scale (issue trackers, security reports).
TRADITIONAL = Scale("traditional", (
    _level("critical", "\U0001F534", "Critical",
           "a security vulnerability, data loss or corruption, or a crash or outage in normal use; must be "
           "fixed before merging",
           "blocker", "severe", "showstopper"),
    _level("high", "\U0001F7E0", "High",
           "a bug that breaks a feature or gives wrong results in common cases; should be fixed before merging",
           "major", "important", "error", "bug", "serious"),
    _level("medium", "\U0001F7E1", "Medium",
           "a bug in edge cases or error handling, or a performance, reliability or maintainability problem "
           "worth fixing soon",
           "moderate", "warning", "normal"),
    _level("low", "\U0001F535", "Low",
           "a minor issue: naming, style, readability, small clean-ups",
           "minor", "nit", "nitpick", "style", "trivial", "cosmetic"),
    _level("info", "⚪", "Info",
           "an observation or suggestion; no change required",
           "informational", "note", "suggestion", "optional", "question"),
), pre_existing_flag=True)

SCALES = {scale.name: scale for scale in (CODE_REVIEW, TRADITIONAL)}

# A severity label the model put at the start of a title or body; rocket-critic adds its own.
_MARKERS = sorted({lvl.marker for scale in SCALES.values() for lvl in scale.levels}, key=len, reverse=True)
_NAMES = sorted({lvl.name.lower() for scale in SCALES.values() for lvl in scale.levels}, key=len, reverse=True)
# The parts of a severity label: the name must be a whole word, and only counts as a
# label when a marker, bold or a separator says so ("Information leak", "Low-level
# retry" and "Important config is ignored" are titles, not labels).
_LEADING_LABEL = re.compile(
    r"^\s*(?P<marker>" + "|".join(map(re.escape, _MARKERS)) + r")?️?\s*(?P<open>\*\*)?\s*(?:"
    + "|".join(map(re.escape, _NAMES)) + r")\b(?P<close>\s*\*\*)?\s*(?P<pre>\(pre-existing\))?\s*"
    r"(?P<sep>:|[\-–—](?=\s))?(?P<after>\s*\*\*)?\s*",
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
    pre_existing: bool = False  # with a scale whose pre-existing is a flag
    scale: str = CODE_REVIEW.name

    @property
    def label(self) -> str:
        lvl = SCALES[self.scale].level(self.severity)
        return f"{lvl.marker} **{lvl.name}**" + (" (pre-existing)" if self.pre_existing else "")

    def where(self) -> str:
        lines = f"{self.start_line}-{self.line}" if self.start_line else str(self.line)
        return f"`{self.path}:{lines}`"


def normalize_severity(value: Any, scale: Scale = CODE_REVIEW) -> str | None:
    """The level of ``scale`` that ``value`` names, or None if unrecognised."""
    return scale.normalize(value)


def extract_json(text: str) -> str | None:
    """The findings object from claude's final reply, when it printed it instead of writing the file."""
    for block in reversed(_FENCED_JSON.findall(text)):
        if '"findings"' in block:
            return block
    return None


def parse_findings(text: str, scale: Scale = CODE_REVIEW) -> tuple[str, list[Finding]]:
    """Parse and check a findings document against ``scale``; returns (summary, findings).

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
        pre_existing = raw.get("pre_existing", False)
        if not isinstance(pre_existing, bool):
            errs.append("pre_existing must be true or false")
            pre_existing = False
        severity = scale.normalize(raw.get("severity"))
        if severity is None:
            msg = f"severity {raw.get('severity')!r} is not one of {', '.join(scale.keys)}"
            if scale.pre_existing_flag and CODE_REVIEW.normalize(raw.get("severity")) == "pre-existing":
                msg += ' (give a level and set "pre_existing": true)'
            errs.append(msg)
        elif not scale.pre_existing_flag:
            if pre_existing:
                severity = "pre-existing"  # this scale has it as a level instead of a flag
            pre_existing = False
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
                                _strip_label(body), start_line, pre_existing, scale.name))
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
    """``text`` without a severity label the model put in front ("🟡 **Nit**: ...")."""
    text = text.strip()
    m = _LEADING_LABEL.match(text)
    if m and (m["marker"] or (m["open"] and (m["close"] or m["after"])) or m["sep"]):
        return text[m.end():].strip()
    return text


def _unquote(path: str) -> str:
    """A path as git prints it, without the C-style quoting it uses for unusual names."""
    if len(path) < 2 or not (path.startswith('"') and path.endswith('"')):
        return path
    return codecs.escape_decode(path[1:-1].encode())[0].decode("utf-8", "replace")


def commentable_lines(diff: str) -> dict[str, set[int]]:
    """Lines of each file that a review comment can be attached to: the new-side
    lines (added or context) shown in the diff's hunks.

    Hunk lengths come from the ``@@`` headers, so an added line that itself starts
    with ``++ `` (shown as ``+++ ...``) is never taken for a file header.
    """
    lines: dict[str, set[int]] = {}
    current: set[int] | None = None
    new_line = old_left = new_left = 0
    for row in diff.splitlines():
        # Inside a hunk every row starts with "+", "-", " " or a backslash, so these two are always headers.
        if row.startswith("diff --git "):
            current = None
            old_left = new_left = 0
        elif row.startswith("@@"):
            m = re.match(r"@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", row)
            if m:
                old_left = int(m.group(1) or 1)
                new_line = int(m.group(2))
                new_left = int(m.group(3) or 1)
        elif old_left > 0 or new_left > 0:
            tag = row[:1]
            if tag == "-":
                old_left -= 1
                continue
            if tag == "+":
                new_left -= 1
            elif tag in (" ", ""):
                old_left -= 1
                new_left -= 1
            else:  # "\ No newline at end of file"
                continue
            if current is not None:
                current.add(new_line)
            new_line += 1
        elif row.startswith("+++ "):
            target = _unquote(row[4:].rstrip("\t").strip())
            current = None if target == "/dev/null" else lines.setdefault(target.removeprefix("b/"), set())
    return lines


@dataclass
class Review:
    """A GitHub review ready to post, and what went where."""

    payload: dict[str, Any]
    inline: list[Finding]
    outside: list[Finding]  # not on a line of the diff: listed in the review body
    held: list[Finding]  # below min_severity: not posted

    def to_json(self) -> dict[str, Any]:
        return {"payload": self.payload, "inline": [asdict(f) for f in self.inline],
                "outside": [asdict(f) for f in self.outside], "held": [asdict(f) for f in self.held]}


def counts(findings: list[Finding], scale: Scale = CODE_REVIEW) -> str:
    """E.g. "1 🔴 Important · 0 🟡 Nit · 2 🟣 Pre-existing"."""
    parts = [f"{sum(f.severity == lvl.key for f in findings)} {lvl.marker} {lvl.name}" for lvl in scale.levels]
    old = sum(f.pre_existing for f in findings)
    if old:
        parts.append(f"{old} pre-existing")
    return " \u00b7 ".join(parts)


def comment_body(f: Finding) -> str:
    head = f"{f.label}: {f.title}" if f.title else f.label
    return f"{head}\n\n{f.body}" if f.body else head


def build_review(summary: str, findings: list[Finding], commentable: dict[str, set[int]], commit_id: str,
                 min_severity: str = "", header: str = "", inline: bool = True,
                 scale: Scale = CODE_REVIEW) -> Review:
    """One review (event COMMENT) with an inline comment per finding on a diff line; the
    rest are listed in the body, so GitHub never rejects the review over a line number.
    ``inline=False`` lists every finding in the body (the fallback if GitHub still refuses one).
    Findings less severe than ``min_severity`` are left out."""
    cutoff = scale.rank(min_severity) if min_severity else len(scale.levels)
    held = [f for f in findings if scale.rank(f.severity) > cutoff]
    shown = [f for f in findings if scale.rank(f.severity) <= cutoff]
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

    body = [header or "**Code review**", "", counts(shown, scale) if shown else "No issues found."]
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
        lvl = scale.level(min_severity)
        body += ["", f"{len(held)} finding(s) less severe than {lvl.marker} {lvl.name} not posted."]
    payload = {"commit_id": commit_id, "event": "COMMENT", "body": "\n".join(body), "comments": comments}
    return Review(payload, placed, outside, held)

