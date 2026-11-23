# lbw-runner

Runs your LBW jobs on your own machine with your own Claude Code. The design,
the protocol, and the plan are in [docs/runner.md](../docs/runner.md).

## Build

Go 1.24 or later, no other dependencies. From the repository root,
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
lbw-runner login --server https://lbw.example.com   # then enter the code on your profile
lbw-runner doctor                                    # checks git, Claude Code, and the server
lbw-runner run                                       # or: lbw-runner install-service
```

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
