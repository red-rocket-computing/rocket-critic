"""Update rocket-critic in place from its GitHub releases (`rocket-critic update`).

Releases are found and downloaded with ``gh api`` (REST only, like the rest of
rocket-critic), so a private repository works with the normal ``gh`` login. The release's wheel is checked against its
``SHA256SUMS`` and installed into the environment rocket-critic runs from:
with ``pipx install --force`` for a pipx install, otherwise with that
environment's pip.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from . import proc

RELEASE_REPO = "sgstreet/rocket-critic"
DIST_NAME = "rocket-critic"
SUMS = "SHA256SUMS"


class UpdateError(RuntimeError):
    pass


@dataclass
class Release:
    tag: str
    version: str
    assets: dict[str, int]  # name -> asset id
    wheel: str  # asset name

    @property
    def has_sums(self) -> bool:
        return SUMS in self.assets


def tag_version(tag: str) -> str:
    """The package version a release tag names: v1.2.3 -> 1.2.3, v1.2.3-rc1 -> 1.2.3rc1."""
    return re.sub(r"-(a|b|rc|alpha|beta)\.?", r"\1", tag.removeprefix("v"))


def version_key(version: str) -> tuple:
    """Sort key for X.Y.Z[suffix] versions; a suffix (rc1) sorts before the plain release."""
    m = re.match(r"^(\d+(?:\.\d+)*)(.*)$", version.strip())
    if not m:
        return ((), 0, version)
    nums = tuple(int(n) for n in m.group(1).split("."))
    nums += (0,) * (3 - len(nums))
    return (nums, 0 if m.group(2) else 1, m.group(2))


def find_release(repo: str, tag: str | None = None, gh: str = "gh") -> Release:
    """The latest release of ``repo`` (or the one tagged ``tag``), via the gh login."""
    if tag and not tag.startswith("v"):
        tag = "v" + tag
    path = f"repos/{repo}/releases/tags/{tag}" if tag else f"repos/{repo}/releases/latest"
    try:
        data = json.loads(proc.run([gh, "api", path], timeout=60).stdout)
    except proc.CommandError as exc:
        what = f"release {tag}" if tag else "the latest release"
        raise UpdateError(f"could not find {what} of {repo}: {exc}") from None
    except FileNotFoundError:
        raise UpdateError(f"{gh} is not installed; it is needed to find and download releases") from None
    version = tag_version(data["tag_name"])
    assets = {a["name"]: a["id"] for a in data.get("assets") or [] if a.get("name") and a.get("id")}
    wanted = f"{DIST_NAME.replace('-', '_')}-{version}-py3-none-any.whl"
    wheel = wanted if wanted in assets else next((n for n in assets if n.endswith(".whl")), "")
    if not wheel:
        raise UpdateError(f"release {data['tag_name']} of {repo} has no wheel to install")
    return Release(data["tag_name"], version, assets, wheel)


def installed_from_source() -> str | None:
    """Where rocket-critic is installed from, if that's an editable source checkout (else None)."""
    try:
        text = metadata.distribution(DIST_NAME).read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return None
    if not text:
        return None
    info = json.loads(text)
    if (info.get("dir_info") or {}).get("editable"):
        return info.get("url", "").removeprefix("file://") or "a source checkout"
    return None


def fetch_asset(repo: str, asset_id: int, gh: str = "gh") -> bytes:
    """A release asset's content, through the REST API (``gh release download`` needs GraphQL)."""
    cmd = [gh, "api", "-H", "Accept: application/octet-stream", f"repos/{repo}/releases/assets/{asset_id}"]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=600, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise UpdateError(f"downloading asset {asset_id} timed out") from None
    if res.returncode != 0:
        detail = res.stderr.decode(errors="replace").strip().splitlines()
        raise UpdateError(f"could not download asset {asset_id}: {detail[0] if detail else res.returncode}")
    return res.stdout


def download(release: Release, repo: str, dest: Path, gh: str = "gh") -> Path:
    """Download the release's wheel (and SHA256SUMS) into ``dest`` and check the wheel's hash."""
    dest.mkdir(parents=True, exist_ok=True)
    for name in [release.wheel] + ([SUMS] if release.has_sums else []):
        (dest / name).write_bytes(fetch_asset(repo, release.assets[name], gh))
    wheel = dest / release.wheel
    if release.has_sums:
        expected = None
        for line in (dest / SUMS).read_text().splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("*") == release.wheel:
                expected = parts[0].lower()
        if expected is None:
            raise UpdateError(f"{SUMS} of {release.tag} has no entry for {release.wheel}")
        actual = hashlib.sha256(wheel.read_bytes()).hexdigest()
        if actual != expected:
            raise UpdateError(f"{release.wheel} does not match {SUMS} (got {actual}, expected {expected}); "
                              "not installing it")
    return wheel


def in_pipx() -> bool:
    """Whether this interpreter is a pipx-managed environment."""
    return f"{os.sep}pipx{os.sep}venvs{os.sep}" in sys.prefix + os.sep


def install_command(wheel: Path, reinstall: bool = False) -> list[str]:
    """The command that installs ``wheel`` into the environment rocket-critic runs from."""
    pipx = shutil.which("pipx")
    if in_pipx() and pipx:
        # Keeps pipx's record of the package in step (a later `pipx upgrade` uses it).
        return [pipx, "install", "--force", str(wheel)]
    cmd = [*pip_command(), "install", "--upgrade"]
    if reinstall:
        cmd += ["--force-reinstall", "--no-deps"]
    return cmd + [str(wheel)]


def pip_command() -> list[str]:
    return [sys.executable, "-m", "pip"]


def install(wheel: Path, reinstall: bool = False) -> None:
    cmd = install_command(wheel, reinstall)
    try:
        proc.run(cmd, timeout=900)
    except proc.CommandError as exc:
        raise UpdateError(f"installing {wheel.name} failed: {exc}") from None
    except FileNotFoundError:
        raise UpdateError(f"could not run {cmd[0]}") from None


def watcher_argv(pid: int) -> list[str] | None:
    """The rocket-critic arguments of the running watcher ``pid`` (from /proc), or None."""
    try:
        args = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").split("\0")
    except OSError:
        return None
    args = [a for a in args if a]
    if "rocket_critic" in args:
        return args[args.index("rocket_critic") + 1:]
    return None


def under_systemd(pid: int) -> bool:
    """Whether ``pid`` was started by systemd (which then restarts it, not rocket-critic)."""
    try:
        environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return False
    return any(var.startswith(b"INVOCATION_ID=") for var in environ)
