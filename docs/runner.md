# Runners: jobs on a person's own machine

Status: prototype (branch `local-runner`). This page is the design of the prototype.
What to build next, including the security model, is `docs/runner-spec.md`.

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
- Claude Code runs headless, so nobody can approve a command. Two things let a
  build run the project's tools anyway:
  - **Project commands.** A build job carries the project's setup, test, lint,
    and preview commands (`commands`), and the runner allows exactly those as
    `Bash(<command>:*)`. LBW runs the same commands on the result.
  - **Sandbox.** The runner turns on Claude Code's sandbox with `--settings`:
    any shell command is approved inside it, writes stay in the worktree, the
    network reaches only `sandbox_domains` (by default the common package
    registries and GitHub), a command can never leave the sandbox
    (`allowUnsandboxedCommands: false`), and Claude Code stops instead of
    running unsandboxed when the sandbox cannot start (`failIfUnavailable`).
    It is on by default except on Windows. Linux needs `bubblewrap` and
    `socat`, which `lbw-runner doctor` checks. `"sandbox": false` in
    `config.json`, or `LBW_RUNNER_SANDBOX=off`, turns it off. The Docker image
    sets that, because the container is the boundary. Docker itself does not
    run inside the sandbox.
- When Claude Code finishes, the runner commits any leftover changes as the
  person's git identity and sends every commit since the base as one
  `git format-patch` mailbox (5 MB at most). It never pushes.
- LBW applies the commits on `lbw/runner-<job>` with `git am`, keeping the
  person as author and LBW's committer. It then runs the project's tests and, if
  the job asked for one, pushes and opens the pull request with the
  organization's GitHub App. The gates below can hold it first.

### Gates

A runner job stops at the same gates as a job in LBW (`runtime/local_runner.py`).

- **Spec.** When the job writes a spec first, LBW sends the runner a job with
  `phase: "spec"` and the bundled `sdd` skill as its `instructions`. The runner
  runs Claude Code with only Read, Glob, and Grep: no editing tools, no shell,
  no subagents, and no sandbox. Nothing can be written before a person accepts
  the spec. The runner throws the worktree away and sends the spec back as the
  summary. The job waits at `spec`. Sending it
  back asks the runner to rewrite it from the note.
- **Plan.** When the triage gate asks, the runner writes the plan the same way
  (`phase: "plan"`), and the job waits at `triage` until a person builds it or
  hands it off.
- **Build.** The build job carries the accepted spec and plan. LBW applies the
  runner's commits in a worktree of their own, because LBW itself keeps `.lbw/`
  in the checkout's .gitignore while the job runs. It then checks out the
  result as `lbw/runner-<job>`, keeping a person's edits in `git stash`, and
  commits the spec with it, so a spec alone never ships.
- **Start.** The runner fetches its start commit from the remote, so LBW starts
  it from the newest commit a remote branch already has. Commits only this
  server has, such as a failed job's applied commits that a rerun continues
  from, go to the runner as `start_patch`. Nothing has to be pushed first.
- **Approval.** When the approval gate asks, the commits wait on that branch.
  Approving runs the tests and publishes without the runner. Sending it back
  gives the runner the earlier commits as `start_patch`, and it continues from
  them.
- A runner too old to know `phase` answers a spec or plan job with a change.
  LBW refuses that answer and asks the person to update `lbw-runner`.

### Local models

A person can also run jobs on a model server on their own machine, such as
Ollama, LM Studio, or vLLM. Here LBW runs its own plan, build, and review loop
as usual. Only the model calls go to the person's machine.

- **Organization.** Under Organization, Models, Local models, an owner or admin
  chooses Off, On for everyone, or On for chosen people, and then turns it on
  person by person. The default is Off.
- **Profile.** Under Profile, Integrations, Local models, a person it is on for
  turns on their server, picks Ollama, LM Studio, vLLM, or OpenAI-compatible,
  and sets its address (Ollama's `http://127.0.0.1:11434` by default).
  **Sync models** asks their runner for `GET /v1/models` and stores the names.
  A new address forgets the old names.
- **Job form.** Each synced model shows as "name · Ollama (your machine)".
  Choosing one needs one of the person's runners to be online and relaying,
  or the request is refused with `409 runner_required`.
- **Relay.** LBW's OpenAI client sends each call through an httpx transport
  that queues it in memory for the acting person (`runtime/llm_relay.py`).
  The runner long-polls `GET /api/runner/llm/next`, confirms the call with
  `POST /api/runner/llm/{call_id}/ack`, sends it to the address, and posts the
  answer to `POST /api/runner/llm/{call_id}`. A runner is only handed calls for
  its own owner, and only the runner that took a call can confirm or answer
  it. A call nobody confirms within 5 seconds is offered again to the owner's
  other runners: a runner that died mid-poll leaves a request behind that LBW
  cannot tell is gone. A call or an answer is at most 16 MB. A call that no runner
  takes within 30 seconds fails the job.
- **Runner side.** The runner relays only `GET /v1/models` and
  `POST /v1/chat/completions`, and only to this machine or a private network
  address: 10/8, 172.16/12, 192.168/16, fc00::/7, and 100.64/10 (Tailscale).
  The address is set on the LBW profile, so the runner checks the address it
  actually dials: a host name that resolves to a public address is refused,
  link-local addresses such as a cloud metadata service are never reached, and
  no proxy is used. A public host must be listed in `llm_hosts` in
  `config.json`. `llm_private_network: false` limits the relay to this machine
  and `llm_hosts`. `no_llm_relay: true` turns the relay off. It does not follow
  redirects. A machine without Claude Code still relays.
- **Someone else acts.** A coworker who continues, reworks, or reruns the job
  uses their own server and their own runner. If local models are not on for
  them, or their server lacks the model, they are refused.
- **Usage.** Each call is recorded under the service "Ollama (your machine)"
  (or the server's label) and the model name.

### What is stored

Following AGENTS.md, prompts and logs stay out of the database.

| Where | What |
|---|---|
| `runners` | owner, name, token hash, platform, versions, whether it relays model calls, last seen, a waiting command |
| `tenants.local_models` | `off`, `everyone`, or `chosen` |
| `local_models` | per person: allowed (for `chosen`), on, server kind, address, synced model names |
| memory (`LlmRelay`) | each relayed model call and its answer, while the call runs |
| `runner_links` | a pending link: device-code hash, short code, expiry |
| `runner_jobs` | ids, status, times, a short error. No goal, no output |
| memory (`RunnerHub`) | the goal handed to the runner, its events, and the patch, while the job runs |
| audit log | `runner.approve`, `runner.link`, `runner.revoke`, `runner.update_claude`, `task.runner`, `task.usage`, `local_models.policy`, `local_models.person`, `local_models.server`, `local_models.sync` |
| usage rollups | each run's tokens under service `claude-code (runner)` and the model Claude Code reported |

### Usage

Claude Code's final `result` line reports tokens per model. The runner turns
each into an LBW `llm` event, counting input the way LBW does (prompt tokens
include cache reads and writes, which are also counted on their own). The
existing rollups, dashboard, and audit summary pick them up unchanged.

### The app and its dashboard

`lbw-runner install-app` adds **LBW Runner** where the platform lists apps:
`~/Applications/LBW Runner.app` on macOS, a desktop entry on Linux, and a Start
menu entry on Windows. The app holds its own copy of the runner, which starts at
login (the LaunchAgent or systemd user unit runs that copy; on Windows, the
Startup folder). Opening the app runs `lbw-runner open`: it starts the runner
when it is not running and opens the dashboard, in an app window when Chrome,
Edge, Brave, or Chromium is installed. `lbw-runner uninstall-app` removes the
app and stops the login start. Settings and the token stay.

`lbw-runner tray` is the icon in the menu bar (macOS) or system tray (Windows,
Linux), drawn with `fyne.io/systray`, the runner's one dependency. It is its
own process, so a problem there never touches a running job, and only one runs
per person (`tray.pid`). It reads the dashboard on 127.0.0.1 with the dashboard
key every 3 seconds. Its menu shows the runner's state, **Waiting for you**
with the first five items, **Running** here and in LBW with the first five
jobs, local model calls in flight, and **Open dashboard**, **Pause new work**
(or Resume), **Start runner** when the runner is not running, and **Quit tray
icon**. A waiting or running item opens that change in LBW. On macOS the icon
is a template image that follows the menu bar's colors, its dot fills in when
something waits, and the count sits beside it; it runs inside the app bundle,
so it has no Dock icon. It starts at login from a second LaunchAgent
(`dev.lookbothways.lbw-runner.tray`, desktop sessions only) on macOS, the
autostart folder on Linux, and the Startup folder on Windows. Quitting it keeps
it closed until the next login or until LBW Runner is opened.

The tray asks the dashboard as the tray (`?from=tray`), so the inbox is read
from LBW once a minute while only the tray watches, and every 10 seconds while
the dashboard page is open. With an ETag, most of those reads are an empty 304.

Linux and Windows build without cgo from any machine. macOS needs cgo, because
the menu bar is drawn through AppKit: `make dist` builds both Mac architectures
with cgo on a Mac, and without the icon elsewhere, saying so.

`lbw-runner run` serves the dashboard on `127.0.0.1:7317` (`dashboard_port`
in `config.json`; `no_dashboard: true` or `LBW_RUNNER_DASHBOARD=off` turns it
off). It shows:

- whether the runner is online, paused, not linked, or disconnected, and the
  server, machine name, Claude Code version, relay, and sandbox;
- each running job: its goal, project, step, model, elapsed time, tokens, its
  progress lines, and a link that opens the change in LBW
  (`/?section=changes&project=owner/repo&change=<task id>`);
- the model calls relayed to the person's model server, with the model name,
  time, and result;
- recent jobs and the runner's log.

At the top, **Waiting for you** lists the jobs paused for the owner: a spec
to accept, a plan to review, or a change to approve, in projects they can
write. Each row opens that change in LBW, where the decision is made. The tab
title counts them. **Running now** shows this machine's jobs with their
progress, and under **In LBW** the owner's other queued or running jobs (on
LBW's models, local models, or another of their machines), each with **Stop**.
The runner reads `GET /api/runner/inbox` every 10 seconds only while the
dashboard is open, with an ETag, so an idle runner adds no load and an
unchanged inbox is an empty 304. Each item carries the goal's first line, not
the goal. **Stop** there calls `POST /api/runner/tasks/{id}/stop`, which needs
the same write access as Stop in LBW and is written to the audit log.

From it the person can **Pause new work** (running work goes on, new jobs and
model calls wait in LBW until they resume), **Stop** a running job (LBW marks it
failed with "Stopped on the runner by its owner", and it can be rerun), update
Claude Code, and link the machine when it is not linked. A runner that is not
linked, or that LBW disconnected, keeps the dashboard up, takes no work, and
waits to be linked again.

The dashboard's state is memory only: no prompt, answer, or log is written to
disk or sent to LBW by it. A model call keeps its model name, never its prompt.

### Maintenance

Each heartbeat reports the runner version, the Claude Code version, and whether
Claude Code was found. **Update Claude Code** on the profile queues a command
that the runner runs at its next heartbeat (`claude update`), then reports the
new version.

### Runner updates

A runner release is a GitHub release tagged `runner-vX.Y.Z`, made by
`.github/workflows/runner-release.yml` when such a tag is pushed, or when a
release with such a tag is published on GitHub, which gets the binaries
attached. CI also builds the macOS binaries with cgo on a Mac for every pull
request, so a broken tray build is caught before a release. It holds a
binary per platform, `SHA256SUMS`, and `SHA256SUMS.sig`: an Ed25519 signature
of `SHA256SUMS` made with the release key in the `RUNNER_SIGNING_KEY` secret.
The public half is built into the runner (`internal/update/key.go`), and the
workflow refuses to publish when the signature does not check against it.

A runner built from a release checks GitHub's release list once a day, after a
random first wait of up to an hour, with an ETag so an unchanged list is a 304.
It never installs on its own. The dashboard shows **LBW Runner X is
available** with **Install update** and **What's new**, the tray adds **Install
update X**, `lbw-runner status` says so, and `lbw-runner update` installs it
(`--check` only looks). Installing:

1. refuses while a job runs on this machine or a model call is in flight;
2. downloads `SHA256SUMS`, its signature, and this platform's binary, and
   continues only when the signature is the release key's and the binary
   matches its listed sum;
3. puts the binary in place of the running program (beside it, so it is one
   rename; on Windows the old one is moved aside first);
4. restarts the runner on it in place, so launchd or systemd keeps tracking
   it, and the tray restarts itself when it sees its binary changed.

`releases_url` in `config.json` points at a mirror of the release list, such as
one inside a company network; the signature check is the same. `no_update_check:
true` stops the daily check. A build from source (a version that is not
`X.Y.Z`) does not check or update. To change the release key, run
`go run ./cmd/lbw-runner-sign keygen PATH`, put the printed key in
`internal/update/key.go`, and replace the secret. Runners built before then
trust only the old key, so they are updated by hand once.

### Safety

- Outbound HTTPS only to LBW. The one port the runner opens is its dashboard,
  on 127.0.0.1. A random key in the runner folder, readable only by its owner,
  opens it: `lbw-runner open` passes the key once and the page keeps it as an
  HttpOnly, SameSite=Strict cookie. The dashboard answers only requests that
  name 127.0.0.1 or localhost (so a rebound DNS name cannot reach it), takes
  changes only as JSON from its own page, and cannot be framed. `open` checks
  the runner's health with an HMAC of a fresh nonce before it sends the key, so
  another program on the port never sees it.
- The token is scoped to one person and can be revoked from the profile. A
  revoked runner takes no more work, and its waiting jobs are cancelled. With
  the dashboard on, it waits to be linked again; without it, it exits.
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

Runner (`runner/`, Go 1.24, the standard library plus `fyne.io/systray`):

- `lbw-runner install-app | open | tray | login | run | status | doctor | update-claude | install-service | uninstall-app | logout`.
- `internal/monitor` keeps the dashboard's state, `internal/dashboard` serves
  it, `internal/app` installs the app and draws its icons, and `internal/tray`
  is the menu bar or tray icon.
- `make runner`, `make runner-test`, `make runner-dist` (macOS, Linux, Windows),
  `make runner-image`.
- Tests use a fake `claude` script and a fake LBW server.

Known limits of the prototype, each tracked as an issue:

- The hub is in memory: one LBW process, and a restart fails running runner jobs.
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
runner/bin/lbw-runner run          # or: runner/bin/lbw-runner install-app
```

`install-app` copies the runner into the app, starts it at login, and opens
the dashboard. A machine can also be linked from the dashboard instead of
`login`.

Then add a Claude endpoint with the sign-in **Each person's machine** under
Organization, Models, add a model from it, and start a job on that model. To use a checkout you already
have instead of a fresh clone, add it to `~/Library/Application Support/lbw-runner/config.json`
(macOS) or `~/.config/lbw-runner/config.json` (Linux):

```json
{ "repos": { "hhollenstain/lbw": "/Users/you/projects/lbw" } }
```
