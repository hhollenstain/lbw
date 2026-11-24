# Multi-arch agent image (linux/amd64 + linux/arm64).
# The agent itself is CPU-bound. GPUs belong to the model server, not this image.
FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends git gh ca-certificates bash make \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /opt/agent-lbw

COPY pyproject.toml README.md ./
COPY libs/lbw_shared ./libs/lbw_shared
COPY src ./src

RUN pip install --no-cache-dir ./libs/lbw_shared . \
    && useradd --create-home --uid 10001 lbw \
    && mkdir -p /workspaces \
    && chown -R lbw:lbw /opt/agent-lbw /workspaces

USER lbw
WORKDIR /workspaces

ENTRYPOINT ["lbw"]
CMD ["doctor"]
