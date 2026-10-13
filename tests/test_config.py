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
