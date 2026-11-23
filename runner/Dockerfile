# The runner for servers and CI. On a laptop, use the native binary instead:
# a container cannot reach the Claude Code login kept in your OS keychain, so
# here Claude Code bills the ANTHROPIC_API_KEY you pass in.
FROM golang:1.24-bookworm AS build
ARG VERSION=dev
WORKDIR /src
COPY go.mod ./
COPY cmd ./cmd
COPY internal ./internal
RUN CGO_ENABLED=0 go build -trimpath -ldflags "-s -w -X main.version=${VERSION}" \
    -o /out/lbw-runner ./cmd/lbw-runner

FROM node:22-bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force \
    && useradd --create-home runner
COPY --from=build /out/lbw-runner /usr/local/bin/lbw-runner
USER runner
ENV LBW_RUNNER_HOME=/home/runner/.lbw-runner
# The container is the boundary: Claude Code's sandbox needs namespaces Docker
# does not give by default, so jobs here run without it.
ENV LBW_RUNNER_SANDBOX=off
VOLUME ["/home/runner/.lbw-runner"]
ENTRYPOINT ["lbw-runner"]
CMD ["run"]
