from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass

from agent_lbw.progress import record_test_run
from agent_lbw.runtime.project import configured_value, load_project
from agent_lbw.runtime.testbed import (
    run_in_testbed,
    run_with_throwaway_toolchains,
    toolchain_missing,
)
from agent_lbw.sandbox import Workspace
from agent_lbw.tools.base import ToolResult, ToolSpec, object_schema
from agent_lbw.tools.codechange import code_fingerprint, reused_check_result
from agent_lbw.tools.shell import run_command


@dataclass
class _CachedRun:
    fingerprint: str
    result: ToolResult


_CACHE: dict[str, _CachedRun] = {}
_VACUOUS_TEST_RE = re.compile(
    r"would be executed|without actually running|tests were not run",
    re.IGNORECASE,
)
# A suite that printed results started. A missing binary never gets this far.
_SUITE_STARTED = re.compile(
    r"(?im)(?:"
    r"test session starts"
    r"|collected \d+ items?"
    r"|Ran \d+ tests?"
    r"|short test summary"
    r"|\d+ passed"
    r"|\d+ failed"
    r"|FAILED "
    r"|--- FAIL:"
    r")"
)
_RUNNER_MISSING = re.compile(
    r"(?im)(?:"
    r"No module named ['\"]?(?:pytest|nose|tox|nose2)['\"]?"
    r"|(?:pytest|py\.test|jest|vitest|mocha|phpunit|rspec|cargo|gradle|mvn|go|bundle|composer"
    r"|make|just|npm|npx|node|yarn|pnpm|bun|deno|uv|tox|poetry|python3?)"
    r"[^\n]{0,60}(?:not found|command not found)"
    r"|make: \*\*\* No rule to make target\b[^\n]*\b(?:test|check)\b"
    r"|npm ERR! Missing script"
    r"|Missing script: ['\"]?(?:test|tests)"
    r"|error: no test specified"
    r"|Cannot find module ['\"](?:jest|vitest|mocha|pytest)"
    r")"
)


def runner_unavailable(output: str) -> bool:
    """The test command never started a runner. That is not a failed test."""
    text = output or ""
    if toolchain_missing(text):
        # A later step of the command, such as npm after pytest, could not start.
        return True
    if _SUITE_STARTED.search(text):
        return False
    return _RUNNER_MISSING.search(text) is not None


def test_tools(
    workspace: Workspace,
    test_command: str | None,
    timeout_seconds: int,
) -> list[ToolSpec]:
    return [
        ToolSpec(
            name="run_tests",
            description=(
                "Run the project's configured test command. "
                "Call it once after code changes, including changes in another "
                "project in this workspace. If no code file has changed since "
                "the last run, this returns that result and does not run the "
                "suite again. Do not call it again until you edit code. "
                "If no command is configured, look up the official runner and call "
                "configure_project, then call this again."
            ),
            parameters=object_schema({}),
            handler=lambda: run_project_tests(
                workspace, test_command, timeout_seconds, phase="agent"
            ),
        )
    ]


def run_project_tests(
    workspace: Workspace,
    test_command: str | None,
    timeout_seconds: int,
    *,
    phase: str = "tests",
) -> ToolResult:
    command = _active_test_command(workspace, test_command)
    if not command:
        result = ToolResult(
            False,
            "no test command configured for this project. "
            "Look up the official test runner, then call configure_project "
            "with test_command and run_tests again.",
        )
        record_test_run(command="", ok=False, output=result.output, phase=phase)
        return result
    fingerprint = _tree_fingerprint(workspace, command)
    cached = _CACHE.get(str(workspace.root))
    if cached is not None and cached.fingerprint == fingerprint:
        record_test_run(
            command=command,
            ok=cached.result.ok,
            output=cached.result.output,
            phase=phase,
            elapsed_ms=0,
            reused=True,
        )
        return reused_check_result("run_tests", cached.result)
    started = time.perf_counter()
    result = run_command(workspace, command, timeout_seconds)
    result = _reject_vacuous_pass(result)
    if not result.ok and toolchain_missing(result.output or "", workspace.root):
        prepared = run_with_throwaway_toolchains(
            workspace, command, result.output or "", timeout_seconds
        )
        if prepared is not None:
            result = _reject_vacuous_pass(prepared)
    if not result.ok and runner_unavailable(result.output or ""):
        prepared = run_in_testbed(workspace, command, result.output or "", timeout_seconds)
        if prepared is not None:
            result = prepared
        if not result.ok and runner_unavailable(result.output or ""):
            result = ToolResult(False, _runner_missing_note(result.output))
    elapsed_ms = int(round((time.perf_counter() - started) * 1000))
    record_test_run(
        command=command,
        ok=result.ok,
        output=result.output,
        phase=phase,
        elapsed_ms=elapsed_ms,
    )
    _CACHE[str(workspace.root)] = _CachedRun(fingerprint=fingerprint, result=result)
    return result


def _runner_missing_note(output: str) -> str:
    note = (
        "The test runner is not installed, or the test script could not be started. "
        "This is not a failing test. Do not change product code or the test command "
        "to hide it."
    )
    body = (output or "").strip()
    return f"{body}\n{note}".strip() if body else note


def _reject_vacuous_pass(result: ToolResult) -> ToolResult:
    """A command that exits 0 without running checks is still a failed test."""
    if not result.ok or not _VACUOUS_TEST_RE.search(result.output or ""):
        return result
    note = (
        "The test command exited 0 without running tests. "
        "Make it execute the suite and exit non-zero when a check fails, "
        "then call run_tests again."
    )
    output = f"{(result.output or '').strip()}\n{note}".strip()
    return ToolResult(False, output)


def current_test_result(
    workspace: Workspace,
    test_command: str | None,
) -> ToolResult | None:
    """The last test result when it already covers the current code. Does not run tests."""
    command = _active_test_command(workspace, test_command)
    if not command:
        return None
    cached = _CACHE.get(str(workspace.root))
    if cached is None or cached.fingerprint != _tree_fingerprint(workspace, command):
        return None
    return cached.result


def _active_test_command(workspace: Workspace, test_command: str | None) -> str | None:
    present, value = configured_value(workspace.root, "test_command")
    if present:
        return value
    if test_command:
        return test_command
    return load_project(workspace.root).test_command


def _tree_fingerprint(workspace: Workspace, test_command: str) -> str:
    digest = hashlib.sha256()
    digest.update(test_command.encode("utf-8"))
    digest.update(b"\0")
    digest.update(code_fingerprint(workspace.root).encode("utf-8"))
    return digest.hexdigest()
