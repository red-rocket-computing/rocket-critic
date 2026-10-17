"""Command line interface."""

from __future__ import annotations

import copy
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

import click

from . import __version__, daemon, filters, proc, service, updater
from .config import (
    CONFIG_NAME,
    DEFAULT_CONFIG_PATH,
    DEFAULT_WORKDIR,
    LEVELS,
    MIN_POLL_INTERVAL,
    PLACEHOLDER_REPO,
    ClaudeConfig,
    ConfigError,
    Settings,
    add_repo,
    config_path,
    load,
    repo_block,
    sample_config,
)
from .ghapp import GitHubApp, GitHubAppError
from .github import GitHub
from .daemon import ConfigWatcher
from .poller import AlreadyRunning, Critic, lock, running_pid
from .runner import build_command, build_env, parse_envelope
from .state import DONE, State
from .workspace import Workspace

log = logging.getLogger("rocket_critic")

_PR_RE = re.compile(
    r"^(?:https?://github\.com/)?(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:#|/pull/)(?P<num>\d+)/?$"
)


def parse_pr_ref(ref: str) -> tuple[str, int]:
    m = _PR_RE.match(ref.strip())
    if not m:
        raise click.BadParameter(f"expected owner/repo#123 or a pull request URL, got {ref!r}")
    return m.group("repo"), int(m.group("num"))


class Ctx:
    def __init__(self, config_path: Path | None, workdir: Path | None = None, verbose: bool = False):
        self.config_path = config_path
        self.workdir = workdir
        self.verbose = verbose
        self._settings: Settings | None = None

    @property
    def settings(self) -> Settings:
        if self._settings is None:
            try:
                self._settings = load(self.config_path, self.workdir)
            except ConfigError as exc:
                raise click.ClickException(str(exc)) from None
        return self._settings


pass_ctx = click.make_pass_decorator(Ctx)


@click.group()
@click.option("-c", "--config", "config_path", type=click.Path(path_type=Path), envvar="ROCKET_CRITIC_CONFIG",
              help=f"Config file. Default: the first of WORKDIR/{CONFIG_NAME} (with -w), ./{CONFIG_NAME} and "
                   f"{DEFAULT_CONFIG_PATH}. Env: ROCKET_CRITIC_CONFIG.")
@click.option("-w", "--workdir", type=click.Path(file_okay=False, path_type=Path), envvar="ROCKET_CRITIC_WORKDIR",
              help="Working directory holding the config file and the repo/PR checkouts "
                   "(overrides the config's workdir). Env: ROCKET_CRITIC_WORKDIR.")
@click.option("-v", "--verbose", is_flag=True, help="Debug logging.")
@click.version_option(__version__)
@click.pass_context
def cli(ctx: click.Context, config_path: Path | None, workdir: Path | None, verbose: bool) -> None:
    """Watch GitHub repos and review pull requests with Claude Code's /code-review."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    ctx.obj = Ctx(config_path, workdir, verbose)


@cli.command()
@click.option("--repo", "repos", multiple=True, metavar="OWNER/REPO",
              help="Repo to watch (repeatable); fills in [[repos]] and creates its directories.")
@click.option("--force", is_flag=True, help="Overwrite an existing config file.")
@pass_ctx
def init(ctx: Ctx, repos: tuple[str, ...], force: bool) -> None:
    """Write a sample config and create the working directory.

    With -w DIR the config goes to DIR/config.toml (with workdir = ".") next
    to DIR/repos and DIR/prs, so later commands only need -w DIR.
    """
    path = config_path(ctx.config_path, ctx.workdir)
    if ctx.workdir:
        workdir = ctx.workdir.expanduser().resolve()
        in_workdir = path.parent.expanduser().resolve() == workdir
        workdir_value = "." if in_workdir else str(workdir)
    else:
        workdir_value = DEFAULT_WORKDIR
    try:
        text = sample_config(workdir_value, list(repos))
    except ConfigError as exc:
        raise click.BadParameter(str(exc), param_hint="--repo") from None
    if path.exists() and not force:
        raise click.ClickException(f"{path} already exists (use --force to overwrite)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)

    settings = ctx.settings
    created = Workspace(settings.workdir).ensure_layout([r.name for r in settings.repos if repos])
    click.echo(f"wrote {path}")
    click.echo(f"workdir {settings.workdir}" + (f" (created {len(created)} directories)" if created else ""))
    hint = f" -w {ctx.workdir}" if ctx.workdir and not ctx.config_path else ""
    todo = "[claude]" if repos else "[[repos]] and [claude]"
    click.echo(f"edit {todo} then run `rocket-critic{hint} doctor`")


@cli.command("add-repo")
@click.argument("name", metavar="OWNER/REPO")
@click.option("--label", "labels", multiple=True, help="Review PRs with this label (repeatable).")
@click.option("--reviewer", "reviewers", multiple=True,
              help="Review PRs requesting review from this user or org/team (repeatable).")
@click.option("--base", "base_branches", multiple=True, help="Only PRs targeting this branch (repeatable).")
@click.option("--ignore-author", "ignore_authors", multiple=True,
              help="Skip PRs by this author (repeatable; replaces the default bot list).")
@click.option("--drafts/--no-drafts", default=None, help="Also review draft PRs (default: skip them).")
@click.option("--label-mode", type=click.Choice(["sticky", "request"]),
              help="sticky: review every push while labelled (default); request: a label asks for one "
                   "review and is removed afterwards.")
@click.option("--command", "commands", multiple=True,
              help="PR comment line that requests a review, e.g. /review (repeatable).")
@click.option("--no-check", is_flag=True, help="Don't check that the repo is reachable with gh.")
@pass_ctx
def add_repo_cmd(ctx: Ctx, name: str, labels: tuple[str, ...], reviewers: tuple[str, ...],
                 base_branches: tuple[str, ...], ignore_authors: tuple[str, ...], drafts: bool | None,
                 label_mode: str | None, commands: tuple[str, ...], no_check: bool) -> None:
    """Add a repo to watch to the config file.

    The entry is appended (or replaces the sample's owner/repo placeholder),
    keeping the file's comments. Edit the file for per-repo [claude]/[review]
    overrides.
    """
    s = ctx.settings
    try:
        block = repo_block(
            name,
            skip_drafts=None if drafts is None else not drafts,
            labels=list(labels) or None,
            reviewers=list(reviewers) or None,
            ignore_authors=list(ignore_authors) or None,
            base_branches=list(base_branches) or None,
            label_mode=label_mode,
            commands=list(commands) or None,
        )
    except ConfigError as exc:
        raise click.BadParameter(str(exc), param_hint="OWNER/REPO") from None
    if any(r.name.lower() == name.lower() for r in s.repos):
        raise click.ClickException(f"{name} is already in {s.path}")
    if not no_check:
        try:
            prs = GitHub().list_open_prs(name)
        except proc.CommandError as exc:
            raise click.ClickException(f"cannot list PRs in {name}: {exc} (use --no-check to add it anyway)")
        click.echo(f"{name}: reachable, {len(prs)} open PR(s)")
    try:
        replaced = add_repo(s.path, block)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from None
    created = Workspace(s.workdir).ensure_layout([name])
    what = "replaced the owner/repo placeholder with" if replaced else "added"
    click.echo(f"{what} {name} in {s.path}" + (f"; created {len(created)} directories" if created else ""))


_TOKEN_KINDS = {
    "github_pat_": "fine-grained token",
    "ghp_": "classic token",
    "gho_": "OAuth token",
    "ghu_": "GitHub App user token",
    "ghs_": "GitHub App installation token",
}


def describe_token(claude: ClaudeConfig, app: GitHubApp | None = None) -> tuple[bool, str]:
    """Where the claude subprocess's GitHub token comes from, never showing the token itself.

    Returns ``(False, why)`` when [claude.env] sets one that does not resolve and no
    GitHub App takes its place.
    """
    explicit = claude.resolved_env()
    if app is not None and "GH_TOKEN" not in explicit and "GITHUB_TOKEN" not in explicit:
        return True, f"GitHub App {app.cfg.app_id} (a new token, limited to the repo, for each review)"
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        missing = claude.unresolved_env().get(key)
        if missing:
            names = ", ".join(f"${name}" for name in missing)
            return False, (f"[claude.env] {key} uses {names}, which is not set (or empty) in this shell, "
                           "so your gh login is used instead; export it before running rocket-critic")
    env = claude.resolved_env()
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        token = env.get(key) if key in claude.env else None
        source = "[claude.env]"
        if token is None and os.environ.get(key):
            token, source = os.environ[key], "the environment"
        if token:
            kind = next((v for k, v in _TOKEN_KINDS.items() if token.startswith(k)), "token")
            return True, f"{key} from {source} ({kind})"
    return True, "none set; /code-review uses your gh login"


def warn_unresolved_env(settings: Settings) -> None:
    """Log [claude.env] entries dropped because a ${VAR} they use is unset.

    A missing token is expected when a GitHub App provides it.
    """
    seen = set()
    for repo in settings.repos or [settings.repo("example/example")]:
        for key, names in repo.claude.unresolved_env().items():
            if settings.github_app and key in ("GH_TOKEN", "GITHUB_TOKEN"):
                continue
            if (key, tuple(names)) not in seen:
                seen.add((key, tuple(names)))
                log.warning("[claude.env] %s skipped: %s not set or empty", key,
                            ", ".join(f"${n}" for n in names))


@cli.command()
@pass_ctx
def doctor(ctx: Ctx) -> None:
    """Check gh, git, claude and the configured model backend."""
    s = ctx.settings
    ok = True

    def report(good: bool, label: str, detail: str = "") -> None:
        nonlocal ok
        ok &= good
        mark = click.style("ok  ", fg="green") if good else click.style("FAIL", fg="red")
        click.echo(f"[{mark}] {label}" + (f": {detail}" if detail else ""))

    click.echo(f"config: {s.path.resolve()}\nworkdir: {s.workdir}\nrepos: {', '.join(r.name for r in s.repos) or '(none)'}")
    for tool in ("git", "gh"):
        report(shutil.which(tool) is not None, f"{tool} on PATH", shutil.which(tool) or "not found")

    gh = GitHub()
    gh_login = gh.whoami()
    authed, status = gh.auth_status()
    report(authed, "gh auth status", " ".join(status.split())[:160])

    real_repos = [r for r in s.repos if r.name != PLACEHOLDER_REPO]
    if len(real_repos) < len(s.repos):
        report(False, f"repo {PLACEHOLDER_REPO}",
               "this is the sample placeholder; replace it with `rocket-critic add-repo OWNER/REPO`")
    for repo in real_repos:
        try:
            prs = gh.list_open_prs(repo.name)
            report(True, f"list PRs in {repo.name}", f"{len(prs)} open")
        except proc.CommandError as exc:
            report(False, f"list PRs in {repo.name}", str(exc))

    app = GitHubApp(s.github_app) if s.github_app else None
    app_login = None
    if app:
        report(shutil.which("openssl") is not None, "openssl on PATH (signs GitHub App tokens)",
               shutil.which("openssl") or "not found")
        try:
            app_login = app.login
            report(True, f"GitHub App {s.github_app.app_id}", f"{app_login}, key {s.github_app.private_key}")
        except GitHubAppError as exc:
            report(False, f"GitHub App {s.github_app.app_id}", str(exc))
        if app_login:
            for repo in real_repos:
                try:
                    installation = app.installation_id(repo.name)
                    app.token(repo.name)
                    report(True, f"GitHub App on {repo.name}", f"installation {installation}, token issued")
                except GitHubAppError as exc:
                    report(False, f"GitHub App on {repo.name}", str(exc))

    # One backend check per distinct claude configuration.
    seen: set[str] = set()
    for repo in s.repos or [s.repo("example/example")]:
        c = repo.claude
        env = build_env(c, repo.name)
        key = repr((c.binary, c.model, sorted(c.resolved_env().items())))
        if key in seen:
            continue
        seen.add(key)
        binary = shutil.which(c.binary)
        if not binary:
            report(False, f"claude binary {c.binary!r}", "not found")
            continue
        version = proc.run([c.binary, "--version"], env=env, timeout=60, check=False).stdout.strip()
        report(True, "claude", f"{binary} {version}")

        base_url = env.get("ANTHROPIC_BASE_URL", "Anthropic default")
        probe = copy.deepcopy(c)
        probe.allowed_tools, probe.disallowed_tools = [], []
        cmd = build_command("Reply with exactly the word OK and nothing else.", probe)
        label = f"model {c.model or 'default'} via {base_url}"
        try:
            res = proc.run(cmd, env=env, timeout=180, check=False)
            result = parse_envelope(res.stdout)
            report(not result.is_error and "OK" in result.text.upper(), label, result.text.strip()[:80])
        except Exception as exc:  # noqa: BLE001 - doctor reports everything
            report(False, label, str(exc))

        token_ok, token_detail = describe_token(c, app)
        report(token_ok, "GitHub token for /code-review", token_detail)
        if token_detail.startswith("GitHub App"):
            report(app_login is not None, "/code-review will post as", app_login or "unknown (GitHub App check failed)")
            continue
        login = gh.whoami(env=env)
        detail = login or "unknown (gh api user failed)"
        if login and gh_login and login.lower() == gh_login.lower() and token_ok and not token_detail.startswith("none"):
            detail += " (the token belongs to your own account; post as another name with a separate account's token)"
        report(login is not None, "/code-review will post as", detail)

    if not ok:
        sys.exit(1)


class Duration(click.ParamType):
    """Seconds, or a number with an s/m/h suffix (e.g. 90, 30s, 5m, 1h)."""

    name = "DURATION"
    _units = {"s": 1, "m": 60, "h": 3600}

    def convert(self, value, param, ctx):
        if isinstance(value, int):
            return value
        m = re.fullmatch(r"\s*(\d+)\s*([smh]?)\s*", str(value).lower())
        if not m:
            self.fail(f"{value!r} is not a duration like 90, 30s, 5m or 1h", param, ctx)
        seconds = int(m.group(1)) * self._units[m.group(2) or "s"]
        if seconds < MIN_POLL_INTERVAL:
            self.fail(f"must be at least {MIN_POLL_INTERVAL}s", param, ctx)
        return seconds


@cli.command()
@click.option("--once", is_flag=True, help="Poll a single time and exit.")
@click.option("--repo", "only", multiple=True, help="Only watch this owner/repo (repeatable).")
@click.option("-i", "--interval", type=Duration(), envvar="ROCKET_CRITIC_POLL_INTERVAL",
              help="Time between polls, e.g. 90, 30s, 5m, 1h (default: poll_interval from the config). "
                   "Env: ROCKET_CRITIC_POLL_INTERVAL.")
@pass_ctx
def watch(ctx: Ctx, once: bool, only: tuple[str, ...], interval: int | None) -> None:
    """Poll repos and review new or updated pull requests."""
    s = ctx.settings
    config = ConfigWatcher(s, workdir_override=ctx.workdir, interval=interval, only=only)
    config.apply_overrides(s)  # --interval is also the base of the retry backoff
    repos = config.repos(s)
    if not repos:
        raise click.ClickException("no repos configured")
    try:
        with lock(s.workdir):
            warn_unresolved_env(s)
            critic = Critic(s)
            critic.install_signal_handlers()
            log.info("watching %s every %s (pid %d; config %s is reloaded when it changes)",
                     ", ".join(r.name for r in repos), format_duration(s.poll_interval), os.getpid(),
                     config.path)
            critic.watch(repos, once=once, reload=config.check)
    except AlreadyRunning as exc:
        raise click.ClickException(str(exc)) from None


@cli.command()
@click.option("--repo", "only", multiple=True, help="Only watch this owner/repo (repeatable).")
@click.option("-i", "--interval", type=Duration(), envvar="ROCKET_CRITIC_POLL_INTERVAL",
              help="Time between polls (see `watch --interval`).")
@click.option("--wait", default=10.0, show_default=True, help="Seconds to wait for the watcher to start.")
@pass_ctx
def start(ctx: Ctx, only: tuple[str, ...], interval: int | None, wait: float) -> None:
    """Run `watch` in the background (daemon mode).

    Output goes to WORKDIR/rocket-critic.log. The config file is reloaded when it
    changes; use `reload` to force it, and `stop` to end the watcher.
    """
    s = ctx.settings
    pid = running_pid(s.workdir)
    if pid:
        raise click.ClickException(f"already running (pid {pid}) for {s.workdir}")
    if not only and not s.repos:
        raise click.ClickException("no repos configured")
    argv = ["-c", str(Path(s.path).resolve())]
    if ctx.workdir:
        argv += ["-w", str(s.workdir)]
    if ctx.verbose:
        argv.append("-v")
    argv.append("watch")
    for name in only:
        argv += ["--repo", name]
    if interval is not None:
        argv += ["--interval", str(interval)]
    try:
        pid = daemon.start(argv, s.workdir, wait=wait)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(f"started rocket-critic (pid {pid}); log: {s.workdir / daemon.LOG_NAME}")


@cli.command()
@click.option("--now", is_flag=True, help="Abort a review in progress instead of letting it finish.")
@click.option("--timeout", type=float, help="Give up waiting after this many seconds.")
@pass_ctx
def stop(ctx: Ctx, now: bool, timeout: float | None) -> None:
    """Stop the background watcher (after the current review, unless --now)."""
    workdir = ctx.settings.workdir
    pid = running_pid(workdir)
    if not pid:
        click.echo(f"not running for {workdir}")
        return
    click.echo(f"stopping rocket-critic (pid {pid})" + ("" if now else "; a review in progress finishes first"))
    try:
        daemon.stop(workdir, now=now, timeout=timeout)
    except TimeoutError as exc:
        raise click.ClickException(f"{exc}; run `stop --now` to abort it") from None
    click.echo("stopped")


@cli.command()
@pass_ctx
def reload(ctx: Ctx) -> None:
    """Make the running watcher re-read the config now and poll."""
    workdir = ctx.settings.workdir
    pid = daemon.reload(workdir)
    if not pid:
        raise click.ClickException(f"not running for {workdir}")
    click.echo(f"asked rocket-critic (pid {pid}) to reload {ctx.settings.path}; see {workdir / daemon.LOG_NAME}")


@cli.command()
@click.option("--check", is_flag=True, help="Only report whether a newer release exists.")
@click.option("--version", "wanted", metavar="X.Y.Z", help="Install this release instead of the latest (may be older).")
@click.option("--force", is_flag=True, help="Reinstall even if this version is already installed.")
@click.option("--restart", is_flag=True, help="Restart the background watcher afterwards, so it runs the new version.")
@click.option("--repo", default=updater.RELEASE_REPO, show_default=True, envvar="ROCKET_CRITIC_RELEASE_REPO",
              help="Repository to take releases from. Env: ROCKET_CRITIC_RELEASE_REPO.")
@pass_ctx
def update(ctx: Ctx, check: bool, wanted: str | None, force: bool, restart: bool, repo: str) -> None:
    """Update rocket-critic to the latest GitHub release.

    Releases are found and downloaded with your gh login, so this works for a
    private repository. The wheel is checked against the release's SHA256SUMS
    before it is installed into the environment rocket-critic runs from (with
    pipx for a pipx install).
    """
    try:
        release = updater.find_release(repo, wanted)
    except updater.UpdateError as exc:
        raise click.ClickException(str(exc)) from None
    current = __version__
    newer = updater.version_key(release.version) > updater.version_key(current)
    same = updater.version_key(release.version) == updater.version_key(current)
    if check:
        if newer:
            click.echo(f"rocket-critic {release.version} is available (installed: {current}); "
                       "run `rocket-critic update`")
        elif same:
            click.echo(f"rocket-critic {current} is up to date")
        else:
            click.echo(f"rocket-critic {current} is newer than release {release.version}")
        return
    if same and not force:
        click.echo(f"rocket-critic {current} is up to date")
        return
    if not newer and not same and not wanted and not force:
        click.echo(f"rocket-critic {current} is newer than the latest release ({release.version}); nothing to do")
        return
    source = updater.installed_from_source()
    if source:
        raise click.ClickException(f"rocket-critic is installed from a source checkout ({source}); "
                                   "update it there with `git pull`")
    click.echo(f"installing rocket-critic {release.version} (installed: {current}) from {release.tag}")
    try:
        with tempfile.TemporaryDirectory(prefix="rocket-critic-update-") as tmp:
            wheel = updater.download(release, repo, Path(tmp))
            updater.install(wheel, reinstall=same)
    except updater.UpdateError as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(f"updated rocket-critic to {release.version}")
    restart_watcher(ctx, restart, current)


def restart_watcher(ctx: Ctx, restart: bool, old_version: str) -> None:
    """After an update: restart the background watcher (``restart``), or say how to."""
    try:
        workdir = ctx.settings.workdir
    except click.ClickException:
        if restart:
            raise
        return  # no config: there is no watcher to find
    pid = running_pid(workdir)
    if not pid or pid < 0:
        if restart:
            click.echo(f"no background watcher is running for {workdir}")
        return
    argv = updater.started_by_start(pid)
    unit = service.unit_for_pid(pid) if argv is None else None
    if unit:
        if not restart:
            click.echo(f"the {unit} service (pid {pid}) still runs {old_version}; restart it with "
                       f"`rocket-critic update --restart` or `systemctl --user restart {unit}`")
            return
        click.echo(f"restarting the {unit} service (pid {pid}); a review in progress finishes first")
        try:
            service.systemctl("restart", f"{unit}.service")
        except service.ServiceError as exc:
            raise click.ClickException(str(exc)) from None
        click.echo(f"restarted the {unit} service (pid {service.main_pid(unit) or '?'})")
        return
    if argv is None:
        how = ("restart it the way it was started: `systemctl --user restart rocket-critic` for a systemd "
               "service, Ctrl-C and `rocket-critic watch` in a terminal, or `rocket-critic stop && rocket-critic "
               "start` for a watcher an earlier version started in the background")
        if restart:
            raise click.ClickException(f"the watcher (pid {pid}) wasn't started by this version's "
                                       f"`rocket-critic start`, so it isn't restarted here; {how}")
        click.echo(f"the watcher (pid {pid}) still runs {old_version}; {how}")
        return
    if not restart:
        click.echo(f"the background watcher (pid {pid}) still runs {old_version}; restart it with "
                   "`rocket-critic update --restart` or `rocket-critic stop && rocket-critic start`")
        return
    click.echo(f"restarting the background watcher (pid {pid}); a review in progress finishes first")
    daemon.stop(workdir)
    try:
        pid = daemon.start(argv, workdir)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(f"started rocket-critic (pid {pid}); log: {workdir / daemon.LOG_NAME}")


@cli.group("service")
def service_cmd() -> None:
    """Run the watcher as a systemd user service (install, remove, status)."""


def _service_name(name: str) -> str:
    try:
        return service.check_name(name)
    except service.ServiceError as exc:
        raise click.ClickException(str(exc)) from None


@service_cmd.command("install")
@click.option("--name", default=service.DEFAULT_NAME, show_default=True, help="Unit name (one per workdir).")
@click.option("--repo", "only", multiple=True, help="Only watch this owner/repo (repeatable).")
@click.option("-i", "--interval", type=Duration(), help="Time between polls (see `watch --interval`).")
@click.option("--start/--no-start", default=True, show_default=True, help="Start (or restart) it now.")
@click.option("--linger", is_flag=True,
              help="Also run `loginctl enable-linger`, so it starts at boot and keeps running after logout.")
@click.option("--force", is_flag=True, help="Overwrite a unit file that `service install` didn't write.")
@pass_ctx
def service_install(ctx: Ctx, name: str, only: tuple[str, ...], interval: int | None, start: bool, linger: bool,
                    force: bool) -> None:
    """Install (or update) a systemd user service that runs `watch`.

    Writes ~/.config/systemd/user/NAME.service for the current config and workdir, with
    the rocket-critic of this environment and your current PATH, then enables it. A
    watcher started with `rocket-critic start` for the same workdir is stopped first,
    so the service can take over. Run it again after moving the config or workdir.
    """
    name = _service_name(name)
    s = ctx.settings
    if not only and not s.repos:
        raise click.ClickException("no repos configured")
    path = service.unit_path(name)
    if path.exists() and not service.ours(path) and not force:
        raise click.ClickException(f"{path} exists and wasn't written by `service install`; use --force to "
                                   "replace it")
    argv = service.executable() + ["-c", str(Path(s.path).resolve())]
    if ctx.workdir:
        argv += ["-w", str(s.workdir)]
    argv.append("watch")
    for repo in only:
        argv += ["--repo", repo]
    if interval is not None:
        argv += ["--interval", str(interval)]

    try:
        pid = running_pid(s.workdir)
        if pid and pid > 0 and service.main_pid(name) != pid:
            if updater.started_by_start(pid) is None:
                raise click.ClickException(f"a watcher (pid {pid}) is already running for {s.workdir}, not "
                                           "started by `rocket-critic start`; stop it first")
            click.echo(f"stopping the background watcher (pid {pid}) so the service can take over; a review in "
                       "progress finishes first")
            daemon.stop(s.workdir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(service.render(argv, s.workdir, os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")))
        click.echo(f"wrote {path}")
        service.systemctl("daemon-reload")
        service.systemctl("enable", f"{name}.service")
        if start:
            service.systemctl("restart", f"{name}.service")
            click.echo(f"started {name} (pid {service.main_pid(name) or '?'})")
        else:
            click.echo(f"enabled {name}; start it with `systemctl --user start {name}`")
        if linger:
            service.enable_linger()
            click.echo("enabled lingering: the service starts at boot and keeps running after logout")
        elif service.linger() is False:
            click.echo("note: the service stops when you log out; `rocket-critic service install --linger` (or "
                       "`loginctl enable-linger`) keeps it running and starts it at boot")
    except service.ServiceError as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(f"logs: `journalctl --user -u {name} -f`; status: `rocket-critic service status`")


@service_cmd.command("remove")
@click.option("--name", default=service.DEFAULT_NAME, show_default=True, help="Unit name.")
@click.option("--force", is_flag=True, help="Remove a unit file that `service install` didn't write.")
def service_remove(name: str, force: bool) -> None:
    """Stop and disable the service and delete its unit file.

    A review in progress finishes first (up to the unit's 50-minute stop timeout).
    """
    name = _service_name(name)
    path = service.unit_path(name)
    if not path.exists():
        click.echo(f"no {path}; nothing to remove")
        return
    if not service.ours(path) and not force:
        raise click.ClickException(f"{path} wasn't written by `service install`; use --force to remove it")
    try:
        click.echo(f"stopping {name}; a review in progress finishes first")
        service.systemctl("disable", "--now", f"{name}.service")
        path.unlink()
        service.systemctl("daemon-reload")
        service.systemctl("reset-failed", f"{name}.service", check=False)
    except service.ServiceError as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(f"removed {path}")


@service_cmd.command("status")
@click.option("--name", default=service.DEFAULT_NAME, show_default=True, help="Unit name.")
def service_status(name: str) -> None:
    """Whether the service is installed, enabled and running."""
    name = _service_name(name)
    path = service.unit_path(name)
    if not path.exists():
        click.echo(f"{name}: not installed ({path} does not exist)")
        return
    try:
        enabled = service.systemctl("is-enabled", f"{name}.service", check=False) or "unknown"
        active = service.systemctl("is-active", f"{name}.service", check=False) or "unknown"
    except service.ServiceError as exc:
        raise click.ClickException(str(exc)) from None
    pid = service.main_pid(name)
    lingering = service.linger()
    click.echo(f"{name}: {active}" + (f" (pid {pid})" if pid else "") + f", {enabled}")
    click.echo(f"unit: {path}" + ("" if service.ours(path) else " (not written by `service install`)"))
    if lingering is not None:
        click.echo("linger: " + ("yes (starts at boot, runs without a login)" if lingering else
                                 "no (stops when you log out; see `service install --linger`)"))
    click.echo(f"logs: journalctl --user -u {name} -f")


@cli.command()
@click.argument("pr_ref", metavar="OWNER/REPO#N")
@click.option("--level", type=click.Choice(LEVELS), help="Override the /code-review level.")
@click.option("--model", help="Override the model.")
@click.option("--comment/--no-comment", default=None,
              help="Post inline comments (default from config); --no-comment just prints findings.")
@click.option("--force", is_flag=True,
              help="Ignore filters, review history and the per-PR review limit.")
@click.option("--full", is_flag=True, help="Review the whole PR, without earlier-review context.")
@pass_ctx
def review(ctx: Ctx, pr_ref: str, level: str | None, model: str | None, comment: bool | None, force: bool,
           full: bool) -> None:
    """Review a single pull request now."""
    s = ctx.settings
    name, number = parse_pr_ref(pr_ref)
    repo = copy.deepcopy(s.repo(name))
    if level:
        repo.review.level = level
    if model:
        repo.claude.model = model
    warn_unresolved_env(s)
    critic = Critic(s)
    try:
        pr = critic.gh.get_pr(repo.name, number)
    except proc.CommandError as exc:
        raise click.ClickException(str(exc)) from None
    if not force:
        repo.review.settle_seconds = 0  # an explicit request needs no settle delay
        ok, why = filters.should_review(pr, repo, critic.state, retry_base=0)
        if ok and why.startswith(filters.RETRY) and not critic.retry_is_safe(pr, repo):
            ok, why = False, "the failed attempt may have posted comments"
        if not ok:
            raise click.ClickException(f"{pr.ref}: not reviewing ({why}); use --force to override")
    outcome = critic.review(pr, repo, comment=comment, incremental=False if full else None)
    if outcome.output_path:
        click.echo(outcome.output_path.read_text())
        click.echo(f"saved to {outcome.output_path}", err=True)
    if outcome.status != DONE:
        raise click.ClickException(f"{pr.ref}: {outcome.status}: {outcome.detail}")


@cli.command()
@click.option("--repo", help="Only this owner/repo.")
@click.option("-n", "--limit", default=30, show_default=True)
@pass_ctx
def status(ctx: Ctx, repo: str | None, limit: int) -> None:
    """Show whether the watcher runs, and recent reviews."""
    workdir = ctx.settings.workdir
    pid = running_pid(workdir)
    if pid:
        log_path = workdir / daemon.LOG_NAME
        click.echo(f"watcher: running (pid {pid})" + (f"; log {log_path}" if log_path.exists() else ""))
    else:
        click.echo("watcher: not running")
    state = State(workdir / "state.db")
    rows = state.recent(repo, limit)
    if not rows:
        click.echo("no reviews yet")
        return
    click.echo(f"{'WHEN':16}  {'PR':32}  {'SHA':7}  {'STATUS':7}  {'TRY':3}  {'TIME':>6}  DETAIL")
    for r in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.updated_at))
        secs = f"{r.duration_ms / 1000:.0f}s" if r.duration_ms else ""
        detail = r.error or r.output_path or ""
        click.echo(f"{when:16}  {r.repo + '#' + str(r.pr):32}  {r.head_sha[:7]:7}  {r.status:7}  "
                   f"{r.attempts:>3}  {secs:>6}  {detail}")


@cli.command()
@pass_ctx
def clean(ctx: Ctx) -> None:
    """Remove leftover PR worktrees."""
    s = ctx.settings
    try:
        with lock(s.workdir):
            removed = Workspace(s.workdir).clean()
    except AlreadyRunning as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(f"removed {len(removed)} worktree(s)")


def format_duration(seconds: int) -> str:
    for unit, size in (("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def main() -> None:
    cli()
