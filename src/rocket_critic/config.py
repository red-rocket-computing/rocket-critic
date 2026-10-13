"""Configuration loading.

The config is a TOML file with global ``[claude]`` and ``[review]`` tables and
one ``[[repos]]`` entry per watched repository. A repo may carry its own
``[repos.claude]`` / ``[repos.review]`` tables which are merged over the
globals (``env`` is merged key by key).
"""

from __future__ import annotations

import copy
import os
import re
import tomllib
from importlib import resources
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LEVELS = ("low", "medium", "high", "xhigh", "max")
LABEL_MODES = ("sticky", "request")
MIN_POLL_INTERVAL = 10  # seconds

CONFIG_NAME = "config.toml"
DEFAULT_WORKDIR = "~/.local/share/rocket-critic"


def user_config_path() -> Path:
    """``$XDG_CONFIG_HOME/rocket-critic/config.toml`` (default ``~/.config/rocket-critic/config.toml``)."""
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "rocket-critic" / CONFIG_NAME


DEFAULT_CONFIG_PATH = user_config_path()

DEFAULT_ALLOWED_TOOLS = [
    "Read",
    "Grep",
    "Glob",
    "Agent",
    "Skill",
    "TodoWrite",
    "Bash(gh pr view:*)",
    "Bash(gh pr diff:*)",
    "Bash(gh pr checks:*)",
    "Bash(gh api:*)",
    "Bash(git diff:*)",
    "Bash(git log:*)",
    "Bash(git show:*)",
    "Bash(git status:*)",
    "Bash(git blame:*)",
    "Bash(git merge-base:*)",
    "Bash(git rev-parse:*)",
    "Bash(git ls-files:*)",
    "Bash(git remote -v)",
    "Bash(echo:*)",
]

# Edit/Write are allowed only inside each review's scratch directory (added per run);
# with permission_mode = "dontAsk" every other write is refused.
DEFAULT_DISALLOWED_TOOLS = ["NotebookEdit", "WebFetch", "WebSearch"]

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(ValueError):
    pass


@dataclass
class ClaudeConfig:
    binary: str = "claude"
    model: str = ""
    timeout: int = 2700
    permission_mode: str = "dontAsk"
    allowed_tools: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_TOOLS))
    disallowed_tools: list[str] = field(default_factory=lambda: list(DEFAULT_DISALLOWED_TOOLS))
    extra_args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)

    def resolved_env(self, environ: dict[str, str] | None = None) -> dict[str, str]:
        """``env`` with ``${VAR}`` references expanded.

        A value that references an unset or empty variable is dropped entirely,
        so an optional ``GH_TOKEN = "${ROCKET_CRITIC_GH_TOKEN}"`` falls back to
        the normal gh login instead of becoming an empty token. Literal empty
        values (e.g. ``ANTHROPIC_API_KEY = ""``) are kept.
        """
        environ = os.environ if environ is None else environ
        out: dict[str, str] = {}
        for key, value in self.env.items():
            if key in self.unresolved_env(environ):
                continue
            out[key] = _ENV_REF_RE.sub(lambda m: environ[m.group(1)], value)
        return out

    def unresolved_env(self, environ: dict[str, str] | None = None) -> dict[str, list[str]]:
        """Keys of ``env`` dropped by :meth:`resolved_env`, with the unset or empty variables they reference."""
        environ = os.environ if environ is None else environ
        out: dict[str, list[str]] = {}
        for key, value in self.env.items():
            missing = [name for name in _ENV_REF_RE.findall(value) if not environ.get(name)]
            if missing:
                out[key] = missing
        return out


@dataclass
class ReviewConfig:
    level: str = "medium"
    max_findings: str = ""
    comment: bool = True
    max_diff_lines: int = 5000
    rereview_on_push: bool = True
    max_attempts: int = 3
    keep_worktrees: bool = False
    max_reviews_per_pr: int = 5
    settle_seconds: int = 0
    incremental: bool = True


@dataclass
class RepoConfig:
    name: str
    claude: ClaudeConfig
    review: ReviewConfig
    skip_drafts: bool = True
    labels: list[str] = field(default_factory=list)
    reviewers: list[str] = field(default_factory=list)
    ignore_authors: list[str] = field(default_factory=list)
    base_branches: list[str] = field(default_factory=list)
    # "sticky": every push while labelled is reviewed; "request": a label asks for one review
    # and is removed afterwards, so adding it again asks for another.
    label_mode: str = "sticky"
    # PR comment lines (e.g. "/review") that request a review, from people with write access.
    commands: list[str] = field(default_factory=list)

    @property
    def owner(self) -> str:
        return self.name.split("/", 1)[0]

    @property
    def repo(self) -> str:
        return self.name.split("/", 1)[1]


@dataclass
class GitHubAppConfig:
    """A GitHub App that /code-review posts as (``[github_app]``)."""

    app_id: str
    private_key: Path
    installation_id: int | None = None  # default: looked up per repo
    api_url: str = "https://api.github.com"


@dataclass
class Settings:
    workdir: Path
    poll_interval: int
    claude: ClaudeConfig
    review: ReviewConfig
    repos: list[RepoConfig]
    path: Path | None = None
    github_app: GitHubAppConfig | None = None

    def repo(self, name: str) -> RepoConfig:
        """The configured repo ``name``, or one built from the globals if unconfigured."""
        for r in self.repos:
            if r.name.lower() == name.lower():
                return r
        _check_repo_name(name)
        return RepoConfig(name=name, claude=copy.deepcopy(self.claude), review=copy.deepcopy(self.review))


def config_path(path: Path | None = None, workdir: Path | None = None) -> Path:
    """Where ``init`` writes the config: ``path``, else ``<workdir>/config.toml``, else the user config."""
    if path:
        return Path(path).expanduser()
    if workdir:
        return Path(workdir).expanduser() / CONFIG_NAME
    return user_config_path()


def config_candidates(workdir: Path | None = None, cwd: Path | None = None) -> list[Path]:
    """Config files searched, in order: the workdir, the current directory, the user config directory."""
    paths = []
    if workdir:
        paths.append(Path(workdir).expanduser() / CONFIG_NAME)
    paths.append(Path(cwd or Path.cwd()) / CONFIG_NAME)
    paths.append(user_config_path())
    unique: list[Path] = []
    for p in paths:
        if all(p.resolve() != q.resolve() for q in unique):
            unique.append(p)
    return unique


def find_config(path: Path | None = None, workdir: Path | None = None, cwd: Path | None = None) -> Path:
    """``path`` if given, else the first existing file from :func:`config_candidates`."""
    if path:
        return Path(path).expanduser()
    candidates = config_candidates(workdir, cwd)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    tried = ", ".join(str(c) for c in candidates)
    raise ConfigError(f"no config file found (looked for {tried}); run `rocket-critic init`")


def load(path: Path | None = None, workdir: Path | None = None) -> Settings:
    """Load the config chosen by :func:`find_config`.

    A relative ``workdir`` in the file is relative to the file's directory, so
    ``workdir = "."`` keeps a config next to its repo/PR directories. An
    explicit ``workdir`` argument overrides the file's value.
    """
    path = find_config(path, workdir)
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path} (run `rocket-critic init`)") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from None
    settings = parse(data)
    settings.path = path
    app = settings.github_app
    if app and not app.private_key.is_absolute():
        app.private_key = (path.parent / app.private_key).resolve()
    if workdir:
        settings.workdir = Path(workdir).expanduser().resolve()
    elif not settings.workdir.is_absolute():
        settings.workdir = (path.parent / settings.workdir).resolve()
    return settings


def parse(data: dict[str, Any]) -> Settings:
    data = dict(data)
    claude_raw = _table(data.pop("claude", {}), "claude")
    review_raw = _table(data.pop("review", {}), "review")
    repos_raw = data.pop("repos", [])
    app_raw = data.pop("github_app", None)
    workdir = Path(_str(data.pop("workdir", DEFAULT_WORKDIR), "workdir")).expanduser()
    poll_interval = _int(data.pop("poll_interval", 300), "poll_interval", minimum=MIN_POLL_INTERVAL)
    if data:
        raise ConfigError(f"unknown top-level keys: {', '.join(sorted(data))}")

    claude = _claude(claude_raw, "claude")
    review = _review(review_raw, "review")

    if not isinstance(repos_raw, list):
        raise ConfigError("repos must be an array of tables ([[repos]])")
    repos: list[RepoConfig] = []
    seen: set[str] = set()
    for i, raw in enumerate(repos_raw):
        repo = _repo(_table(raw, f"repos[{i}]"), claude_raw, review_raw, i)
        if repo.name.lower() in seen:
            raise ConfigError(f"repo listed twice: {repo.name}")
        seen.add(repo.name.lower())
        repos.append(repo)

    github_app = _github_app(_table(app_raw, "github_app")) if app_raw is not None else None
    return Settings(workdir=workdir, poll_interval=poll_interval, claude=claude, review=review, repos=repos,
                    github_app=github_app)


def _github_app(raw: dict[str, Any]) -> GitHubAppConfig:
    raw = dict(raw)
    app_id = raw.pop("app_id", None)
    if isinstance(app_id, bool) or not (isinstance(app_id, int) or (isinstance(app_id, str) and app_id.isdigit())):
        raise ConfigError("github_app.app_id is required and must be a number")
    key = raw.pop("private_key", None)
    if not isinstance(key, str) or not key:
        raise ConfigError("github_app.private_key is required (path to the app's .pem file)")
    installation_id = raw.pop("installation_id", None)
    if installation_id is not None:
        installation_id = _int(installation_id, "github_app.installation_id", minimum=1)
    api_url = _str(raw.pop("api_url", "https://api.github.com"), "github_app.api_url").rstrip("/")
    if raw:
        raise ConfigError(f"github_app: unknown keys: {', '.join(sorted(raw))}")
    return GitHubAppConfig(str(app_id), Path(key).expanduser(), installation_id, api_url)


def _repo(raw: dict[str, Any], claude_raw: dict[str, Any], review_raw: dict[str, Any], i: int) -> RepoConfig:
    raw = dict(raw)
    where = f"repos[{i}]"
    name = raw.pop("name", None)
    if not isinstance(name, str):
        raise ConfigError(f"{where}.name is required")
    _check_repo_name(name)
    where = f"repos.{name}"

    merged_claude = dict(claude_raw)
    override_claude = _table(raw.pop("claude", {}), f"{where}.claude")
    env = dict(_table(claude_raw.get("env", {}), "claude.env"))
    env.update(_table(override_claude.get("env", {}), f"{where}.claude.env"))
    merged_claude.update(override_claude)
    merged_claude["env"] = env
    merged_review = {**review_raw, **_table(raw.pop("review", {}), f"{where}.review")}

    repo = RepoConfig(
        name=name,
        claude=_claude(merged_claude, f"{where}.claude"),
        review=_review(merged_review, f"{where}.review"),
        skip_drafts=_bool(raw.pop("skip_drafts", True), f"{where}.skip_drafts"),
        labels=_strs(raw.pop("labels", []), f"{where}.labels"),
        reviewers=_strs(raw.pop("reviewers", []), f"{where}.reviewers"),
        ignore_authors=_strs(raw.pop("ignore_authors", []), f"{where}.ignore_authors"),
        base_branches=_strs(raw.pop("base_branches", []), f"{where}.base_branches"),
        label_mode=_str(raw.pop("label_mode", "sticky"), f"{where}.label_mode"),
        commands=_strs(raw.pop("commands", []), f"{where}.commands"),
    )
    if repo.label_mode not in LABEL_MODES:
        raise ConfigError(f"{where}.label_mode must be one of {', '.join(LABEL_MODES)}")
    if any(not c.strip() for c in repo.commands):
        raise ConfigError(f"{where}.commands must not contain empty strings")
    if raw:
        raise ConfigError(f"{where}: unknown keys: {', '.join(sorted(raw))}")
    return repo


def _claude(raw: dict[str, Any], where: str) -> ClaudeConfig:
    raw = dict(raw)
    c = ClaudeConfig()
    c.binary = _str(raw.pop("binary", c.binary), f"{where}.binary")
    c.model = _str(raw.pop("model", c.model), f"{where}.model")
    c.timeout = _int(raw.pop("timeout", c.timeout), f"{where}.timeout", minimum=30)
    c.permission_mode = _str(raw.pop("permission_mode", c.permission_mode), f"{where}.permission_mode")
    c.allowed_tools = _strs(raw.pop("allowed_tools", c.allowed_tools), f"{where}.allowed_tools")
    c.disallowed_tools = _strs(raw.pop("disallowed_tools", c.disallowed_tools), f"{where}.disallowed_tools")
    c.extra_args = _strs(raw.pop("extra_args", c.extra_args), f"{where}.extra_args")
    env = _table(raw.pop("env", {}), f"{where}.env")
    for key, value in env.items():
        if not isinstance(value, str):
            raise ConfigError(f"{where}.env.{key} must be a string")
    c.env = dict(env)
    if raw:
        raise ConfigError(f"{where}: unknown keys: {', '.join(sorted(raw))}")
    return c


def _review(raw: dict[str, Any], where: str) -> ReviewConfig:
    raw = dict(raw)
    r = ReviewConfig()
    r.level = _str(raw.pop("level", r.level), f"{where}.level")
    if r.level not in LEVELS:
        raise ConfigError(f"{where}.level must be one of {', '.join(LEVELS)}")
    max_findings = raw.pop("max_findings", r.max_findings)
    if isinstance(max_findings, bool) or not isinstance(max_findings, (int, str)):
        raise ConfigError(f"{where}.max_findings must be a number, \"all\" or \"\"")
    r.max_findings = str(max_findings)
    if r.max_findings not in ("", "all") and not (r.max_findings.isdigit() and int(r.max_findings) > 0):
        raise ConfigError(f"{where}.max_findings must be a positive number, \"all\" or \"\"")
    r.comment = _bool(raw.pop("comment", r.comment), f"{where}.comment")
    r.max_diff_lines = _int(raw.pop("max_diff_lines", r.max_diff_lines), f"{where}.max_diff_lines", minimum=0)
    r.rereview_on_push = _bool(raw.pop("rereview_on_push", r.rereview_on_push), f"{where}.rereview_on_push")
    r.max_attempts = _int(raw.pop("max_attempts", r.max_attempts), f"{where}.max_attempts", minimum=1)
    r.keep_worktrees = _bool(raw.pop("keep_worktrees", r.keep_worktrees), f"{where}.keep_worktrees")
    r.max_reviews_per_pr = _int(raw.pop("max_reviews_per_pr", r.max_reviews_per_pr), f"{where}.max_reviews_per_pr",
                                minimum=0)
    r.settle_seconds = _int(raw.pop("settle_seconds", r.settle_seconds), f"{where}.settle_seconds", minimum=0)
    r.incremental = _bool(raw.pop("incremental", r.incremental), f"{where}.incremental")
    if raw:
        raise ConfigError(f"{where}: unknown keys: {', '.join(sorted(raw))}")
    return r


def _check_repo_name(name: str) -> None:
    if not _REPO_RE.match(name):
        raise ConfigError(f"repo must look like owner/name, got {name!r}")


def _table(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be a table")
    return value


def _str(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{where} must be a string")
    return value


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where} must be true or false")
    return value


def _int(value: Any, where: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"{where} must be an integer >= {minimum}")
    return value


def _strs(value: Any, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{where} must be a list of strings")
    return list(value)


PLACEHOLDER_REPO = "owner/repo"


def _template() -> str:
    return resources.files("rocket_critic").joinpath("sample_config.toml").read_text()


def _block_span(text: str, name: str) -> tuple[int, int] | None:
    """Span of the ``[[repos]]`` header line through the end of its key lines for repo ``name``."""
    m = re.search(r'^\[\[repos\]\][ \t]*\n(?:[^\n\[].*\n)*?name = "' + re.escape(name) + r'".*\n(?:[^\n\[#].*\n)*',
                  text, re.M)
    return (m.start(), m.end()) if m else None


def repo_block(
    name: str,
    *,
    skip_drafts: bool | None = None,
    labels: list[str] | None = None,
    reviewers: list[str] | None = None,
    ignore_authors: list[str] | None = None,
    base_branches: list[str] | None = None,
    label_mode: str | None = None,
    commands: list[str] | None = None,
) -> str:
    """A commented ``[[repos]]`` entry like the sample config's, with the given settings."""
    _check_repo_name(name)
    text = _template()
    start, end = _block_span(text, PLACEHOLDER_REPO)
    block = text[start:end].replace(f'name = "{PLACEHOLDER_REPO}"', f"name = {_toml_str(name)}")
    if label_mode is not None and label_mode not in LABEL_MODES:
        raise ConfigError(f"label_mode must be one of {', '.join(LABEL_MODES)}")
    values = {"skip_drafts": skip_drafts, "labels": labels, "reviewers": reviewers,
              "ignore_authors": ignore_authors, "base_branches": base_branches,
              "label_mode": label_mode, "commands": commands}
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, str):
            rendered = _toml_str(value)
        else:
            rendered = "[" + ", ".join(_toml_str(v) for v in value) + "]"
        line = re.search(rf"^{key} = [^#\n]*?(\s*#.*)?$", block, re.M)
        comment = line.group(1) or ""
        # Keep the trailing comment aligned when the value fits in the old column.
        pad = max(1, len(line.group(0)) - len(comment.lstrip()) - len(f"{key} = {rendered}")) if comment else 0
        block = block[:line.start()] + f"{key} = {rendered}" + (" " * pad + comment.lstrip() if comment else "") + \
            block[line.end():]
    return block


def sample_config(workdir: str = DEFAULT_WORKDIR, repos: list[str] | None = None) -> str:
    """The annotated sample config, with ``workdir`` and (optionally) the ``[[repos]]`` entries filled in."""
    text = _template()
    text = text.replace(f'workdir = "{DEFAULT_WORKDIR}"', f"workdir = {_toml_str(workdir)}", 1)
    if repos:
        start, end = _block_span(text, PLACEHOLDER_REPO)
        entries = [repo_block(name) for name in repos]
        text = text[:start] + "\n".join(entries) + text[end:]
    return text


def add_repo(path: Path, block: str) -> bool:
    """Add a ``[[repos]]`` entry to the config file at ``path``, keeping its comments and layout.

    The sample's ``owner/repo`` placeholder entry is replaced; otherwise the entry is
    appended. The result is validated before the file is (atomically) rewritten.
    Returns True if the placeholder was replaced.
    """
    text = path.read_text()
    current = parse(tomllib.loads(text))
    new = parse(tomllib.loads(block)).repos[0].name
    if any(r.name.lower() == new.lower() for r in current.repos):
        raise ConfigError(f"{new} is already in {path}")
    span = _block_span(text, PLACEHOLDER_REPO)
    if span:
        text = text[:span[0]] + block + text[span[1]:]
    else:
        text = text.rstrip("\n") + "\n\n" + block
    try:
        parse(tomllib.loads(text))
    except (ConfigError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"adding {new} would make {path} invalid: {exc}") from None
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)
    return span is not None


def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
