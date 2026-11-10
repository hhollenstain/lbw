from __future__ import annotations

import re
import subprocess

from agent_lbw.sandbox import Workspace
from agent_lbw.tools.base import ToolResult, ToolSpec, object_schema

_PUBLISH_COMMAND = re.compile(r"\bgit\s+push\b|\bgh\s+pr\s+create\b", re.IGNORECASE)
_COMMIT_COMMAND = re.compile(r"\bgit\s+commit\b", re.IGNORECASE)
_PROTECTED_PUSH = re.compile(
    r"\bgit\s+push\b.*(\bmain\b|\bmaster\b|HEAD:main|HEAD:master)",
    re.IGNORECASE,
)


def shell_tools(
    workspace: Workspace,
    default_timeout: int,
    *,
    allow_publish: bool = False,
) -> list[ToolSpec]:
    return [
        ToolSpec(
            name="run_command",
            description=(
                "Run a shell command in the workspace. Use this for language tooling, "
                "formatters, and project-specific CLIs. Prefer run_tests for the test suite."
            ),
            parameters=object_schema(
                {
                    "command": {
                        "type": "string",
                        "description": "Shell command to run with bash -lc.",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Optional timeout. Defaults to the agent command timeout.",
                    },
                },
                ["command"],
            ),
            handler=lambda command, timeout_seconds=None: run_command(
                workspace,
                command,
                int(timeout_seconds) if timeout_seconds is not None else default_timeout,
                allow_publish=allow_publish,
            ),
        )
    ]


def run_command(
    workspace: Workspace,
    command: str,
    timeout_seconds: int,
    *,
    allow_publish: bool = False,
) -> ToolResult:
    if not command or not command.strip():
        return ToolResult(False, "command is required")
    if _PROTECTED_PUSH.search(command):
        return ToolResult(False, "refusing to push directly to main/master")
    if _COMMIT_COMMAND.search(command):
        return ToolResult(
            False,
            "refusing git commit; the cycle commits once after review",
        )
    if not allow_publish and _PUBLISH_COMMAND.search(command):
        return ToolResult(
            False,
            "publish is disabled for this run; refusing git push / gh pr create",
        )
    try:
        result = subprocess.run(
            ["/bin/bash", "-lc", command],
            cwd=workspace.root,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(False, f"timed out after {timeout_seconds}s: {command}")
    except OSError as exc:
        return ToolResult(False, f"failed to start command: {exc}")

    output = _combine(result.stdout, result.stderr)
    if result.returncode != 0:
        return ToolResult(False, f"exit {result.returncode}\n{output}".strip())
    return ToolResult(True, output or "(no output)")


def _combine(stdout: str, stderr: str) -> str:
    parts = [part.strip() for part in (stdout, stderr) if part and part.strip()]
    return "\n".join(parts)
