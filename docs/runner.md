# Runners: jobs on a person's own machine

Status: prototype (branch `local-runner`). This page is the design and the plan.

## Why

Some people want LBW to drive the Claude Code they already use, on their own
computer: their login, their tools, their local checkout. A **runner** is a small
program on that machine. It is linked to one person's LBW account, waits for that
person's jobs, runs each one with their local Claude Code, and sends the result
back. LBW keeps doing what it already does well: the job page, the audit trail,
usage, tests, and the pull request.

## Goals

- A person links a machine once, with a short code, and can disconnect it from
  their profile at any time.
- A job on a model the organization set to run on each person's machine runs
  there, with their own Claude Code.
- LBW never sees, stores, or forwards a Claude credential for those jobs.
- The runner never needs push access to the repository.
- Progress, usage, and audit look the same as a job LBW runs itself.
- LBW can keep the machine's Claude Code current (`claude update`) and see its
  health.
- One install command per platform, no runtime to install first.

## Not goals (for now)

- Running a coworker's job on someone else's machine. A job never runs on a
  machine its acting person did not link, the same rule as credentials.
- Sharing one runner across people. Team or server runners use an API key and
  are covered by the Docker image below.
- Replacing LBW's own agent loop. A project can use both.

## Policy: whose Claude account pays

Running your own Claude Code with your own Claude plan login, headlessly
(`claude -p`), is supported by Anthropic and draws from your plan's limits.
A runner used by its owner stays inside that.

Offering this to other people as a product feature is different. Anthropic's
Agent SDK documentation says that unless previously approved, Anthropic does not
allow third-party developers to offer claude.ai login or rate limits for their
products. So:

- The runner never handles the Claude login. The person signs in through
  Claude Code's own flow, on their machine. LBW only starts `claude`.
- Before LBW offers subscription-backed runs to other organizations, get written
  approval from Anthropic (tracked as an issue).
- The same runner works with an API key (including plan API credits) by setting
  `ANTHROPIC_API_KEY`, which is how the Docker image runs.

## Packaging: Go binary first, Docker image second

| | Native Go binary | Docker image |
|---|---|---|
| Reaches the Claude Code login in the OS keychain | Yes | No: needs `ANTHROPIC_API_KEY` |
| Works in the person's own checkout and git credentials | Yes | Only what is mounted |
| Install | One file, no runtime (`brew`, `curl`, or download) | Docker Desktop, plus mounts |
| Starts at login | launchd or systemd user service | Docker restart policy |
| Updates Claude Code | `claude update` on the host | Rebuild or pull the image |
| Best for | Laptops and desktops | Servers, CI, shared team runners |

The point of a runner is the person's own Claude Code. On macOS that login lives
in the Keychain, and a container cannot reach it. So the laptop path is the Go
binary. Go cross-compiles to every target from one machine, has no runtime to
install, and its standard library covers everything the runner needs (HTTP,
TLS, JSON, processes), so the prototype has no third-party dependencies.

The Docker image is built from the same code. It is for always-on runners on a
server, where Claude Code uses an API key and the project is cloned inside the
container.

## How it fits together

```mermaid
sequenceDiagram
    participant P as Person (browser)
    participant L as LBW server
    participant R as lbw-runner (person's machine)
    participant C as Claude Code (local)
    R->>L: POST /api/runner/link (name)
    L-->>R: device code + short code
    P->>L: enter short code on Profile, Integrations
    R->>L: POST /api/runner/link/token
    L-->>R: runner token (only its hash is stored)
    loop every 30s
        R->>L: heartbeat (runner + Claude Code version)
        L-->>R: commands (update_claude)
    end
    P->>L: POST /api/tasks model_id=<a model on each person's machine>
    R->>L: GET /api/runner/jobs/next (long poll)
    L-->>R: goal, base commit, repository
    R->>C: claude -p … --output-format stream-json (in a worktree)
    C-->>R: stream of events
    R->>L: POST …/events (progress, tokens; keeps the lease)
    R->>L: POST …/finish (summary + git format-patch)
    L->>L: git am on lbw/runner-…, run tests, push, open PR
```

### Linking

Device-code flow, like signing in a TV. The runner calls `/api/runner/link`
and shows an eight-character code. The person types it under **Profile,
Integrations, Your machines**. The runner then collects a token of its own
(`lbwr_…`). LBW keeps only a SHA-256 hash of it. Codes expire after ten
minutes and work once. The runner keeps the token in the macOS Keychain, the
Linux Secret Service, or, when neither exists, a file only the user can read.

### Jobs

- An owner or admin adds a Claude endpoint under Organization, Models with the
  sign-in **Each person's machine**. It takes no key. Its model names are
  Claude Code's `sonnet`, `opus`, and `haiku`, and each model added from it
  is listed on the job form as "(your machine)". Choosing one needs one of the
  person's runners to be online (seen in the last 90 seconds), or the request
  is refused with `409 runner_required`. The same holds for whoever continues
  or reruns the job. Schedules and agent teams run in LBW, so they cannot use
  such a model.
- LBW records the request as usual, with `agent_kind = claude_code`, and sends
  it to the acting person's most recently seen runner.
- The job carries the model name, and the runner passes it to `claude --model`.
  The runner's own `model` setting applies only when a job names none.
- The runner long-polls `/api/runner/jobs/next`. Leasing is atomic, so two polls
  cannot take the same job. A lease lasts two minutes, and each event post
  renews it. If the runner stops checking in, the job fails with a clear reason.
- The runner makes a fresh `git worktree` at the exact commit LBW chose: from a
  checkout the person mapped in `repos`, or from a cached clone made with the
  machine's own git credentials. It runs Claude Code there with a limited tool
  allowlist and `--permission-mode acceptEdits`.
- When Claude Code finishes, the runner commits any leftover changes as the
  person's git identity and sends every commit since the base as one
  `git format-patch` mailbox (5 MB at most). It never pushes.
- LBW applies the commits on `lbw/runner-<job>` with `git am`, keeping the
  person as author and LBW's committer. It then runs the project's tests and, if
  the job asked for one, pushes and opens the pull request with the
  organization's GitHub App. The pull request is the review in the prototype.

### What is stored

Following AGENTS.md, prompts and logs stay out of the database.

| Where | What |
|---|---|
| `runners` | owner, name, token hash, platform, versions, last seen, a waiting command |
| `runner_links` | a pending link: device-code hash, short code, expiry |
| `runner_jobs` | ids, status, times, a short error. No goal, no output |
| memory (`RunnerHub`) | the goal handed to the runner, its events, and the patch, while the job runs |
| audit log | `runner.approve`, `runner.link`, `runner.revoke`, `runner.update_claude`, `task.runner`, `task.usage` |
| usage rollups | each run's tokens under service `claude-code (runner)` and the model Claude Code reported |

### Usage

Claude Code's final `result` line reports tokens per model. The runner turns
each into an LBW `llm` event, counting input the way LBW does (prompt tokens
include cache reads and writes, which are also counted on their own). The
existing rollups, dashboard, and audit summary pick them up unchanged.

### Maintenance

Each heartbeat reports the runner version, the Claude Code version, and whether
Claude Code was found. **Update Claude Code** on the profile queues a command
that the runner runs at its next heartbeat (`claude update`), then reports the
new version. Runner self-updates come with signed releases (see the plan).

### Safety

- Outbound HTTPS only, so no port is opened on the person's machine.
- The token is scoped to one person and can be revoked from the profile. A
  revoked runner stops, and its waiting jobs are cancelled.
- Remote URLs sent to the runner have any user name or token removed.
- Each job runs in its own worktree, removed afterwards. The person's checkout
  is not touched.
- Claude Code gets a tool allowlist (read, edit, and git read commands by
  default). The person widens it in `config.json`, for example with
  `"Bash(make test:*)"`.
- LBW accepts only known event fields from a runner and caps the patch size.
- LBW still runs the tests on the applied branch before it publishes.

## The prototype in this branch

Server:

- `runtime/store/runners.py`: linking, tokens, heartbeat, leasing, routing.
- `runtime/runner_hub.py`: in-memory handoff between a waiting job and the runner.
- `runtime/local_runner.py`: sends the job, shows progress, applies the commits.
- `runner_http.py`: `/api/runner/...` for the runner, `/api/me/runners...` for the profile.
- `runtime/improve.py` `publish_runner_change`: tests, push, and pull request.
- Schema version 10, OpenAPI bodies, and `tests/test_runners.py`.

Runner (`runner/`, Go 1.24, standard library only):

- `lbw-runner login | run | status | doctor | update-claude | install-service | logout`.
- `make runner`, `make runner-test`, `make runner-dist` (macOS, Linux, Windows),
  `make runner-image`.
- Tests use a fake `claude` script and a fake LBW server.

Known limits of the prototype, each tracked as an issue:

- Runner jobs skip the plan and approval gates. The pull request is the review.
- The hub is in memory: one LBW process, and a restart fails running runner jobs.
- No signed releases or self-update yet.
- No organization policy for who may use runners or which tools they allow.
- No secret scan on the incoming patch before LBW applies it.

## Trying it locally

```sh
make runner                       # builds runner/bin/lbw-runner
claude                            # sign in to Claude Code once, if you have not
runner/bin/lbw-runner login --server https://www-local.lookbothways.dev \
  --ca-file .lbw/certs/ca.crt     # the local dev CA from make certs
# enter the code under Profile, Integrations, Your machines
runner/bin/lbw-runner doctor
runner/bin/lbw-runner run
```

Then add a Claude endpoint with the sign-in **Each person's machine** under
Organization, Models, add a model from it, and start a job on that model. To use a checkout you already
have instead of a fresh clone, add it to `~/Library/Application Support/lbw-runner/config.json`
(macOS) or `~/.config/lbw-runner/config.json` (Linux):

```json
{ "repos": { "hhollenstain/lbw": "/Users/you/projects/lbw" } }
```
