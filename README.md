# LBW

Look both ways. LBW sits on a project, makes a change, runs that project's tests locally, and only then commits (and optionally publishes) the result. Create-PR also requires a separate review that the diff actually fulfills the stated goal — green tests alone are not enough.

## Install / Quick start

```bash
# Install from source
pip install .
# or with uv
uv sync --extra dev
```

CLI entry point:
```bash
lbw --help
```

Test / lint:
```bash
pytest -q
ruff format src tests
ruff check src tests
```

See the bundled demo:
- `examples/demo-project/`
- Skill conventions: `skills/agent-lbw/SKILL.md`

The agent process is the same on an Apple Silicon MacBook and on a Linux box with an NVIDIA GPU. Inference is a separate OpenAI-compatible server:

| Machine | Model server | Why |
| --- | --- | --- |
| MacBook (Apple Silicon) | [Ollama](https://ollama.com) on the host | Docker Desktop cannot pass Metal into a Linux VM |
| Home lab (NVIDIA, including RTX 5090) | Ollama or vLLM in Compose with GPU passthrough | Blackwell needs host driver **570+** and `nvidia-container-toolkit` |

A change moves through roles you can turn on, turn off, or add:

```
  ask → architect plans → you triage → builder
      → reviewers (QA, and any role you enable)
      → you approve → commit / pull request
```

The web UI waits before any code and again before the pull request. Changes
in the left menu groups every project into waiting for you, in progress, and
done, and shows the step each change is on. Uncheck one gate
to skip that review, or bypass all gates. Keep improving, `lbw run`, and
`lbw watch` pass the gates themselves. Builder and QA stay on. Add a review
role at `.lbw/roles/<name>/ROLE.md` (`job: review`) and enable it from the
Roles panel or `roles.enabled` in `.lbw/config.yaml`. `lbw roles` lists
the catalog.

```
  lbw run / watch          lbw ui
        │                       │
        ├─ architect plans      ├─ architect plans, then waits
        ├─ builder writes       ├─ you choose Build or Hand off
        ├─ reviewers judge      ├─ builder, then each review role
        ├─ tests and goal check ├─ you approve from the verdicts
        └─ commit / pull request└─ then commit / pull request
              │
              ▼
     OpenAI-compatible API
     (Ollama Metal · Ollama CUDA · vLLM · cloud)
```

## Prerequisites

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- `git`
- A model server. On a Mac, install Ollama and pull a coding model:

```bash
brew install ollama
ollama serve
ollama pull qwen2.5-coder:14b
```

Docker is optional. This repo's own tests do not need a model or a GPU.

## Local MacBook

```bash
cd lbw
cp .env.example .env
uv sync --extra dev
uv run pytest -q
uv run lbw doctor
```

`doctor` tells you whether you are on Apple Silicon or NVIDIA, which backend to use, and whether the model endpoint is reachable. `LBW_MODEL_NAME` must match a name from `ollama list` (on this Mac that is `Qwen2.5-Coder:14b`, not the library alias).

Point the agent at the bundled demo (it has a failing test on purpose):

```bash
cd examples/demo-project
git init
git add -A
git commit -m "Initial demo project"
cd ../..

uv run lbw init examples/demo-project
uv run lbw run --workspace examples/demo-project --goal "Make the test suite pass."
```

Continuous mode, using `.lbw/goals.md` plus failing tests as the backlog:

```bash
uv run lbw watch --workspace /path/to/your/project
```

Attach any local git checkout. The agent will not read or write outside that workspace.

Local models often cannot hold the context the server advertises. Set `LBW_CONTEXT_WINDOW` (or `--context-window`, or the Context window field in the web UI) to the number of tokens this machine can run, for example `8192`. LBW fits each prompt to that window and, on Ollama, sends it as `num_ctx`. Leave it unset to use the window the server reports.

The web UI (`lbw ui`) lets you queue cycles, choose a model turned on for the project, and switch workspaces. Browse local folders, create a new LBW project, or clone a git repo onto this machine so you can run and open PRs locally. Remembered folders live in `.lbw/workspaces.json`. Archive a tab to hide it from the session, or remove it from the list; neither deletes the folder on disk. Add a host and its models in organization settings. The proposal screen lists the models turned on for that project. `--base-url` on `run`, `watch`, and `ui` still selects a host from the CLI.

## Onboard a project

```bash
uv run lbw init /path/to/your/project
```

That writes:

```text
.lbw/config.yaml   # test command, publish settings
.lbw/goals.md      # checkbox backlog the watcher consumes
.lbw/runs/         # JSON logs written after every cycle
.lbw/servers.json  # hosts remembered when a task names a URL
.lbw/workspaces.json  # remembered folders for the workspace picker
.lbw/lbw.db       # organization and people (SQLite)
```

If `test_command` is omitted, LBW infers one (`pytest`, `npm test`, `make test`, `cargo test`, `go test`).

New workspaces enable the bundled process skills: debug, explore, implement,
research, review, security, tdd, ui, and kiwi. Turn them on or off in the Skills
panel. They are the agent's process. The kiwi skill is the one that tells the
agent how to use a KiwiFS server.

Publish/create-PR is off until you turn it on in `.lbw/config.yaml` or pass `--create-pr`. That option opens a feature branch and pull request; it never pushes to `main`. Commits still require a green test run when `LBW_REQUIRE_TESTS=true`.

## Containerized

The agent image is multi-arch (`linux/arm64` and `linux/amd64`) and does **not** need a GPU. Mount a host directory at `/workspaces` and talk to a model server.

`LBW_WORKSPACE` (default `./workspaces`) is the only project volume. Cloned repos and each project's `.lbw/` (config, goals, run history) persist on the host because they live *inside* that folder. Do not bind-mount this app's `.lbw` over `/workspaces/.lbw`; that hides the mounted project's own config.

`docker compose up` starts the organization site and the engineer dashboard behind nginx. `make certs` installs `certutil` when it is missing, signs a local certificate, trusts it on this machine, and loads it into the ingress container. The organization site is [https://www-local.lookbothways.dev](https://www-local.lookbothways.dev). The engineer dashboard is [https://crossing-local.lookbothways.dev](https://crossing-local.lookbothways.dev). `make restart-local` rebuilds both services. `make restart-agent` rebuilds the organization site. `make restart-crossing` rebuilds the dashboard. One-off commands replace the UI with `run`, `clone`, `init`, `watch`, or `doctor`.

With no `LBW_DATABASE_URL`, the organization and its people are stored in SQLite at `.lbw/lbw.db` on the install. `make up-local` rebuilds both images when the files they copy have changed, then starts Postgres, KiwiFS, both services, and nginx. The organization site is [https://www-local.lookbothways.dev](https://www-local.lookbothways.dev). Postgres and KiwiFS stay up after the command returns. `make wipe` stops the local stack and the NVIDIA stack and deletes their Docker volumes.

To run the agent container against the same database:

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up
```

Postgres listens on `127.0.0.1:5432` (`lbw` / `lbw`, database `lbw`). Its files stay in the `lbw-postgres` Docker volume, so the organization and people survive a restart. `make wipe` deletes `lbw-postgres`, `lbw-kiwifs`, and the Ollama volume. The agent container receives `LBW_DATABASE_URL` on the compose network. Project files, run logs, and remembered folders stay on disk.

`make export-github` writes the GitHub App to gitignored `github.local.yaml`. After a wipe, `make up-local` and then `make hydrate` seal that file back into Postgres. The private key stays out of git and out of `.lbw/`. Details are in [docs/github-app.md](docs/github-app.md).

### Clone a repo into the container

`git` is in the image; the entrypoint is `lbw`, so clone with `lbw clone` (or clone on the host into `$LBW_WORKSPACE`). The destination must not exist yet — clone first, then `init` only if you still need scaffolding.

```bash
mkdir -p workspaces
export LBW_WORKSPACE="$(pwd)/workspaces"
docker compose run --rm agent clone git@github.com:user/repo.git
docker compose run --rm agent init /workspaces/repo
docker compose run --rm agent run -w /workspaces/repo --goal "Add a README.md"
```

Public GitHub repositories can be added by URL with no credentials. Push, pull requests, and merge use one GitHub App. An LBW engineer saves its key on the engineer dashboard ([docs/github-app.md](docs/github-app.md)). Locally that host is [https://crossing-local.lookbothways.dev](https://crossing-local.lookbothways.dev). The organization site is [https://www-local.lookbothways.dev](https://www-local.lookbothways.dev). Both names resolve to `127.0.0.1`. Each organization, on its own site, installs the app on the GitHub account that owns that organization's repositories ([docs/github-organization.md](docs/github-organization.md)). An installation token is minted when a push, pull request, or merge needs it, and it is not stored. The first visit asks for the admin account: a name, email, and password. After that, sign in with that email and password. Each person has a name and an email. They connect GitHub from the GitHub tab on Personal once the organization has a GitHub App. Personal rules are listed by name. The plus button opens a dialog to add one, and a listed rule can be viewed as markdown, edited, or deleted. LBW sends them to GitHub and stores the login GitHub returns. The organization lists that username and the teams they are on. On a team, Edit selects several people to change their rank or remove them. The add button opens a search and adds several people at once. Remove team is available once nobody is left on it. The sign-in page can create another organization. LBW engineers open `/crossingguard`, which lists every organization. A menu there adds another LBW engineer. The page can unlock a person and save Entra, Auth0, or SAML sign-in. An organization admin does not open that page. It lists a job only when LBW failed, and it does not show code or a pull request. A later sign-in from Entra, Auth0, or another SSO or SAML provider matches the same person by email. The sign-in token is not stored. Sync then copies their organization role, teams, and repository access. Add a repository the app can see to the organization, a person, or a team.

Models stay outside LBW. Organization settings are split into People, Rules, Skills, Roles, Models, and GitHub. On Models, add an OpenAI, Anthropic, Google, Ollama, vLLM, LM Studio, or other OpenAI-compatible endpoint and check it. LBW stores the model names that endpoint returns, and Refresh replaces that list. Choose one of those names, then grant it to a project, a team, or a person. Rank does not include it. An agent is a worker LBW can hand the change to: LBW's own builder, Claude Code, Codex, or Cursor. LBW still orchestrates the run. A person has to accept work an agent built before that task can succeed. The API key is sealed and is not shown again.

Private GitHub repos can also use the SSH mount in `docker-compose.yml`, or be cloned on the host:

```bash
git clone git@github.com:user/repo.git "$LBW_WORKSPACE/repo"
```

### Mac + Docker Desktop

Keep Ollama on the host (Metal), then:

```bash
export LBW_WORKSPACE="$(pwd)/workspaces"
docker compose -f docker-compose.yml -f docker-compose.mac.yml run --rm agent doctor
docker compose -f docker-compose.yml -f docker-compose.mac.yml up --build
# UI: https://www-local.lookbothways.dev
docker compose -f docker-compose.yml -f docker-compose.mac.yml run --rm agent \
  run --workspace /workspaces --goal "Make the test suite pass."
```

### NVIDIA home lab (5090 and similar)

On the Linux host:

1. NVIDIA driver **570 or newer** (Blackwell / RTX 50-series reports compute capability 12.0).
2. [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
3. `sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker`.

Then:

```bash
export LBW_WORKSPACE="$(pwd)/workspaces"
export LBW_MODEL_NAME=qwen2.5-coder:32b
docker compose -f docker-compose.yml -f docker-compose.nvidia.yml up --build -d ollama
docker compose -f docker-compose.yml -f docker-compose.nvidia.yml exec ollama ollama pull qwen2.5-coder:32b
docker compose -f docker-compose.yml -f docker-compose.nvidia.yml run --rm agent doctor
docker compose -f docker-compose.yml -f docker-compose.nvidia.yml run --rm agent \
  clone git@github.com:user/repo.git
docker compose -f docker-compose.yml -f docker-compose.nvidia.yml run --rm agent \
  watch --workspace /workspaces/repo
```

A 32B coder model is a reasonable default on a 32 GB 5090. Swap `LBW_MODEL_NAME` if you prefer vLLM or a larger quant. Any server that speaks `/v1/chat/completions` works; set `LBW_MODEL_BASE_URL` accordingly.

## KiwiFS

LBW keeps project memory in [KiwiFS](https://github.com/kiwifs/kiwifs), a markdown knowledge base with search. A local model cannot hold the repository and a pile of notes in one prompt. The agent asks KiwiFS for a short brief, then writes the conclusion back so the next cycle does not rediscover it.

The default server is the one already running on this machine:

```bash
curl -sS http://127.0.0.1:3333/api/kiwi/context
uv run lbw doctor
```

`doctor` reports whether that server answers. `LBW_KIWIFS_URL` overrides the address. Set it empty to turn the tools off. If the server requires a key, set `LBW_KIWIFS_API_KEY` (sent as `Authorization: Bearer …`).

### One base, one folder per project

Reads search the whole base, including guides that are already there. Writes stay under `projects/<name>/`, so a cycle cannot overwrite `guides/` or `getting-started/`. For a git repo, `<name>` is the origin repository (`lbw` from `github.com/you/lbw.git`), including when LBW is pointed at a subdirectory. A folder that is not a git repo uses that folder's name. A repo with no remote uses the git root folder's name. Set `kiwifs_prefix` in `.lbw/config.yaml` to use a different folder. `LBW_KIWIFS_PREFIX` does the same when the project config does not. An explicit empty `kiwifs_prefix:` removes the fence and lets writes land anywhere in the base.

New projects enable the bundled `kiwi` skill. On a project that already has `.lbw/config.yaml`, add `kiwi` under `skills.enabled`.

### How a small model should use it

The skill says this to the agent. The short version:

1. `kiwi_brief` the question and leave the budget alone. That is a keyword search plus a peek of the top pages, not the pages themselves.
2. `kiwi_peek` one path, or `kiwi_read` with a `heading`, when the brief is not the answer. Do not read several full pages.
3. When the next run would otherwise rediscover a fact (the test command, a rejected approach, which file owns the behavior), `kiwi_append` a few lines to `episodes/<date>.md`, or `kiwi_write` `decisions.md`. A note is a few sentences. Do not store diffs, logs, or source files there.

The default brief is 800 tokens. The tool will not return more than 2000.

| Model | Context left for notes | `budget_tokens` |
| --- | --- | --- |
| 7B, about 8k | tight | 400–600 |
| 14B, 16k–32k | the default | 800 |
| 32B and larger | room to spare | 1200–2000 |

Keyword search does not need a second model. Leave vector search off while the coder model is loaded. An embedding model in Ollama uses the same memory the coder needs, and BM25 on this server is enough to find a page.

### Docker

`docker compose up` starts KiwiFS next to the agent and points the agent at `http://kiwifs:3333`. `make up-local` publishes KiwiFS on `127.0.0.1:3333` and runs the agent in that stack. `make restart-local` leaves KiwiFS running. Rule text for an organization, team, project, or person is stored in its own KiwiFS space. The pages stay in the `lbw-kiwifs` volume.

Set `LBW_KIWIFS_URL` when KiwiFS is already running somewhere else. Set it empty to turn the tools off.

## Safety

- All file and shell tools are rooted in `--workspace`. Path escape is rejected.
- `web_search` and `fetch_url` only reach public http(s) URLs. Private,
  loopback, and link-local addresses are rejected.
- Likely secrets (`.env`, keys, `credentials.json`) cannot be committed.
- Force-push and `--no-verify` are not available.
- Auto-commit is skipped when tests fail and `LBW_REQUIRE_TESTS` is on.
- Create-PR commits the project change, pushes that branch, and only then opens the pull request. It does not open a pull request from a branch that was not pushed, and it never pushes to `main`/`master`.

This is still a coding agent with a shell inside a trusted workspace. Do not point it at a tree you would not edit yourself.

## CLI

| Command | Purpose |
| --- | --- |
| `lbw doctor` | Hardware, git/docker, model health, KiwiFS |
| `lbw init [path]` | Write `.lbw/` scaffolding (path must already exist) |
| `lbw clone URL [DIR]` | `git clone` into `DIR` or `./<repo>` and write `.lbw/` |
| `lbw run -w PATH -g "..." -m MODEL --base-url URL` | One improve → test → commit cycle |
| `lbw ui -w PATH --base-url URL` | Local web UI to queue and run tasks (`--web-ui` on `run` also works) |
| `lbw watch -w PATH` | Repeat cycles on an interval |

Environment variables are listed in `.env.example`.