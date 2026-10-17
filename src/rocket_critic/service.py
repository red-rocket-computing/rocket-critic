"""A systemd user service that runs `watch` (`rocket-critic service install/remove/status`)."""

from __future__ import annotations

import getpass
import os
import re
import shutil
import sys
from pathlib import Path

from . import proc
from .config import user_config_path

DEFAULT_NAME = "rocket-critic"
# First line of every unit `service install` writes; `remove` only deletes such files.
MARKER = "# Written by `rocket-critic service install`; `rocket-critic service remove` deletes it."
_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")


class ServiceError(RuntimeError):
    pass


def check_name(name: str) -> str:
    name = name.removesuffix(".service")
    if not _NAME_RE.match(name):
        raise ServiceError(f"invalid service name {name!r}")
    return name


def unit_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"


def unit_path(name: str) -> Path:
    return unit_dir() / f"{check_name(name)}.service"


def env_file() -> Path:
    """The optional environment file the unit reads (tokens, poll period)."""
    return user_config_path().parent / "rocket-critic.env"


def ours(path: Path) -> bool:
    try:
        return path.read_text().startswith(MARKER)
    except OSError:
        return False


def executable() -> list[str]:
    """How the service runs rocket-critic: the script next to this interpreter (a venv or pipx
    environment keeps its path across `update`), else the one on PATH, else ``python -m``."""
    sibling = Path(sys.executable).parent / "rocket-critic"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return [str(sibling)]
    found = shutil.which("rocket-critic")
    if found:
        return [str(Path(found).absolute())]
    return [sys.executable, "-m", "rocket_critic"]


def quote(arg: str, command: bool = True) -> str:
    """``arg`` for ExecStart (``command``) or Environment=: quoted if needed, specifiers (``%``)
    escaped, and in a command line variables (``$``) too; Environment= doesn't expand ``$``."""
    arg = arg.replace("%", "%%")
    if command:
        arg = arg.replace("$", "$$")
    if arg and not re.search(r'[\s"\'\\;]', arg):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _path(path: Path) -> str:
    """A path setting (taken literally to the end of the line, so not quoted)."""
    return str(path).replace("%", "%%")


def render(argv: list[str], workdir: Path, path_env: str) -> str:
    """The unit file text; ``argv`` is the full ExecStart command."""
    return f"""{MARKER}
# Regenerate it with `rocket-critic service install` (e.g. after moving the config or workdir).

[Unit]
Description=rocket-critic pull request reviewer
Documentation=https://github.com/sgstreet/rocket-critic
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
WorkingDirectory={_path(workdir)}
ExecStart={" ".join(quote(a) for a in argv)}
ExecReload=/bin/kill -HUP $MAINPID

# The PATH `service install` ran with, so the service finds git, gh and claude.
Environment={quote("PATH=" + path_env, command=False)}
# Optional settings, e.g. GH_TOKEN for gh without a desktop keyring, or
# ROCKET_CRITIC_GH_TOKEN for [claude.env]. Keep this file private (chmod 600).
EnvironmentFile=-{_path(env_file())}

# Stop: SIGTERM goes to the watcher only, which lets a review in progress
# finish (claude.timeout defaults to 45 minutes); anything still running when
# TimeoutStopSec expires is killed.
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=50min

Restart=on-failure
RestartSec=30s

[Install]
WantedBy=default.target
"""


def systemctl(*args: str, check: bool = True) -> str:
    if not shutil.which("systemctl"):
        raise ServiceError("systemctl not found: systemd user services are not available here")
    try:
        res = proc.run(["systemctl", "--user", *args], timeout=3600, check=check)
    except proc.CommandError as exc:
        raise ServiceError(f"systemctl --user {' '.join(args)} failed: {exc}") from None
    return res.stdout.strip()


def main_pid(name: str) -> int | None:
    """PID of the service's running watcher, or None."""
    try:
        value = systemctl("show", "-p", "MainPID", "--value", f"{check_name(name)}.service", check=False)
    except ServiceError:
        return None
    return int(value) if value.isdigit() and int(value) > 0 else None


def unit_for_pid(pid: int) -> str | None:
    """Name of a unit written by `service install` whose watcher is ``pid``."""
    try:
        files = sorted(unit_dir().glob("*.service"))
    except OSError:
        return None
    for path in files:
        if ours(path) and main_pid(path.stem) == pid:
            return path.stem
    return None


def linger() -> bool | None:
    """Whether systemd keeps your user services running without a login session (None: unknown)."""
    if not shutil.which("loginctl"):
        return None
    res = proc.run(["loginctl", "show-user", getpass.getuser(), "-p", "Linger", "--value"], timeout=60,
                   check=False)
    value = res.stdout.strip()
    return {"yes": True, "no": False}.get(value)


def enable_linger() -> None:
    try:
        proc.run(["loginctl", "enable-linger", getpass.getuser()], timeout=60)
    except (proc.CommandError, FileNotFoundError) as exc:
        raise ServiceError(f"loginctl enable-linger failed: {exc}") from None
