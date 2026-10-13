from pathlib import Path

import pytest

from rocket_critic.config import GitHubAppConfig
from rocket_critic.ghapp import GitHubApp, GitHubAppError


def test_missing_key_is_reported(tmp_path):
    app = GitHubApp(GitHubAppConfig("1", tmp_path / "nope.pem"))
    with pytest.raises(GitHubAppError, match="private key not found"):
        app.jwt()


def test_bad_key_is_reported(tmp_path):
    key = tmp_path / "bad.pem"
    key.write_text("not a key")
    with pytest.raises(GitHubAppError, match="not a PEM file"):
        GitHubApp(GitHubAppConfig("1", key)).jwt()


def test_configured_installation_skips_lookup():
    app = GitHubApp(GitHubAppConfig("1", Path("k.pem"), installation_id=7))
    assert app.installation_id("any/repo") == 7  # no API call needed


@pytest.mark.parametrize("content, reason", [
    ("", "is empty"),
    ("123456\n", "not a PEM file"),
    ("-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n", "holds '-----BEGIN PUBLIC KEY-----'"),
    ("-----BEGIN RSA PRIVATE KEY-----\nProc-Type: 4,ENCRYPTED\nAAAA\n-----END RSA PRIVATE KEY-----\n",
     "password-protected"),
    ("-----BEGIN RSA PRIVATE KEY-----\nAAAA\n", "truncated"),
])
def test_key_problems_are_explained(tmp_path, content, reason):
    key = tmp_path / "app.pem"
    key.write_text(content)
    with pytest.raises(GitHubAppError, match=reason) as exc:
        GitHubApp(GitHubAppConfig("1", key)).jwt()
    assert "Generate a private key" in str(exc.value) and "AAAA" not in str(exc.value)


def test_unparseable_key_gets_a_hint(tmp_path):
    key = tmp_path / "app.pem"
    key.write_text("-----BEGIN RSA PRIVATE KEY-----\nnot base64!\n-----END RSA PRIVATE KEY-----\n")
    with pytest.raises(GitHubAppError, match="not a valid private key"):
        GitHubApp(GitHubAppConfig("1", key)).jwt()
