# lbw-runner

Runs your LBW jobs on your own machine with your own Claude Code. The design,
the protocol, and the plan are in [docs/runner.md](../docs/runner.md).

## Build

Go 1.24 or later. The one dependency is `fyne.io/systray` (Apache-2.0) for the
menu bar or tray icon. Linux and Windows build without cgo from any machine;
macOS builds with cgo, so build macOS binaries on a Mac (`make dist` does it for
both Mac architectures there, and says so when it cannot). From the repository root,
`make runner` builds `runner/bin/lbw-runner` for local testing (and
`runner-test`, `runner-dist`, `runner-image` run the targets below). Build
output in `runner/bin/` and `runner/dist/` is gitignored.

```sh
make build      # bin/lbw-runner for this machine
make test       # go vet and go test
make dist       # dist/ for macOS, Linux, and Windows, with SHA256SUMS
make image      # the Docker image, for servers that use an API key
```

## Use

```sh
lbw-runner install-app                               # adds the LBW Runner app and starts it at login
```

Open **LBW Runner** like any other app. Its dashboard starts with **Waiting for
you**: the plans, specs, and changes waiting on your review or approval, each
opening that change in LBW. It shows the jobs running on this machine with
their progress, your other jobs running in LBW with Stop on each, the model
calls relayed to your model server, recent jobs, and the runner's log. From it
you can link this machine, pause new work, stop any of your jobs, and update
Claude Code. On macOS the app is
`~/Applications/LBW Runner.app`; on Linux it is a desktop entry; on Windows it is
in the Start menu. `lbw-runner uninstall-app` removes it.

The app also puts an icon in the menu bar (macOS) or system tray (Windows,
Linux). Its menu shows what is waiting for you and what is running, each opening
that change in LBW, how many local model calls are in flight, and Open dashboard
and Pause new work. On macOS the count of waiting items sits beside the icon,
and the dot fills in when something waits. The icon starts at login; Quit tray
icon hides it until you open LBW Runner again. On Linux the tray needs a desktop
that shows StatusNotifier icons, which GNOME does through the AppIndicator
extension.

Without the app:

```sh
lbw-runner login --server https://lbw.example.com   # then enter the code on your profile
lbw-runner doctor                                    # checks git, Claude Code, and the server
lbw-runner run                                       # or: lbw-runner install-service
lbw-runner open                                      # the dashboard, at http://127.0.0.1:7317
```

When a newer LBW Runner is published, the dashboard and the tray say so, with
**Install update**. Nothing installs until you choose it, and never while a job
runs. From a terminal:

```sh
lbw-runner update --check    # is there a newer release?
lbw-runner update            # install it, and restart the runner and tray on it
```

Only a release signed by LBW's release key is installed. Set `releases_url` to
use a mirror of the GitHub release list, or `no_update_check: true` to stop the
daily check. A build from source does not update itself.

To publish a release, push a tag such as `runner-v0.3.0`. The release workflow
builds every platform, and signs `SHA256SUMS` with the `RUNNER_SIGNING_KEY`
secret.

The dashboard listens on 127.0.0.1 only and opens with a key kept in the runner
folder. Set `dashboard_port` to move it, or `no_dashboard: true` (or
`LBW_RUNNER_DASHBOARD=off`) to turn it off, as the Docker image should.

Settings live in `config.json` in the runner folder (`LBW_RUNNER_HOME`, or
`lbw-runner` in your user config folder): `repos` maps `owner/repo` to a
checkout you already have, `allowed_tools` widens what Claude Code may run,
and `ca_file` trusts a local certificate authority.

Jobs run Claude Code's shell commands in its sandbox: any command is approved
there, writes stay in the job's worktree, and the network reaches only
`sandbox_domains` (by default the common package registries and GitHub). A
build may also run the project's own setup, test, and lint commands. On Linux
the sandbox needs `bubblewrap` and `socat`. Set `"sandbox": false`, or
`LBW_RUNNER_SANDBOX=off`, to run without it.

The runner also relays model calls to a model server on this machine, such as
Ollama, when you choose one of your local models on the job form. You set the
server's address on your LBW profile. The runner relays to this machine and to
private network addresses, such as a GPU box at `192.168.1.20` or on
Tailscale. A server on the public internet must be listed in `llm_hosts`. Set
`llm_private_network: false` to relay only to this machine and `llm_hosts`, or
`no_llm_relay: true` to turn the relay off.
Without Claude Code installed, the runner only relays.

## Docker

```sh
docker run -d --name lbw-runner \
  -e LBW_SERVER=https://lbw.example.com \
  -e LBW_RUNNER_TOKEN=lbwr_… \
  -e ANTHROPIC_API_KEY=sk-ant-… \
  -v lbw-runner:/home/runner/.lbw-runner \
  lbw-runner:dev
```

Get the token by running `lbw-runner login` once with `LBW_RUNNER_HOME` pointing
at a folder you can read, or link it from a laptop and copy the token from the
token file. In a container Claude Code bills the API key, because the Claude
Code login on your computer is kept in its OS keychain.
