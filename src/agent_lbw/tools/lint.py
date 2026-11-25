from __future__ import annotations

import hashlib
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from agent_lbw.progress import record_lint_run
from agent_lbw.runtime.project import configured_value, load_project
from agent_lbw.runtime.testbed import (
    ruff_unavailable,
    run_lint_in_testbed,
    run_with_throwaway_ruff,
    run_with_throwaway_toolchains,
    toolchain_missing,
)
from agent_lbw.sandbox import Workspace
from agent_lbw.tools.base import ToolResult, ToolSpec, object_schema
from agent_lbw.tools.codechange import code_fingerprint, reused_check_result
from agent_lbw.tools.shell import run_command

_RUFF_ARROW_PATH = re.compile(r"-->\s+([^:\s]+):\d+")
_COLON_PATH = re.compile(r"^([^:\s]+?\.\w+):\d+(?::\d+)?", re.MULTILINE)


@dataclass
class _CachedRun:
    fingerprint: str
    result: ToolResult


_CACHE: dict[str, _CachedRun] = {}


def lint_targets_changed_files(output: str, changed_files: list[str]) -> bool:
    """True when lint output is about this cycle's files, or cannot be attributed."""
    mentioned = _lint_mentioned_paths(output)
    if not mentioned:
        return True
    changed = {_normalize_lint_path(path) for path in changed_files if path}
    if not changed:
        return False
    return any(_lint_path_matches(path, changed) for path in mentioned)


def _lint_mentioned_paths(output: str) -> set[str]:
    found: set[str] = set()
    for match in _RUFF_ARROW_PATH.finditer(output or ""):
        found.add(_normalize_lint_path(match.group(1)))
    for match in _COLON_PATH.finditer(output or ""):
        found.add(_normalize_lint_path(match.group(1)))
    found.discard("")
    return found


def _lint_path_matches(mentioned: str, changed: set[str]) -> bool:
    if mentioned in changed:
        return True
    return any(mentioned.endswith(f"/{path}") or path.endswith(f"/{mentioned}") for path in changed)


def _normalize_lint_path(path: str) -> str:
    posix = Path(str(path).strip().strip("\"'`")).as_posix()
    while posix.startswith("./"):
        posix = posix[2:]
    return posix


def apply_ruff_autofix(
    workspace: Workspace,
    lint_command: str | None,
    timeout_seconds: int,
) -> bool:
    """Run `ruff check --fix` when the project lint command uses ruff check."""
    if not lint_command or "ruff check" not in lint_command:
        return False
    if "ruff check --fix" in lint_command:
        fix_command = lint_command
    else:
        fix_command = lint_command.replace("ruff check", "ruff check --fix")
    started = time.perf_counter()
    command = fix_command
    if shutil.which("ruff") is None:
        prepared = run_with_throwaway_ruff(workspace, fix_command, timeout_seconds)
        if prepared is not None:
            elapsed_ms = int(round((time.perf_counter() - started) * 1000))
            record_lint_run(
                command=fix_command,
                ok=prepared.ok,
                output=prepared.output,
                phase="autofix",
                elapsed_ms=elapsed_ms,
            )
            return True
    result = run_command(workspace, command, timeout_seconds)
    record_lint_run(
        command=fix_command,
        ok=result.ok,
        output=result.output,
        phase="autofix",
        elapsed_ms=int(round((time.perf_counter() - started) * 1000)),
    )
    return True


def lint_tools(
    workspace: Workspace,
    lint_command: str | None,
    timeout_seconds: int,
) -> list[ToolSpec]:
    return [
        ToolSpec(
            name="run_lint",
            description=(
                "Run the project's configured linter (for example ruff format "
                "then ruff check). Call this once after code changes, including "
                "changes in another project in this workspace. If no code file "
                "has changed since the last run, returns that result without "
                "running the linter again. Do not call it again until you edit "
                "code. If ruff is not installed, this builds a throwaway "
                "environment outside the project and runs it there. If no "
                "command is configured, look up the official linter and call "
                "configure_project, then call this again."
            ),
            parameters=object_schema({}),
            handler=lambda: run_project_lint(
                workspace, lint_command, timeout_seconds, phase="agent"
            ),
        )
    ]


def run_project_lint(
    workspace: Workspace,
    lint_command: str | None,
    timeout_seconds: int,
    *,
    phase: str = "lint",
) -> ToolResult:
    command = _active_lint_command(workspace, lint_command)
    if not command:
        result = ToolResult(
            False,
            "no lint command configured for this project. "
            "Look up the official linter, then call configure_project with "
            "lint_command and run_lint again.",
        )
        record_lint_run(command="", ok=False, output=result.output, phase=phase)
        return result
    fingerprint = _tree_fingerprint(workspace, command)
    cached = _CACHE.get(str(workspace.root))
    if cached is not None and cached.fingerprint == fingerprint:
        record_lint_run(
            command=command,
            ok=cached.result.ok,
            output=cached.result.output,
            phase=phase,
            elapsed_ms=0,
            reused=True,
        )
        return reused_check_result("run_lint", cached.result)
    started = time.perf_counter()
    result = run_command(workspace, command, timeout_seconds)
    if not result.ok and toolchain_missing(result.output or "", workspace.root):
        prepared = run_with_throwaway_toolchains(
            workspace, command, result.output or "", timeout_seconds
        )
        if prepared is not None:
            result = prepared
    if not result.ok and ruff_unavailable(result.output or ""):
        prepared = run_lint_in_testbed(workspace, command, result.output or "", timeout_seconds)
        if prepared is not None:
            result = prepared
        else:
            note = (
                "Ruff is not installed. A throwaway environment could not be "
                "built outside the project. This is not a lint finding. Do not "
                "change product code to hide it."
            )
            body = (result.output or "").strip()
            result = ToolResult(False, f"{body}\n{note}".strip() if body else note)
    elapsed_ms = int(round((time.perf_counter() - started) * 1000))
    record_lint_run(
        command=command,
        ok=result.ok,
        output=result.output,
        phase=phase,
        elapsed_ms=elapsed_ms,
    )
    _CACHE[str(workspace.root)] = _CachedRun(fingerprint=fingerprint, result=result)
    return result


def current_lint_result(
    workspace: Workspace,
    lint_command: str | None,
) -> ToolResult | None:
    """The last lint result when it already covers the current code. Does not run lint."""
    command = _active_lint_command(workspace, lint_command)
    if not command:
        return None
    cached = _CACHE.get(str(workspace.root))
    if cached is None or cached.fingerprint != _tree_fingerprint(workspace, command):
        return None
    return cached.result


def _active_lint_command(workspace: Workspace, lint_command: str | None) -> str | None:
    present, value = configured_value(workspace.root, "lint_command")
    if present:
        return value
    if lint_command:
        return lint_command
    return load_project(workspace.root).lint_command


def _tree_fingerprint(workspace: Workspace, lint_command: str) -> str:
    digest = hashlib.sha256()
    digest.update(lint_command.encode("utf-8"))
    digest.update(b"\0")
    digest.update(code_fingerprint(workspace.root).encode("utf-8"))
    return digest.hexdigest()
