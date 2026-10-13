import tomllib
from importlib import resources

import pytest

from rocket_critic.config import DEFAULT_ALLOWED_TOOLS, ConfigError, parse


def test_defaults():
    s = parse({})
    assert s.poll_interval == 300
    assert s.review.level == "medium"
    assert s.review.comment is True
    assert s.claude.allowed_tools == DEFAULT_ALLOWED_TOOLS
    assert s.repos == []


def test_sample_config_parses():
    text = resources.files("rocket_critic").joinpath("sample_config.toml").read_text()
    s = parse(tomllib.loads(text))
    assert s.claude.model == "glm-5.3:cloud"
    assert s.repos[0].name == "owner/repo"
    assert s.repos[0].claude.env["ANTHROPIC_BASE_URL"] == "http://localhost:11434"


def test_repo_overrides_merge_over_globals():
    s = parse({
        "claude": {"model": "glm-5.3:cloud", "env": {"A": "1", "B": "2"}},
        "review": {"level": "low", "max_findings": 5},
        "repos": [
            {"name": "o/a"},
            {"name": "o/b", "claude": {"model": "other", "env": {"B": "3"}}, "review": {"level": "high"}},
        ],
    })
    a, b = s.repos
    assert a.claude.model == "glm-5.3:cloud" and a.review.level == "low"
    assert b.claude.model == "other"
    assert b.claude.env == {"A": "1", "B": "3"}
    assert b.review.level == "high" and b.review.max_findings == "5"
    # Globals are untouched by the override.
    assert s.claude.env == {"A": "1", "B": "2"}


def test_unconfigured_repo_uses_globals():
    s = parse({"review": {"level": "max"}})
    r = s.repo("x/y")
    assert r.review.level == "max"
    r.review.level = "low"
    assert s.review.level == "max"


def test_env_expansion_drops_unset_refs():
    s = parse({"claude": {"env": {"GH_TOKEN": "${TOK}", "KEY": "", "URL": "http://${HOST}:1"}}})
    assert s.claude.resolved_env({"HOST": "h"}) == {"KEY": "", "URL": "http://h:1"}
    assert s.claude.resolved_env({"HOST": "h", "TOK": "t"})["GH_TOKEN"] == "t"


@pytest.mark.parametrize("data, msg", [
    ({"review": {"level": "huge"}}, "level"),
    ({"review": {"max_findings": "lots"}}, "max_findings"),
    ({"repos": [{"name": "not-a-repo"}]}, "owner/name"),
    ({"repos": [{"name": "o/r"}, {"name": "O/R"}]}, "twice"),
    ({"repos": [{"name": "o/r", "colour": "red"}]}, "unknown keys"),
    ({"bogus": 1}, "unknown top-level"),
    ({"claude": {"env": {"X": 1}}}, "must be a string"),
    ({"poll_interval": 1}, "poll_interval"),
    ({"review": {"max_reviews_per_pr": -1}}, "max_reviews_per_pr"),
    ({"review": {"incremental": "yes"}}, "incremental"),
    ({"github_app": {"private_key": "k.pem"}}, "app_id"),
    ({"github_app": {"app_id": 1}}, "private_key"),
    ({"github_app": {"app_id": 1, "private_key": "k", "extra": 1}}, "unknown keys"),
])
def test_validation(data, msg):
    with pytest.raises(ConfigError, match=msg):
        parse(data)


def test_relative_workdir_is_relative_to_config_file(tmp_path):
    from rocket_critic.config import load

    cfg = tmp_path / "setup" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text('workdir = "."\n')
    assert load(cfg).workdir == cfg.parent.resolve()
    cfg.write_text('workdir = "data"\n')
    assert load(cfg).workdir == (cfg.parent / "data").resolve()


def test_workdir_argument_picks_config_and_overrides(tmp_path):
    from rocket_critic.config import config_path, load

    wd = tmp_path / "wd"
    wd.mkdir()
    (wd / "config.toml").write_text('workdir = "/somewhere/else"\n')
    assert config_path(None, wd) == wd / "config.toml"
    assert load(workdir=wd).workdir == wd.resolve()
    other = tmp_path / "other.toml"
    other.write_text("")
    s = load(other, wd)  # explicit config wins for the file, -w for the directory
    assert s.path == other and s.workdir == wd.resolve()


def test_sample_config_fills_workdir_and_repos():
    from rocket_critic.config import sample_config

    s = parse(tomllib.loads(sample_config('/a "b"', ["o/one", "o/two"])))
    assert str(s.workdir) == '/a "b"'
    assert [r.name for r in s.repos] == ["o/one", "o/two"]
    assert s.repos[1].ignore_authors == ["dependabot[bot]", "renovate[bot]"]
    with pytest.raises(ConfigError):
        sample_config(".", ["nope"])


@pytest.fixture
def places(tmp_path, monkeypatch):
    """A workdir, a current directory and a user config directory, each able to hold a config."""
    wd, cwd, xdg = tmp_path / "wd", tmp_path / "cwd", tmp_path / "xdg"
    for d in (wd, cwd, xdg / "rocket-critic"):
        d.mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.chdir(cwd)

    def write(where, poll):
        path = {"wd": wd, "cwd": cwd, "user": xdg / "rocket-critic"}[where] / "config.toml"
        path.write_text(f"poll_interval = {poll}\n")
        return path

    return wd, write


def test_config_search_order(places):
    from rocket_critic.config import load

    wd, write = places
    user = write("user", 30)
    assert load().path == user and load(workdir=wd).path == user
    here = write("cwd", 20)
    assert load().path == here and load(workdir=wd).path == here
    work = write("wd", 10)
    assert load(workdir=wd).path == work and load(workdir=wd).poll_interval == 10
    assert load().path == here  # without -w the workdir isn't searched
    assert load(user).path == user  # an explicit -c always wins


def test_config_search_without_any_file(places):
    from rocket_critic.config import load

    wd, _ = places
    with pytest.raises(ConfigError, match="no config file found") as exc:
        load(workdir=wd)
    msg = str(exc.value)
    assert msg.index(str(wd)) < msg.index("cwd") < msg.index("xdg")


def test_cwd_config_relative_workdir(places):
    from rocket_critic.config import load

    _, write = places
    here = write("cwd", 20)
    here.write_text('workdir = "state"\n')
    assert load().workdir == (here.parent / "state").resolve()


def test_add_repo_after_per_repo_subtables(tmp_path):
    from rocket_critic.config import add_repo, load, repo_block

    p = tmp_path / "config.toml"
    p.write_text('# keep me\n[[repos]]\nname = "a/one"\n[repos.review]\nlevel = "high"\n'
                 '[repos.claude.env]\nX = "1"\n')
    assert add_repo(p, repo_block("b/two", base_branches=["main"])) is False
    one, two = load(p).repos
    assert (one.review.level, one.claude.env) == ("high", {"X": "1"})
    assert (two.name, two.review.level, two.claude.env, two.base_branches) == ("b/two", "medium", {}, ["main"])
    assert p.read_text().startswith("# keep me\n")
    with pytest.raises(ConfigError, match="already in"):
        add_repo(p, repo_block("A/One"))


def test_github_app_config(tmp_path):
    from rocket_critic.config import load

    cfg = tmp_path / "config.toml"
    cfg.write_text('[github_app]\napp_id = "123"\nprivate_key = "keys/app.pem"\n')
    app = load(cfg).github_app
    assert app.app_id == "123" and app.private_key == (tmp_path / "keys" / "app.pem").resolve()
    assert app.installation_id is None and app.api_url == "https://api.github.com"
    assert load(cfg).repos == [] and parse({}).github_app is None
