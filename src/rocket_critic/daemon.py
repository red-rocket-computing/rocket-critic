"""Background watcher: config reloading, and starting/stopping a detached ``watch``."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from .config import ConfigError, RepoConfig, Settings, load
from .poller import running_pid

log = logging.getLogger(__name__)

LOG_NAME = "rocket-critic.log"
# Set in the environment of a watcher started by `start`, so `update --restart` knows it may restart it.
DAEMON_ENV = "ROCKET_CRITIC_DAEMON"


class ConfigWatcher:
    """Reloads the config file when it changes (or when forced), keeping command-line overrides.

    ``-w`` and ``--interval`` keep their values, ``--repo`` keeps limiting the repos, and the
    workdir never changes while running (it holds the lock, state and checkouts).
    """

    def __init__(self, settings: Settings, *, workdir_override: Path | None = None,
                 interval: int | None = None, only: Sequence[str] = ()):
        self.path = Path(settings.path).resolve()
        self.workdir = settings.workdir
        self.workdir_override = workdir_override
        self.interval = interval
        self.only = list(only)
        self.current = settings
        self._stamp = self._fingerprint()

    def _fingerprint(self) -> tuple[int, int, int] | None:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def repos(self, settings: Settings) -> list[RepoConfig]:
        return [settings.repo(name) for name in self.only] if self.only else settings.repos

    def apply_overrides(self, settings: Settings) -> Settings:
        if self.interval is not None:
            settings.poll_interval = self.interval
        return settings

    def check(self, forced: bool = False) -> tuple[Settings, list[RepoConfig]] | None:
        """A new ``(settings, repos)`` if the file changed (or ``forced``) and is valid, else None."""
        stamp = self._fingerprint()
        if stamp is None:
            # Editors may briefly remove the file while saving; try again next poll.
            if forced:
                log.error("config reload: %s is missing; keeping the current config", self.path)
            return None
        if stamp == self._stamp and not forced:
            return None
        self._stamp = stamp
        try:
            new = self.apply_overrides(load(self.path, self.workdir_override))
        except ConfigError as exc:
            log.error("config reload failed, keeping the current config: %s", exc)
            return None
        if new.workdir != self.workdir:
            log.warning("config reload: workdir changed to %s; restart to use it (still using %s)",
                        new.workdir, self.workdir)
            new.workdir = self.workdir
        repos = self.repos(new)
        if not repos:
            log.error("config reload: no repos configured; keeping the current config")
            return None
        self._log_changes(new, repos)
        self.current = new
        return new, repos

    def _log_changes(self, new: Settings, repos: list[RepoConfig]) -> None:
        old_names = {r.name for r in self.repos(self.current)}
        new_names = {r.name for r in repos}
        parts = []
        if new_names - old_names:
            parts.append("added " + ", ".join(sorted(new_names - old_names)))
        if old_names - new_names:
            parts.append("removed " + ", ".join(sorted(old_names - new_names)))
        if new.poll_interval != self.current.poll_interval:
            parts.append(f"poll every {new.poll_interval}s")
        log.info("config reloaded from %s (%s)", self.path,
                 "; ".join(parts) if parts else f"watching {', '.join(sorted(new_names))}")


def start(argv: Sequence[str], workdir: Path, wait: float = 10.0) -> int:
    """Start ``python -m rocket_critic <argv>`` detached, logging to ``<workdir>/rocket-critic.log``.

    Returns the PID once the child holds the workdir lock. Raises RuntimeError (with the end
    of the log) if it exits or doesn't get the lock within ``wait`` seconds.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    log_path = workdir / LOG_NAME
    with open(log_path, "ab") as out:
        child = subprocess.Popen(
            [sys.executable, "-m", "rocket_critic", *argv],
            stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
            cwd=workdir, start_new_session=True, env={**os.environ, DAEMON_ENV: "1"},
        )
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError(f"the watcher exited (status {child.returncode}):\n{tail(log_path)}")
        if running_pid(workdir) == child.pid:
            return child.pid
        time.sleep(0.1)
    child.terminate()
    raise RuntimeError(f"the watcher did not start within {wait:.0f}s:\n{tail(log_path)}")


def stop(workdir: Path, *, now: bool = False, timeout: float | None = None) -> int | None:
    """Ask the watcher holding ``workdir`` to stop and wait for it.

    It finishes the current review first unless ``now``. Returns the PID that was stopped, or
    None if nothing was running. Raises TimeoutError if it is still running after ``timeout``.
    """
    pid = running_pid(workdir)
    if not pid or pid < 0:
        return None
    os.kill(pid, signal.SIGTERM)
    if now:
        time.sleep(0.5)  # the second signal aborts the review in progress
        if running_pid(workdir) == pid:
            os.kill(pid, signal.SIGTERM)
    deadline = None if timeout is None else time.monotonic() + timeout
    while running_pid(workdir) == pid:
        if deadline is not None and time.monotonic() > deadline:
            raise TimeoutError(f"rocket-critic (pid {pid}) is still finishing a review")
        time.sleep(0.2)
    return pid


def reload(workdir: Path) -> int | None:
    """Send SIGHUP to the watcher holding ``workdir``. Returns its PID, or None if none runs."""
    pid = running_pid(workdir)
    if not pid or pid < 0:
        return None
    os.kill(pid, signal.SIGHUP)
    return pid


def tail(path: Path, lines: int = 15) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except FileNotFoundError:
        return "(no log)"
