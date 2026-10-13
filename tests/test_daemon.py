import logging
import os

import pytest

from rocket_critic.config import load
from rocket_critic.daemon import ConfigWatcher
from rocket_critic.poller import AlreadyRunning, lock, running_pid

BASE = 'workdir = "."\npoll_interval = 60\n[[repos]]\nname = "o/a"\n'


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(BASE)
    return path


def edit(path, text):
    old = path.stat().st_mtime_ns
    path.write_text(text)
    os.utime(path, ns=(old + 1_000_000_000, old + 1_000_000_000))  # make sure the change is visible


def test_reload_on_change_only(cfg):
    w = ConfigWatcher(load(cfg))
    assert w.check() is None
    edit(cfg, BASE + '[[repos]]\nname = "o/b"\n')
    settings, repos = w.check()
    assert [r.name for r in repos] == ["o/a", "o/b"]
    assert w.check() is None  # unchanged since
    assert w.check(forced=True) is not None


def test_invalid_edit_keeps_current_config(cfg, caplog):
    w = ConfigWatcher(load(cfg))
    edit(cfg, BASE + "poll_interval = oops\n")
    assert w.check() is None
    assert "config reload failed, keeping the current config" in caplog.text
    edit(cfg, BASE.replace("60", "120"))
    settings, _ = w.check()
    assert settings.poll_interval == 120


def test_overrides_survive_reload(cfg, tmp_path, caplog):
    w = ConfigWatcher(load(cfg), interval=30, only=["o/a"])
    edit(cfg, 'workdir = "elsewhere"\npoll_interval = 999\n[[repos]]\nname = "o/a"\n[[repos]]\nname = "o/b"\n')
    with caplog.at_level(logging.INFO):
        settings, repos = w.check()
    assert settings.poll_interval == 30  # --interval wins
    assert [r.name for r in repos] == ["o/a"]  # --repo still limits
    assert settings.workdir == tmp_path.resolve()  # workdir changes need a restart
    assert "restart to use it" in caplog.text


def test_reload_without_repos_or_file_is_ignored(cfg):
    w = ConfigWatcher(load(cfg))
    edit(cfg, 'workdir = "."\n')
    assert w.check() is None
    cfg.unlink()
    assert w.check() is None and w.check(forced=True) is None


def test_lock_records_pid(tmp_path):
    assert running_pid(tmp_path) is None
    with lock(tmp_path):
        assert running_pid(tmp_path) == os.getpid()
        with pytest.raises(AlreadyRunning, match=f"pid {os.getpid()}"):
            with lock(tmp_path):
                pass
        assert running_pid(tmp_path) == os.getpid()  # a failed attempt doesn't wipe the PID
    assert running_pid(tmp_path) is None
