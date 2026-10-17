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

By default, reviews post as your own GitHub account. To post as a bot such as `rocket-critic[bot]`, set up a
[GitHub App](#github-app) before running `watch`.

## Commands

| Command | What it does |
| --- | --- |
| `init [--repo o/r ...] [--force]` | Write a sample config and create the working directory with its `repos/`, `prs/` and `runs/` directories. `--repo` fills in `[[repos]]` and creates that repo's directories. With `-w DIR` the config is written to `DIR/config.toml`. |
| `add-repo o/r [--reviewer NAME ...] [--label L ...] [--label-mode sticky\|request] [--command TEXT ...] [--base BRANCH ...] [--ignore-author LOGIN ...] [--drafts] [--no-check]` | Add a repo to the config file found by the usual search. Comments and layout are kept; the sample's `owner/repo` placeholder is replaced, otherwise the entry is appended. Checks with `gh` that the repo is reachable (skip with `--no-check`), refuses duplicates, and creates the repo's directories. Edit the file afterwards for per-repo `[claude]`/`[review]` overrides. |
| `doctor` | Check `gh` auth, repo access, the `claude` binary, a test prompt against each configured model, and the GitHub login `/code-review` will post as. |
| `watch [--once] [--repo o/r ...] [-i/--interval DURATION]` | Poll the configured repos and review PRs that need it. `--interval` sets the time between polls for this run, as seconds or with an `s`/`m`/`h` suffix (`90`, `30s`, `5m`, `1h`; at least 10s), overriding `poll_interval` (also via `$ROCKET_CRITIC_POLL_INTERVAL`). It is also the base of the retry backoff. One instance per workdir; Ctrl-C stops after the current review, and a second Ctrl-C aborts it. |
| `review o/r#N [--level L] [--model M] [--comment/--no-comment] [--force] [--full]` | Review one PR now. Also accepts a PR URL. `--force` ignores filters, history and the per-PR review limit. `--full` reviews the whole PR without earlier-review context. `--no-comment` prints the findings without posting them. |
| `start [--repo o/r ...] [-i/--interval DURATION]` | Run `watch` in the background ([daemon mode](#running-in-the-background)), logging to `WORKDIR/rocket-critic.log`. Reports success once the watcher is running, or shows the end of the log if it failed to start. |
| `stop [--now] [--timeout SECONDS]` | Stop the background watcher. A review in progress finishes first unless `--now`. |
| `reload` | Make the running watcher re-read the config now and poll straight away. |
| `status [--repo o/r] [-n N]` | Whether the watcher is running (and its PID), then recent reviews from the state database. |
| `clean` | Remove leftover PR worktrees. |

Global options:

- `-w/--workdir DIR` (or `$ROCKET_CRITIC_WORKDIR`): the working directory. It holds the repo and PR
  checkouts, overriding the config's `workdir`, and is the first place searched for the config file.
- `-c/--config PATH` (or `$ROCKET_CRITIC_CONFIG`): use this config file and skip the search.
- `-v/--verbose`: debug logging.

Without `-c`, the config file is the first of these that exists:

1. `DIR/config.toml`, with `-w DIR`
2. `./config.toml` in the current directory
3. `~/.config/rocket-critic/config.toml` (`$XDG_CONFIG_HOME/rocket-critic/config.toml` if set)

`doctor` prints which file it loaded. `init` writes to `-c PATH`, else `DIR/config.toml` with `-w`, else
the user config directory.

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
posting = "claude"                 # or "rocket-critic": checked severities, one review (see below)
guidelines = ""                    # extra review rules, a Markdown file

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
  users or teams. Comment `commands` also ask for reviews. The triggers differ after the first review
  (see below).
- Its added plus deleted lines are at most `max_diff_lines`. A PR over the limit is recorded as `skipped`
  for that commit, so later polls don't fetch it again until the next push.
- Its head commit has not been reviewed yet. A push triggers a new review unless `rereview_on_push = false`.
- The PR has had fewer than `max_reviews_per_pr` reviews (default 5, 0 = no limit). This stops a loop
  with an automated fixer that pushes in response to review comments.
- If `settle_seconds` is set, the head has been unchanged for that long. A burst of pushes then gets one
  review of the last commit.

#### Ways to ask for a review

Each repo can use any mix of these triggers:

| Trigger | Config | Reviews when | Further pushes |
| --- | --- | --- | --- |
| Sticky label | `labels = ["ai-review"]` | the PR has the label | reviewed while the label stays on |
| Request label | `labels = [...]`, `label_mode = "request"` | the label is added | not reviewed until the label is added again |
| Review request | `reviewers = ["name"]` | review is requested from that user/team | not reviewed until review is re-requested |
| Comment command | `commands = ["/review"]` | someone with write access comments the command | not reviewed until asked again |

Every trigger stops at `max_reviews_per_pr`. Adding the same request again with no new commits gives a
full review of that commit; after new commits, the re-review is incremental. Either way the earlier review
threads are passed along.

**Sticky label.** A label stays on the PR, so with `labels` every push is reviewed again (up to
`max_reviews_per_pr`). Remove the label to stop.

**Request label.** With `label_mode = "request"`, adding a label asks for one review. Once the review has
posted, rocket-critic removes the label, as the posting identity (e.g. `rocket-critic[bot]`). Add it again
to ask for another review. This is the closest thing to requesting a review from a GitHub App, which GitHub
doesn't allow.

**Review request.** A review request works like asking a person. GitHub drops the request once that reviewer posts a review, so
later pushes are **not** reviewed until someone clicks *Re-request review*. A re-request triggers a review
even without new commits: an incremental one after new commits, otherwise a full one of the same commit.
Either way the earlier review threads are passed along. To tell a re-request from a request that is still
open because a review posted nothing, rocket-critic checks the PR timeline (one API call, only for such
PRs) and acts only on a request made after its last review started. The same check applies to request
labels, so a label that couldn't be removed doesn't cause repeated reviews. Repeat reviews count toward
`max_reviews_per_pr`.

**Comment command.** With `commands = ["/review", "@rocket-critic review"]`, a PR comment with a line that
is, or starts with, one of the commands (case-insensitive) asks for a review. rocket-critic reacts 👀 when
it takes the request, or 😕 when the size limit or `max_reviews_per_pr` refuses it.
- **Who can use it:** only the repo owner, organization members and collaborators (people with write
  access). Comments posted before rocket-critic started watching the repo are ignored, and each comment
  is used once.
- **What it overrides:** an explicit command also overrides the label/reviewer triggers, drafts,
  `ignore_authors` and `base_branches`.
- **Cost:** checking costs one API call per repo per poll.

`status` shows each commit's outcome: `pending` (settling), `running`, `done`, `failed` (will be retried),
`skipped` (too large) or `attention` (see below). `review --force` overrides all of them.

### Retries without duplicate comments

A failed review is retried with exponential backoff, up to `max_attempts` times per commit. Before retrying a
run that got as far as starting `/code-review --comment`, rocket-critic checks GitHub for reviews or comments
the posting identity made since that run started (with `posting = "rocket-critic"`, also before retrying a
run that got as far as posting). If it finds any, or it can't tell who the posting identity
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

### Severities and review rules

Every finding gets one of three severities, as in GitHub's hosted Claude Code Review:

| | Severity | Meaning |
|---|---|---|
| 🔴 | Important | A bug, security problem or regression to fix before merging |
| 🟡 | Nit | Worth fixing, not blocking |
| 🟣 | Pre-existing | A real problem in code the PR didn't introduce |

rocket-critic writes these rules to a `CLAUDE.md` in the review's scratch directory and sets
`CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1`, so Claude loads that file as memory for the review. The
file also holds:

- **`REVIEW.md` from the repo's default branch** (`review_md = true`, the default), in the same spirit as
  the hosted Code Review: use it to redefine what counts as Important, cap the number of nits, or skip
  paths and kinds of findings. It is always read from the default branch, never from the PR, so a PR
  can't change the rules it is reviewed by.
- **Your own guidelines**: `guidelines = "review-guidelines.md"` in `[review]` or `[repos.review]`, a
  Markdown file (a relative path is relative to the config file).

The PR's own `CLAUDE.md` is not loaded: `--setting-sources user` leaves out project memory.

`post_nits = false` drops Nit findings.

### Who posts the review

With `posting = "claude"` (the default), `/code-review --comment` posts its own comments and is told to
start each one with its severity marker. That is an instruction to the model, which may not always
follow it.

With `posting = "rocket-critic"`, rocket-critic posts the review itself and checks it first:

1. `/code-review` runs without `--comment` and writes its findings to `findings.json` in the scratch
   directory: path, line (optional start line), severity, title and body for each.
2. rocket-critic checks every finding. Each must have a path, a positive line number and a severity it
   recognises (`important`, `nit`, `pre-existing`, or an unambiguous synonym such as `critical` or
   `minor`). If any finding fails, **nothing is posted**: the review is recorded as `failed`, with the
   rejected file kept next to the run output, and retried like any other failure.
3. It adds the marker itself (`🔴 **Important**: …`) and posts **one review**: an inline comment for each
   finding on a line of the PR's diff, and the rest, such as most Pre-existing findings, listed in the
   review body with their `path:line`, under a count of findings by severity. If GitHub still refuses
   a comment's position, every finding goes in the body instead.

In this mode Claude can't post: `gh api` is left out of its allowed tools, and with a GitHub App its
token is read-only. With a user token, rocket-critic also checks that nothing was posted during the run
before posting, and marks the commit `attention` if something was. `review --no-comment` saves the
checked review (`WORKDIR/runs/OWNER/REPO/N/SHA.review.json`, and the run's `.md` output) without posting
it.

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

## Running in the background

`rocket-critic start` runs `watch` as a background process (daemon mode). It takes the same `--repo` and
`--interval` options:

```sh
cd ~/Workspace/rocket-critic/work
../.venv/bin/rocket-critic -w . start --interval 2m
../.venv/bin/rocket-critic -w . status        # watcher: running (pid 12345); log .../rocket-critic.log
tail -f rocket-critic.log
../.venv/bin/rocket-critic -w . stop          # waits for a review in progress; --now aborts it
```

There is one watcher per workdir. Its PID is kept in `WORKDIR/rocket-critic.lock` for as long as it runs,
so `status`, `stop` and `reload` find it. A crash can't leave a stale PID behind.

### Configuration updates

A running watcher (from `start` or a plain `watch`) picks up config changes without restarting:

- Before each poll it checks whether the config file changed, and if so reloads it: added or removed repos,
  triggers, review settings, `[claude]`, `[github_app]`, `poll_interval`.
- `rocket-critic reload` (or `kill -HUP <pid>`) reloads immediately and polls straight away, instead of
  waiting for the next poll.
- An invalid edit is logged (`config reload failed, keeping the current config: ...`) and the watcher keeps
  running with the previous config; save a fixed file to retry. A config with no repos is refused the same
  way.
- Options given on the command line stay in force: `--interval` keeps overriding `poll_interval`, `--repo`
  keeps limiting the repos, and `-w` keeps choosing the workdir.
- A change to `workdir` needs a restart (`stop`, then `start`), because the running watcher holds that
  directory's lock, history and checkouts.

Each reload is logged with what changed, e.g. `config reloaded from .../config.toml (added sgstreet/other)`.

### As a systemd service

On a server, systemd can supervise the watcher instead of `start`/`stop`: it starts it at boot, restarts it
if it crashes and keeps its logs. systemd runs `watch` in the foreground, and `systemctl reload` sends
the same SIGHUP as `rocket-critic reload`. Ready-to-copy units are in
[`contrib/systemd/`](contrib/systemd/):

| File | Runs as | Use when |
| --- | --- | --- |
| [`user/rocket-critic.service`](contrib/systemd/user/rocket-critic.service) | you, under `systemctl --user` | no root needed; uses `%h` so paths follow your home directory |
| [`system/rocket-critic.service`](contrib/systemd/system/rocket-critic.service) | you (`User=`), under the system manager | starts at boot with no login session; needs root to install |
| [`rocket-critic.env.example`](contrib/systemd/rocket-critic.env.example) | | optional environment (tokens, poll period) |

The user unit, with paths for a checkout in `~/Workspace/rocket-critic` and the workdir in `work/`:

```ini
# ~/.config/systemd/user/rocket-critic.service
[Unit]
Description=rocket-critic pull request reviewer
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/Workspace/rocket-critic/work
ExecStart=%h/Workspace/rocket-critic/.venv/bin/rocket-critic -w . watch
ExecReload=/bin/kill -HUP $MAINPID
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
EnvironmentFile=-%h/.config/rocket-critic/rocket-critic.env
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=50min
Restart=on-failure
RestartSec=30s

[Install]
WantedBy=default.target
```

Install and run it:

```sh
mkdir -p ~/.config/systemd/user
cp contrib/systemd/user/rocket-critic.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now rocket-critic
loginctl enable-linger "$USER"              # keep running after logout, and start at boot

systemctl --user status rocket-critic
systemctl --user reload rocket-critic       # re-read config.toml now (it's also reloaded on change)
journalctl --user -u rocket-critic -f
```

For the system unit, replace `stephen` and the paths in the file, then install it as root:

```sh
sudo cp contrib/systemd/system/rocket-critic.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rocket-critic
journalctl -u rocket-critic -f
```

Notes:

- **Stopping waits for the review.** `KillMode=mixed` sends SIGTERM to the watcher only, so a review in
  progress finishes before it exits. Without it, systemd would also signal the `claude` process and cut the
  review short. `TimeoutStopSec=50min` covers the default 45-minute `claude.timeout`; after that, anything
  still running is killed. Raise both together if you raise the timeout.
- **`PATH`.** Services don't load your shell profile. The `Environment=PATH=` line must include the
  directories holding `git`, `gh`, `claude` (often `~/.local/bin`) and `openssl`. Check with
  `systemctl --user show-environment` or by reading `doctor` output from the service's context.
- **`gh` login.** If `gh auth status` says `(keyring)`, the token is in your desktop keyring, which a
  service often can't unlock (always the case for the system unit). Either log `gh` in again with
  `gh auth login --insecure-storage`, which keeps the token in `~/.config/gh/hosts.yml` (private to you),
  or put `GH_TOKEN=...` in the environment file. That token is also what `/code-review` posts with unless
  `[github_app]` or a `GH_TOKEN` in `[claude.env]` is set.
- **Environment file.** Copy `contrib/systemd/rocket-critic.env.example` to
  `~/.config/rocket-critic/rocket-critic.env` and `chmod 600` it. The `-` in `EnvironmentFile=-...` makes it
  optional. Use plain `KEY=value` lines, without `export`.
- **Ollama.** If the reviews use local Ollama models, the Ollama server must be running. For the system
  unit, add `ollama.service` to `After=`.
- **Don't mix** `systemctl` and `rocket-critic start` for the same workdir: there is only one lock, so
  the second one refuses to start. `rocket-critic status` shows the watcher either way.

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
  `WebFetch` and `WebSearch` are disallowed, and `--fix` is never passed. Claude can write files only in
  a per-review scratch directory (`WORKDIR/runs/OWNER/REPO/N/scratch-SHA/`, deleted afterwards), never in
  the checkout.
- With `posting = "claude"`, the allowlist must include `gh api` to post comments, and that command can
  call any GitHub endpoint the token allows. (With `posting = "rocket-critic"`, Claude gets no `gh api`,
  and with a GitHub App only a read-only token.) Give `/code-review` its own limited identity: a [GitHub App](#github-app)
  (recommended; its tokens last an hour and cover only the repo being reviewed) or a
  [fine-grained token](#review-token) limited to the watched repos, with only *Pull requests: read and
  write* and *Contents: read*. A hijacked review can then do no more than post PR comments.
  rocket-critic uses your normal `gh` login for its own polling and cloning.

If `/code-review` tries a tool that is not on the allowlist, the call is denied and logged as a warning
that names the command. The review carries on without it. Many such denials are expected and should stay
denied:

- **Build and test commands** (`make`, `npm`, `cargo`, `pytest`, scripts from the repo). They run code from
  the PR, and so does `make -n`, through `$(shell ...)` and `+` lines.
- **Shell file commands** (`ls`, `cat`, `head`, `grep`, `find`). The Read, Grep and Glob tools do the same
  job but are confined to the checkout; the shell versions can read anything you can, such as `~/.ssh`,
  the GitHub App key or the gh token, and a prompt-injected review could post what it reads.

Only add a command to `allowed_tools` if it can neither run code from the PR nor read files outside the
checkout. The notes rocket-critic adds to `/code-review`'s arguments steer it to the right tools, which
keeps such denials rare.

**Why comments are posted from files.** In `dontAsk` mode, Claude Code refuses shell commands it can't
check statically, whatever the allowlist says. That includes heredocs and quoted strings containing
backticks or `$(...)`, and review comments almost always contain backticks. So rocket-critic gives each
review a scratch directory and tells `/code-review` (in its arguments) to write each JSON payload there
with the Write tool and send it with `gh api ... --input <file>`. It also tells it to run `git` in the
checkout without `-C`. If the log shows denials like `Bash(gh api ... <<'PAYLOAD' ...)`, the model ignored
that note. The comment wasn't posted, so re-run the review with `review OWNER/REPO#N --force`; a more
capable model follows the note more reliably.

If you set `allowed_tools` or `disallowed_tools` yourself, they replace the defaults. Keep
`Bash(gh api:*)`, and add `Bash(echo:*)` and `Bash(git remote -v)` to match the current defaults. A bare
`Edit`/`Write` in `disallowed_tools` is dropped automatically: it would override the scratch-directory
rule, and `dontAsk` refuses every other write anyway.

## Posting identity: token or GitHub App

`/code-review --comment` posts as whichever GitHub identity the Claude subprocess has. rocket-critic itself
always uses your `gh` login to list PRs and clone; only the review posting changes.

| Option | Reviews appear as | Setup |
| --- | --- | --- |
| Nothing configured | you (your `gh` login) | none |
| Token in `[claude.env]` | the account that owns the token | [Review token](#review-token) |
| `[github_app]` | `<app-name>[bot]` | [GitHub App](#github-app) (recommended) |

If both a token and an app are configured, a `GH_TOKEN` that resolves in `[claude.env]` wins. `doctor`
prints which one is used and who reviews will post as.

### Review token

The sample config reads the token from an environment variable:

```toml
[claude.env]
GH_TOKEN = "${ROCKET_CRITIC_GH_TOKEN}"
```

Export it in the shell (or service) that runs rocket-critic: `export ROCKET_CRITIC_GH_TOKEN=github_pat_...`.
If the variable is unset or empty, the `GH_TOKEN` line is skipped and `/code-review` falls back to your `gh`
login. `watch` and `review` log a warning when this happens, and `doctor` shows where the token comes from:

```
[ok  ] GitHub token for /code-review: GH_TOKEN from [claude.env] (fine-grained token)
[ok  ] /code-review will post as: my-review-bot
```

Reviews post under the account that owns the token. A token you create on your own account still posts as
you; to post under another name, create the token on a separate GitHub account (a "machine user") that has
access to the repos. `doctor` points this out when the token's account is the same as your `gh` login.

### GitHub App

A GitHub App gives the reviews their own name (`<app-name>[bot]`) without a second user account. Its tokens
last an hour and are limited to the one repo being reviewed. Setup has three parts, all in GitHub's web UI:
create the app, download its private key, and install it on your repos.

#### 1. Create the app

1. On github.com: your profile picture → **Settings** → **Developer settings** (bottom of the left sidebar)
   → **GitHub Apps** → **New GitHub App**. For an organization, use the organization's **Settings →
   Developer settings** instead, so the org owns the app.
2. Fill in:
   - **GitHub App name**: e.g. `rocket-critic`. Reviews will appear as `rocket-critic[bot]`; names are
     unique across GitHub, so you may need a variant.
   - **Homepage URL**: anything, e.g. this repo's URL.
   - **Webhook**: untick **Active**. rocket-critic polls, so it needs no webhook.
   - **Repository permissions**: **Pull requests: Read and write** and **Contents: Read-only**.
     **Metadata: Read-only** is added automatically. Leave everything else at **No access**.
   - **Where can this GitHub App be installed?**: **Only on this account**.
3. Click **Create GitHub App**. The page that opens shows the **App ID** (and Client ID) near the top.

#### 2. Download the private key

1. On the app's **General** page, scroll to **Private keys** and click **Generate a private key**.
2. Your browser downloads a file named like `rocket-critic.2026-10-13.private-key.pem`. GitHub shows nothing
   to copy, and the key can't be downloaded again later (see [Private keys](#private-keys)).
3. Put it on the machine that runs rocket-critic, as a file. Don't copy and paste its text:

   ```sh
   mkdir -p ~/.config/rocket-critic
   # browser on the same machine:
   mv ~/Downloads/*.private-key.pem ~/.config/rocket-critic/rocket-critic.pem
   # or browser on another computer (run this there):
   scp ~/Downloads/*.private-key.pem you@server:~/.config/rocket-critic/rocket-critic.pem
   ```

4. Restrict it and check it:

   ```sh
   chmod 600 ~/.config/rocket-critic/rocket-critic.pem
   openssl rsa -in ~/.config/rocket-critic/rocket-critic.pem -check -noout   # prints: RSA key ok
   ```

#### 3. Install the app on your repos

1. In the app's settings, click **Install App** in the left sidebar.
2. Click **Install** next to the account that owns the repos (or **Configure** if it's already installed).
3. Choose **Only select repositories**, pick the repos rocket-critic should review, and click **Install**
   (or **Save**).

To review another repo later, add it in the same place (**Install App → Configure → Repository access**)
and to the config with `rocket-critic add-repo OWNER/REPO`.

#### 4. Configure and check

Add to the config:

```toml
[github_app]
app_id = 123456                                        # the App ID from step 1
private_key = "~/.config/rocket-critic/rocket-critic.pem"
# installation_id = 12345678    # optional: skip the per-repo lookup
# api_url = "https://github.example.com/api/v3"        # GitHub Enterprise Server only
```

A relative `private_key` path is relative to the config file's directory. Then run `rocket-critic doctor`:

```
[ok  ] openssl on PATH (signs GitHub App tokens): /usr/bin/openssl
[ok  ] GitHub App 123456: rocket-critic[bot], key /home/you/.config/rocket-critic/rocket-critic.pem
[ok  ] GitHub App on you/your-repo: installation 12345678, token issued
[ok  ] GitHub token for /code-review: GitHub App 123456 (a new token, limited to the repo, for each review)
[ok  ] /code-review will post as: rocket-critic[bot]
```

#### How it works and what to know

- For each review, rocket-critic signs a short-lived JWT with the private key (using the `openssl` command),
  exchanges it for an installation token limited to that repo, and passes the token to `/code-review` as
  `GH_TOKEN`. It never prints or copies the key.
- Tokens last an hour, so keep `claude.timeout` under about 55 minutes (the default is 45).
- GitHub doesn't let you request a review from an app, so a `reviewers` trigger can't name
  `rocket-critic[bot]`. Use a request label or a comment command instead:
  `add-repo OWNER/REPO --label ai-review --label-mode request --command /review`. Label removal and the
  👀 reaction then appear as `rocket-critic[bot]`.
- If the sample config's `GH_TOKEN = "${ROCKET_CRITIC_GH_TOKEN}"` is still present with the variable unset,
  it is simply skipped and the app is used; no warning is shown.

### Private keys

- **GitHub keeps no copy.** It stores only the public half. The `.pem` exists only where your browser saved
  it, and **Generate a private key** creates a new key each time rather than re-downloading an old one.
- **Keep it private.** `chmod 600`, never commit it, never paste it into chats or issues. If you keep a
  backup, use a password manager or an encrypted disk.
- **Lost or broken key:** click **Generate a private key** again, point `private_key` at the new file, run
  `doctor`, then delete the old key under **Private keys**. The app, its installation and rocket-critic's
  review history are unaffected.
- **Rotation without downtime:** an app can have several valid keys at once. Generate the new key, switch the
  config, check with `doctor`, then delete the old key.
- **Leaked key:** delete it under **Private keys** right away. Its tokens stop working within the hour, and
  until then they can only do what the app's permissions allow, on the repos it's installed on.

#### Troubleshooting

| `doctor` says | Meaning | Fix |
| --- | --- | --- |
| `private key not found` | Wrong path in `private_key` | Fix the path (relative paths start from the config file's directory) |
| `is empty` / `is truncated (no -----END line)` | Incomplete file | Download a new key (step 2) |
| `is not a PEM file` | Not a key: e.g. a saved web page or the App ID | Use the downloaded `*.private-key.pem` |
| `holds '-----BEGIN PUBLIC KEY-----'` (or another type) | A public key or certificate | Use the private key |
| `is password-protected` | Encrypted key | Use GitHub's unencrypted download |
| `not a valid private key` / openssl `Could not find private key` | Damaged, usually by copy and paste | Copy the file itself (`scp`), or download a new key |
| `GitHub App 123456: ... HTTP 401` | Key and `app_id` don't belong together, or the key was deleted on GitHub | Check the App ID, or generate a new key |
| `not installed on OWNER/REPO` | The installation doesn't include that repo | **Install App → Configure** and add the repo (step 3) |
| `repo owner/repo: this is the sample placeholder` | Config still has the template entry | `rocket-critic add-repo OWNER/REPO` |

## Development

```sh
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

The end-to-end tests use a local git remote, plus fake `gh` and `claude` scripts in `tests/fakes/`.
They need no network access and no credentials.

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on every pull request and push to `main`. It runs
the tests on Python 3.11–3.13, `pyflakes` (also in the `dev` extras: `python -m pyflakes src tests`), and a
check that the wheel builds, installs and runs.

### Releasing

Releases are built by [`.github/workflows/release.yml`](.github/workflows/release.yml). The version is
defined once, as `__version__` in `src/rocket_critic/__init__.py`, and the release tag is `v` + that version.

1. Set `__version__` (e.g. `"0.2.0"`, or `"0.2.0rc1"` for a release candidate) and merge that to `main`.
2. Start the release, in either of these ways:
   - **Run the workflow** (**Actions → Release → Run workflow** on `main`, with **tag** `v0.2.0`), or from
     the command line:

     ```sh
     gh workflow run release.yml --ref main -f tag=v0.2.0
     ```

     When the tag doesn't exist yet, the workflow creates it on the `main` commit it ran on, after the checks
     pass. Nothing but the version bump has to be pushed, so this also works for tools and sessions that
     can't push tags. New tags are only created from the default branch.
   - **Push a tag** on the merge commit:

     ```sh
     git tag -a v0.2.0 -m "rocket-critic 0.2.0"
     git push origin v0.2.0
     ```

The workflow then:
- checks that the tag matches `__version__` (`v0.2.0` for `0.2.0`; `v0.2.0-rc1` or `v0.2.0rc1` for
  `0.2.0rc1`);
- runs the [CI workflow](.github/workflows/ci.yml): tests on Python 3.11–3.13, lint, wheel check;
- builds the wheel and source archive and checks that the wheel installs and runs;
- creates the tag if the run was started by hand for a new one;
- creates a GitHub release with generated notes, the two files and `SHA256SUMS`. Tags with a suffix become
  pre-releases.

If a release for the tag already exists, for example because it was created on GitHub's **Draft a new
release** page, the workflow attaches the files to it and keeps its title and notes. A draft stays a draft
until you publish it.

Other ways to run the workflow by hand:
- with **tag** set to an existing tag (e.g. `v0.1.0`), it checks out that tag, tests, builds and publishes
  it. Use this to add the files to a release that has none;
- with **tag** empty, it checks, tests and builds the selected branch without publishing anything.

To install a release:

```sh
pipx install https://github.com/sgstreet/rocket-critic/releases/download/v0.2.0/rocket_critic-0.2.0-py3-none-any.whl
```

## License

AGPL-3.0. See [LICENSE](LICENSE).
