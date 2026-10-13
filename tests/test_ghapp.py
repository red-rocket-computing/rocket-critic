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
    with pytest.raises(GitHubAppError, match="could not sign"):
        GitHubApp(GitHubAppConfig("1", key)).jwt()


def test_configured_installation_skips_lookup():
    app = GitHubApp(GitHubAppConfig("1", Path("k.pem"), installation_id=7))
    assert app.installation_id("any/repo") == 7  # no API call needed
