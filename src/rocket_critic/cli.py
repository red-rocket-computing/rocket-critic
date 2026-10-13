"""Command line interface."""

from __future__ import annotations

import copy
import logging
import re
import shutil
import sys
import time
from pathlib import Path

import click

from . import __version__, filters, proc
from .config import (
    CONFIG_NAME,
    DEFAULT_CONFIG_PATH,
    DEFAULT_WORKDIR,
    LEVELS,
    ConfigError,
    Settings,
    config_path,
    load,
    sample_config,
)
from .github import GitHub
from .poller import AlreadyRunning, Critic, lock
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
    def __init__(self, config_path: Path | None, workdir: Path | None = None):
        self.config_path = config_path
        self.workdir = workdir
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
@click.option("-c", "--config", "config_path", type=click.Path(path_type=Path),
              help=f"Config file (default WORKDIR/{CONFIG_NAME} with -w, else {DEFAULT_CONFIG_PATH}).")
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
    ctx.obj = Ctx(config_path, workdir)


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

    click.echo(f"config: {s.path}\nworkdir: {s.workdir}\nrepos: {', '.join(r.name for r in s.repos) or '(none)'}")
    for tool in ("git", "gh"):
        report(shutil.which(tool) is not None, f"{tool} on PATH", shutil.which(tool) or "not found")

    gh = GitHub()
    authed, status = gh.auth_status()
    report(authed, "gh auth status", " ".join(status.split())[:160])

    for repo in s.repos:
        try:
            prs = gh.list_open_prs(repo.name)
            report(True, f"list PRs in {repo.name}", f"{len(prs)} open")
        except proc.CommandError as exc:
            report(False, f"list PRs in {repo.name}", str(exc))

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

        login = gh.whoami(env=env)
        report(login is not None, "/code-review will post as", login or "unknown (gh api user failed)")

    if not ok:
        sys.exit(1)


@cli.command()
@click.option("--once", is_flag=True, help="Poll a single time and exit.")
@click.option("--repo", "only", multiple=True, help="Only watch this owner/repo (repeatable).")
@pass_ctx
def watch(ctx: Ctx, once: bool, only: tuple[str, ...]) -> None:
    """Poll repos and review new or updated pull requests."""
    s = ctx.settings
    repos = [s.repo(name) for name in only] if only else s.repos
    if not repos:
        raise click.ClickException("no repos configured")
    try:
        with lock(s.workdir):
            critic = Critic(s)
            critic.install_signal_handlers()
            log.info("watching %s every %ss", ", ".join(r.name for r in repos), s.poll_interval)
            critic.watch(repos, once=once)
    except AlreadyRunning as exc:
        raise click.ClickException(str(exc)) from None


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
    """Show recent reviews."""
    state = State(ctx.settings.workdir / "state.db")
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


def main() -> None:
    cli()
