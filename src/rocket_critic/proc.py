"""Subprocess helper shared by the git, gh and claude wrappers."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

log = logging.getLogger(__name__)


class CommandError(RuntimeError):
    """A command exited non-zero or timed out."""

    def __init__(self, cmd: Sequence[str], returncode: int | None, stdout: str, stderr: str, timed_out: bool = False):
        self.cmd = list(cmd)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        if timed_out:
            msg = f"command timed out: {_short(cmd)}"
        else:
            detail = (stderr or stdout).strip().splitlines()
            tail = detail[-1] if detail else ""
            msg = f"command failed ({returncode}): {_short(cmd)}" + (f": {tail}" if tail else "")
        super().__init__(msg)


def _short(cmd: Sequence[str], limit: int = 120) -> str:
    text = " ".join(str(c) for c in cmd)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def run(
    cmd: Sequence[str],
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    input: str | None = None,
    timeout: float | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run ``cmd`` capturing text output.

    The child runs in its own process group so a timeout kills everything it
    spawned (claude starts subprocesses of its own), not just the direct child.
    """
    log.debug("run: %s (cwd=%s)", _short(cmd, 400), cwd)
    proc = subprocess.Popen(
        [str(c) for c in cmd],
        cwd=cwd,
        env=dict(env) if env is not None else None,
        stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        stdout, stderr = proc.communicate()
        raise CommandError(cmd, None, stdout, stderr, timed_out=True) from None
    except BaseException:
        _kill_group(proc)
        proc.wait()
        raise
    result = subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)
    if check and proc.returncode != 0:
        raise CommandError(cmd, proc.returncode, stdout, stderr)
    return result


def _kill_group(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
