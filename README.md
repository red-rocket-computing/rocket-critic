# rocket-critic

Code review intelligence tool for inline comments.

rocket-critic watches one or more GitHub repositories for pull requests. When a PR is opened or gets new
commits, it checks the PR out into a local working directory and runs Claude Code's built-in
`/code-review` command in print mode inside that checkout. With `--comment`, `/code-review` posts its
findings to the PR as inline review comments.

Claude Code can use any Anthropic-compatible backend, including Ollama cloud models such as
`glm-5.3:cloud`.

```
poll (gh api …/pulls) → filter → git worktree at PR head → claude -p "/code-review <N> <level> --comment" → record
```

## Requirements

- Python 3.11+
- `git` and an authenticated [`gh`](https://cli.github.com/) (`gh auth login`)
- [Claude Code](https://code.claude.com) (`claude` on `PATH`)
- Optional: [Ollama](https://ollama.com) for non-Anthropic models

## Install

```sh
pipx install git+https://github.com/sgstreet/rocket-critic
# or, from a checkout:
pip install -e '.[dev]'
```

## Quick start

```sh
rocket-critic init                 # writes ~/.config/rocket-critic/config.toml
$EDITOR ~/.config/rocket-critic/config.toml
rocket-critic doctor               # checks gh, git, claude, the model backend and posting identity
rocket-critic review owner/repo#123 --no-comment   # try one PR without posting anything
rocket-critic watch                # poll forever
```

Or keep everything for a setup in one directory:

```sh
rocket-critic -w ~/critic init --repo owner/repo   # ~/critic/config.toml + repos/ prs/ runs/
$EDITOR ~/critic/config.toml
rocket-critic -w ~/critic doctor
rocket-critic -w ~/critic watch
```

## Commands

| Command | What it does |
| --- | --- |
| `init [--repo o/r ...] [--force]` | Write a sample config and create the working directory with its `repos/`, `prs/` and `runs/` directories. `--repo` fills in `[[repos]]` and creates that repo's directories. With `-w DIR` the config is written to `DIR/config.toml`. |
| `doctor` | Check `gh` auth, repo access, the `claude` binary, a test prompt against each configured model, and the GitHub login `/code-review` will post as. |
| `watch [--once] [--repo o/r ...]` | Poll the configured repos every `poll_interval` seconds and review PRs that need it. One instance per workdir; Ctrl-C stops after the current review, and a second Ctrl-C aborts it. |
| `review o/r#N [--level L] [--model M] [--comment/--no-comment] [--force] [--full]` | Review one PR now. Also accepts a PR URL. `--force` ignores filters, history and the per-PR review limit. `--full` reviews the whole PR without earlier-review context. `--no-comment` prints the findings without posting them. |
| `status [--repo o/r] [-n N]` | Recent reviews from the state database. |
| `clean` | Remove leftover PR worktrees. |

Global options:

- `-w/--workdir DIR` (or `$ROCKET_CRITIC_WORKDIR`): the working directory. Its `config.toml` is the config
  file unless `-c` is given, and it holds the repo and PR checkouts, overriding the config's `workdir`.
- `-c/--config PATH` (or `$ROCKET_CRITIC_CONFIG`): the config file. Defaults to `DIR/config.toml` with
  `-w`, else `~/.config/rocket-critic/config.toml`.
- `-v/--verbose`: debug logging.

## Configuration

`rocket-critic init` writes an annotated config you can edit. The main settings:

```toml
workdir = "~/.local/share/rocket-critic"
poll_interval = 300

[claude]
model = "glm-5.3:cloud"
timeout = 2700

[claude.env]                       # environment for the claude subprocess only
ANTHROPIC_BASE_URL = "http://localhost:11434"
ANTHROPIC_AUTH_TOKEN = "ollama"
ANTHROPIC_API_KEY = ""
GH_TOKEN = "${ROCKET_CRITIC_GH_TOKEN}"

[review]
level = "medium"                   # low | medium | high | xhigh | max
comment = true

[[repos]]
name = "owner/repo"
skip_drafts = true
labels = ["needs-review"]          # review PRs with one of these labels...
reviewers = ["my-review-bot"]      # ...or requesting review from one of these users/teams
ignore_authors = ["dependabot[bot]"]
```

A repo can override any `[claude]` or `[review]` setting in its own `[repos.claude]` or `[repos.review]`
table. Keys in `[repos.claude.env]` are merged one by one over `[claude.env]`.

### When a PR is reviewed

- It is open, it is not a draft (unless `skip_drafts = false`), and its author is not in `ignore_authors`.
- If you set `base_branches`, it targets one of them.
- If you set `labels` or `reviewers`, it has one of those labels or requests review from one of those
  users or teams.
- Its added plus deleted lines are at most `max_diff_lines`. A PR over the limit is recorded as `skipped`
  for that commit, so later polls don't fetch it again until the next push.
- Its head commit has not been reviewed yet. A push triggers a new review unless `rereview_on_push = false`.
- The PR has had fewer than `max_reviews_per_pr` reviews (default 5, 0 = no limit). This stops a loop
  with an automated fixer that pushes in response to review comments.
- If `settle_seconds` is set, the head has been unchanged for that long. A burst of pushes then gets one
  review of the last commit.

`status` shows each commit's outcome: `pending` (settling), `running`, `done`, `failed` (will be retried),
`skipped` (too large) or `attention` (see below). `review --force` overrides all of them.

### Retries without duplicate comments

A failed review is retried with exponential backoff, up to `max_attempts` times per commit. Before retrying a
run that got as far as starting `/code-review --comment`, rocket-critic checks GitHub for reviews or comments
the posting identity made since that run started. If it finds any, or it can't tell who the posting identity
is, the commit is marked `attention` instead of retried, so a partial review is never posted twice. The same
check covers a review interrupted by a crash or restart. Use `review --force` to redo it.

History is kept in `<workdir>/state.db`. If the database is lost, rocket-critic skips any commit that
already has a review from the posting identity on GitHub.

### Re-reviews

With `incremental = true` (the default), a re-review after new commits tells `/code-review`:

- which commit was reviewed last, and to report findings only on lines changed since then
  (`git diff <last> HEAD`). After a force-push or rebase, the old commit isn't an ancestor of the new head,
  so the whole PR is reviewed again.
- the earlier inline review threads started by the posting identity, with replies. The model is told not to
  repeat or contradict them, and to treat a point as settled when the author replied disagreeing or
  declining.

This context is appended to the `/code-review` arguments, because `/code-review` ignores
`--append-system-prompt`. Use `review --full` for a fresh full review.

### Working directory

`workdir` in the config sets the working directory. A relative path is relative to the config file's
directory, so a config written by `rocket-critic -w DIR init` has `workdir = "."` and keeps working if
DIR is moved. `-w DIR` on the command line overrides it. Its layout:

```
<workdir>/config.toml                  config file, when created with `-w <workdir> init`
<workdir>/repos/<owner>/<repo>.git     bare partial clone, shared by all PRs of a repo; keeps
                                       refs/rocket-critic/reviewed/<N> at the last reviewed commit
<workdir>/prs/<owner>/<repo>/<N>-<sha> worktree for one review (removed afterwards unless keep_worktrees)
<workdir>/runs/<owner>/<repo>/<N>/     /code-review output (.md) and Claude's JSON result (.json) per commit
<workdir>/state.db                     review history
```

Fork PRs work because rocket-critic fetches `refs/pull/<N>/head`. Private repos use `gh` as the git
credential helper.

## Using Ollama models

Ollama serves an Anthropic-compatible API, so Claude Code can run on Ollama models by setting a few
environment variables in `[claude.env]`. For Ollama cloud models:

```sh
ollama signin                      # once, so *:cloud models are available through the local server
ollama serve                       # if it isn't already running
```

```toml
[claude]
model = "glm-5.3:cloud"

[claude.env]
ANTHROPIC_BASE_URL = "http://localhost:11434"
ANTHROPIC_AUTH_TOKEN = "ollama"
ANTHROPIC_API_KEY = ""
ANTHROPIC_DEFAULT_HAIKU_MODEL = "glm-5.3:cloud"    # Claude Code's background and subagent calls
ANTHROPIC_DEFAULT_SONNET_MODEL = "glm-5.3:cloud"
ANTHROPIC_DEFAULT_OPUS_MODEL = "glm-5.3:cloud"
CLAUDE_CODE_SUBAGENT_MODEL = "glm-5.3:cloud"
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = "1"
```

Run `rocket-critic doctor` to check that the model answers. To use your normal Claude Code login and
Anthropic models, remove the `[claude.env]` table and set `model` to a Claude model or to `""`.
`/code-review` relies on tool calling and subagents, so review quality depends on how well the model
handles both.

## Security

The code under review is untrusted. Its files and PR text reach the model, so a hostile PR can try prompt
injection. rocket-critic limits what such a PR can do:

- Claude runs with `--setting-sources user` and `--strict-mcp-config`. A PR can't load hooks, settings or
  MCP servers from its own `.claude/` directory or `.mcp.json`.
- Claude runs with `--permission-mode dontAsk` and an allowlist (`claude.allowed_tools`). The allowlist
  covers read-only file tools, read-only `git` commands, and the `gh` commands `/code-review` needs.
  `Edit`, `Write`, `WebFetch` and `WebSearch` are disallowed, and `--fix` is never passed.
- To post comments, the allowlist must include `gh api`, and that command can call any GitHub endpoint
  the token allows. Give the claude subprocess its own **fine-grained token** through `GH_TOKEN` in
  `[claude.env]`. Limit the token to the watched repos, with only *Pull requests: read and write* and
  *Contents: read*. A hijacked review can then do no more than post PR comments. rocket-critic uses your
  normal `gh` login for its own polling and cloning.

If `/code-review` needs a tool that is not on the allowlist, the call is denied and logged as a warning
that names the tool. Add the tool to `allowed_tools` if you trust it.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

The end-to-end tests use a local git remote, plus fake `gh` and `claude` scripts in `tests/fakes/`.
They need no network access and no credentials.

## License

AGPL-3.0. See [LICENSE](LICENSE).
