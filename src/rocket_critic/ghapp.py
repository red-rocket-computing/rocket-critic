"""GitHub App authentication: a short-lived installation token for each review.

The app's JWT is signed (RS256) with the ``openssl`` command, so no crypto
library is needed; the three REST calls use the standard library and honour
the usual ``HTTPS_PROXY`` / ``NO_PROXY`` variables.
"""

from __future__ import annotations

import base64
import json
import logging
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import GitHubAppConfig

log = logging.getLogger(__name__)


class GitHubAppError(RuntimeError):
    pass


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def sign_rs256(message: bytes, key_path: str, openssl: str = "openssl") -> bytes:
    """RS256 (RSA PKCS#1 v1.5 with SHA-256) signature of ``message``."""
    try:
        res = subprocess.run([openssl, "dgst", "-sha256", "-sign", key_path, "-binary"], input=message,
                             capture_output=True, timeout=30)
    except FileNotFoundError:
        raise GitHubAppError("the openssl command is needed to sign GitHub App tokens but was not found") from None
    if res.returncode != 0:
        detail = res.stderr.decode(errors="replace").strip().splitlines()
        raise GitHubAppError(f"could not sign with {key_path}: {detail[0] if detail else 'openssl failed'} "
                             f"(the file is not a valid private key; {KEY_HINT})")
    return res.stdout


KEY_HINT = "use the .pem file that 'Generate a private key' on the app's settings page downloads"


def check_key(path: Path) -> None:
    """Raise with a specific reason if ``path`` is clearly not a usable PEM private key.

    Only PEM header lines are ever quoted, never key material.
    """
    if not path.is_file():
        raise GitHubAppError(f"GitHub App private key not found: {path}")
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        raise GitHubAppError(f"cannot read GitHub App private key {path}: {exc.strerror}") from None
    if not text.strip():
        raise GitHubAppError(f"GitHub App private key {path} is empty; {KEY_HINT}")
    headers = [line.strip() for line in text.splitlines() if line.strip().startswith("-----BEGIN ")]
    if not headers:
        raise GitHubAppError(f"{path} is not a PEM file (no -----BEGIN line); {KEY_HINT}")
    if not any("PRIVATE KEY" in h for h in headers):
        raise GitHubAppError(f"{path} holds '{headers[0]}', not a private key; {KEY_HINT}")
    if "ENCRYPTED" in text:
        raise GitHubAppError(f"{path} is password-protected; {KEY_HINT}")
    if "-----END " not in text:
        raise GitHubAppError(f"{path} is truncated (no -----END line); {KEY_HINT}")


class GitHubApp:
    def __init__(
        self,
        cfg: GitHubAppConfig,
        *,
        signer: Callable[[bytes, str], bytes] = sign_rs256,
        clock: Callable[[], float] = time.time,
    ):
        self.cfg = cfg
        self._sign = signer
        self._clock = clock
        self._slug: str | None = None
        self._installations: dict[str, int] = {}

    def jwt(self) -> str:
        """An app JWT, valid for about nine minutes (GitHub allows at most ten)."""
        check_key(self.cfg.private_key)
        now = int(self._clock())
        header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
        # iat is backdated to allow for clock drift between here and GitHub.
        claims = {"iat": now - 60, "exp": now + 540, "iss": self.cfg.app_id}
        payload = _b64url(json.dumps(claims, separators=(",", ":")).encode())
        signing_input = f"{header}.{payload}".encode()
        return f"{header}.{payload}.{_b64url(self._sign(signing_input, str(self.cfg.private_key)))}"

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        req = urllib.request.Request(
            f"{self.cfg.api_url}/{path.lstrip('/')}",
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {self.jwt()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "rocket-critic",
                **({"Content-Type": "application/json"} if body is not None else {}),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            try:
                message = json.load(exc).get("message", "")
            except (ValueError, AttributeError):
                message = ""
            raise GitHubAppError(f"{method} {path}: HTTP {exc.code}" + (f": {message}" if message else "")) from None
        except urllib.error.URLError as exc:
            raise GitHubAppError(f"{method} {path}: {exc.reason}") from None

    @property
    def slug(self) -> str:
        if self._slug is None:
            self._slug = self._request("GET", "app")["slug"]
        return self._slug

    @property
    def login(self) -> str:
        """The account reviews are posted as."""
        return f"{self.slug}[bot]"

    def installation_id(self, repo: str) -> int:
        if self.cfg.installation_id:
            return self.cfg.installation_id
        if repo not in self._installations:
            try:
                self._installations[repo] = int(self._request("GET", f"repos/{repo}/installation")["id"])
            except GitHubAppError as exc:
                if "HTTP 404" in str(exc):
                    raise GitHubAppError(f"the GitHub App is not installed on {repo} (install it and select "
                                         "the repo, or add the repo to its installation)") from None
                raise
        return self._installations[repo]

    def token(self, repo: str, read_only: bool = False) -> str:
        """A new installation token (valid one hour) limited to ``repo``; with ``read_only``,
        also limited to reading code and pull requests."""
        body: dict[str, Any] = {"repositories": [repo.split("/", 1)[1]]}
        if read_only:
            body["permissions"] = {"contents": "read", "pull_requests": "read", "metadata": "read"}
        data = self._request("POST", f"app/installations/{self.installation_id(repo)}/access_tokens", body)
        log.debug("minted GitHub App token for %s (expires %s)", repo, data.get("expires_at"))
        return data["token"]
