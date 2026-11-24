from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from agent_lbw.progress import bind_progress, current_events, reset_progress
from agent_lbw.sandbox import Workspace
from agent_lbw.tools.base import ToolResult
from agent_lbw.tools.tests import run_project_tests, runner_unavailable

_BED = re.compile(r"(lbw-testbed-[0-9a-f]{12}-[a-z0-9-]+)")
_MISSING_PYTEST = (
    "uv sync --dev\n"
    "PYTHONPATH=. uv run python -m pytest tests/ -v --tb=short\n"
    "python3: No module named pytest\n"
    "make: *** [test] Error 1\n"
)


def test_missing_pytest_is_not_a_failed_suite() -> None:
    output = (
        "uv sync --dev\n"
        "PYTHONPATH=. uv run python -m pytest tests/ -v --tb=short\n"
        "/tmp/project/.venv/bin/python3: No module named pytest\n"
        "make: *** [test] Error 1\n"
    )
    assert runner_unavailable(output)
    ran = (
        "============================= test session starts =============================\n"
        "collected 1 item\n"
        "FAILED tests/test_models.py::test_rollback - AssertionError: not rolled back\n"
        "1 failed in 0.02s\n"
    )
    assert not runner_unavailable(ran)
    started = (
        "Ran 1 test in 0.001s\nModuleNotFoundError: No module named 'pytest'\nFAILED (errors=1)\n"
    )
    assert not runner_unavailable(started)
    assert runner_unavailable("go: command not found\n")


def test_a_missing_build_tool_is_not_a_failed_suite() -> None:
    """LBW may run where the project's tools are missing, such as `make` in its image."""
    assert runner_unavailable("/bin/bash: line 1: make: command not found\n")
    assert runner_unavailable("sh: 1: npm: not found\n")
    assert runner_unavailable("bash: uv: command not found\n")
    assert not runner_unavailable("make: *** [test] Error 1\nFAILED tests/test_x.py::test_y\n")


def _pyproject(root: Path, dev: str) -> None:
    (root / "pyproject.toml").write_text(
        "[project]\n"
        'name = "demo"\n'
        'version = "0.1.0"\n'
        "dependencies = []\n"
        "\n"
        "[project.optional-dependencies]\n"
        f"dev = [{dev}]\n"
        'docs = ["mkdocs"]\n',
        encoding="utf-8",
    )


def _patch_commands(monkeypatch, fake) -> None:
    monkeypatch.setattr("agent_lbw.tools.tests.run_command", fake)
    monkeypatch.setattr("agent_lbw.runtime.testbed.run_command", fake)


def _only_uv(name: str) -> str | None:
    if name == "uv":
        return "/usr/bin/uv"
    return None


def _materialize(command: str, *parts: str) -> None:
    match = _BED.search(command)
    if match is None:
        return
    path = Path(tempfile.gettempdir()) / match.group(1)
    for part in parts:
        path = path / part
    if parts and parts[-1] == "python":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        return
    path.mkdir(parents=True, exist_ok=True)


def test_missing_pytest_builds_a_throwaway_environment(tmp_path: Path, monkeypatch) -> None:
    _pyproject(root=tmp_path, dev='"pytest>=8", "pytest-asyncio"')
    calls: list[str] = []

    def fake(workspace, command, timeout, allow_publish=False):
        calls.append(command)
        if "uv venv" in command or "uv sync" in command:
            _materialize(command, "bin", "python")
            return ToolResult(True, "built")
        if "uv pip install" in command:
            return ToolResult(True, "installed")
        if "-m pytest" in command:
            return ToolResult(True, "1 passed in 0.01s")
        return ToolResult(False, _MISSING_PYTEST)

    _patch_commands(monkeypatch, fake)
    monkeypatch.setattr("agent_lbw.runtime.testbed.shutil.which", _only_uv)
    token = bind_progress()
    try:
        result = run_project_tests(Workspace(tmp_path), "make test", 10, phase="after")
        steps = [event.get("message", "") for event in current_events() if event["kind"] == "step"]
    finally:
        reset_progress(token)
    build, rerun = calls[1], calls[2]
    assert result.ok
    assert "1 passed" in result.output
    assert "uv venv" in build
    assert ".[dev]" in build
    assert "lbw-testbed-" in build
    assert str(tmp_path) not in build
    assert rerun.startswith("env -u VIRTUAL_ENV PYTHONPATH=. ")
    assert "bin/python -m pytest tests/ -v --tb=short" in rerun
    assert "uv run" not in rerun
    assert "make" not in rerun
    assert any("throwaway Python environment" in message for message in steps)


def test_locked_project_syncs_extras_outside_the_checkout(tmp_path: Path, monkeypatch) -> None:
    _pyproject(root=tmp_path, dev='"pytest>=8"')
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    calls: list[str] = []

    def fake(workspace, command, timeout, allow_publish=False):
        calls.append(command)
        if "uv sync" in command:
            _materialize(command, "bin", "python")
            return ToolResult(True, "synced")
        if "uv pip install" in command or "-m pytest" in command:
            return ToolResult(True, "1 passed in 0.01s")
        return ToolResult(False, _MISSING_PYTEST)

    _patch_commands(monkeypatch, fake)
    monkeypatch.setattr("agent_lbw.runtime.testbed.shutil.which", _only_uv)
    result = run_project_tests(Workspace(tmp_path), "make test", 10, phase="after")
    assert result.ok
    assert "uv sync --frozen --all-extras --all-groups" in calls[1]
    assert "UV_PROJECT_ENVIRONMENT=" in calls[1]
    assert str(tmp_path) not in calls[1]
    assert "bin/python -m pytest" in calls[-1]
    assert "make test" not in calls[-1]


def test_installed_runner_still_reports_a_real_failure(tmp_path: Path, monkeypatch) -> None:
    _pyproject(root=tmp_path, dev='"pytest>=8"')

    def fake(workspace, command, timeout, allow_publish=False):
        if "uv venv" in command or "uv sync" in command:
            _materialize(command, "bin", "python")
            return ToolResult(True, "built")
        if "-m pytest" in command:
            return ToolResult(
                False,
                "test session starts\ncollected 1 item\n"
                "FAILED tests/test_models.py::test_rollback - AssertionError: not rolled back\n"
                "1 failed in 0.02s",
            )
        return ToolResult(False, "python3: No module named pytest\npytest tests/\n")

    _patch_commands(monkeypatch, fake)
    monkeypatch.setattr("agent_lbw.runtime.testbed.shutil.which", _only_uv)
    result = run_project_tests(Workspace(tmp_path), "make test", 10, phase="after")
    assert not result.ok
    assert not runner_unavailable(result.output)
    assert "not a failing test" not in result.output


def test_missing_pytest_uses_a_container_when_uv_is_missing(tmp_path: Path, monkeypatch) -> None:
    _pyproject(root=tmp_path, dev='"pytest>=8"')
    calls: list[str] = []

    def fake(workspace, command, timeout, allow_publish=False):
        calls.append(command)
        if command.startswith("docker run "):
            return ToolResult(True, "1 passed in 0.01s")
        return ToolResult(False, _MISSING_PYTEST)

    _patch_commands(monkeypatch, fake)
    monkeypatch.setattr("agent_lbw.runtime.testbed.shutil.which", lambda name: None)
    monkeypatch.setattr("agent_lbw.runtime.testbed._docker_available", lambda: True)
    result = run_project_tests(Workspace(tmp_path), "make test", 10, phase="after")
    assert result.ok
    docker = calls[1]
    assert docker.startswith("docker run --rm --network bridge ")
    assert "--cap-drop=ALL" in docker
    assert "--security-opt=no-new-privileges" in docker
    assert "ghcr.io/astral-sh/uv:python3.12-bookworm-slim" in docker
    assert f"{tmp_path}:/workspace" in docker or f"{tmp_path.resolve()}:/workspace" in docker
    assert "UV_PROJECT_ENVIRONMENT=/tmp/lbw-testbed" in docker
    assert "--all-extras" in docker
    assert "bin/python -m pytest tests/ -v --tb=short" in docker


def test_missing_jest_uses_a_throwaway_node_environment(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts": {"test": "jest"}, "devDependencies": {"jest": "^29.0.0"}}\n',
        encoding="utf-8",
    )
    calls: list[str] = []

    def fake(workspace, command, timeout, allow_publish=False):
        calls.append(command)
        if "npm install" in command:
            _materialize(command, "node_modules", ".bin")
            return ToolResult(True, "installed")
        if "node_modules/.bin" in command:
            return ToolResult(True, "1 passed")
        return ToolResult(False, "sh: jest: command not found\n")

    _patch_commands(monkeypatch, fake)
    monkeypatch.setattr(
        "agent_lbw.runtime.testbed.shutil.which",
        lambda name: "/usr/bin/npm" if name == "npm" else None,
    )
    result = run_project_tests(Workspace(tmp_path), "npm test", 10, phase="after")
    assert result.ok
    assert "npm install" in calls[1]
    assert "lbw-testbed-" in calls[1]
    assert str(tmp_path) not in calls[1]
    assert "node_modules/.bin" in calls[2]
    assert calls[2].endswith(" jest")


def test_missing_go_uses_a_throwaway_module_cache(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "go.mod").write_text("module example.com/demo\n\ngo 1.23\n", encoding="utf-8")
    calls: list[str] = []

    def fake(workspace, command, timeout, allow_publish=False):
        calls.append(command)
        if "go test" in command:
            return ToolResult(True, "ok")
        return ToolResult(False, "go: command not found\n")

    _patch_commands(monkeypatch, fake)
    monkeypatch.setattr(
        "agent_lbw.runtime.testbed.shutil.which",
        lambda name: "/usr/bin/go" if name == "go" else None,
    )
    result = run_project_tests(Workspace(tmp_path), "make test", 10, phase="after")
    assert result.ok
    assert "GOCACHE=" in calls[1]
    assert "GOMODCACHE=" in calls[1]
    assert "go test ./..." in calls[1]
    assert "lbw-testbed-" in calls[1]
    assert str(tmp_path) not in calls[1]


def test_missing_pytest_without_a_declared_extra_is_left_alone(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    def fake(workspace, command, timeout, allow_publish=False):
        calls.append(command)
        return ToolResult(False, "python3: No module named pytest\n")

    monkeypatch.setattr("agent_lbw.tools.tests.run_command", fake)
    result = run_project_tests(Workspace(tmp_path), "make test", 10, phase="after")
    assert calls == ["make test"]
    assert not result.ok
    assert "not a failing test" in result.output


def test_run_project_tests_marks_a_missing_runner(tmp_path: Path) -> None:
    script = tmp_path / "check.sh"
    script.write_text(
        "#!/bin/sh\n"
        "echo '/tmp/project/.venv/bin/python3: No module named pytest'\n"
        "echo 'make: *** [test] Error 1'\n"
        "exit 2\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    result = run_project_tests(Workspace(tmp_path), "sh check.sh", 10, phase="after")
    assert not result.ok
    assert runner_unavailable(result.output)
    assert "not a failing test" in result.output


def test_run_project_tests_reuses_result_when_tree_is_unchanged(tmp_path: Path) -> None:
    lbw = tmp_path / ".lbw"
    lbw.mkdir()
    marker = lbw / "runs.txt"
    (tmp_path / "check.py").write_text(
        "from pathlib import Path\n"
        "marker = Path('.lbw/runs.txt')\n"
        "count = int(marker.read_text()) if marker.exists() else 0\n"
        "marker.write_text(str(count + 1))\n",
        encoding="utf-8",
    )
    workspace = Workspace(tmp_path)
    command = "python3 check.py"
    token = bind_progress()
    try:
        first = run_project_tests(workspace, command, 10, phase="agent")
        second = run_project_tests(workspace, command, 10, phase="after")
        events = current_events()
    finally:
        reset_progress(token)
    assert first.ok
    assert second.ok
    assert marker.read_text(encoding="utf-8") == "1"
    assert events[0]["reused"] is False
    assert events[1]["reused"] is True
    assert events[1]["elapsed_ms"] == 0
    assert events[1]["phase"] == "after"


def test_run_project_tests_reads_a_command_added_during_the_run(tmp_path: Path) -> None:
    lbw = tmp_path / ".lbw"
    lbw.mkdir()
    (tmp_path / "check.py").write_text("print('ok')\n", encoding="utf-8")
    workspace = Workspace(tmp_path)
    missing = run_project_tests(workspace, None, 10, phase="agent")
    assert not missing.ok
    assert missing.output.startswith("no test command configured")
    (lbw / "config.yaml").write_text(
        "name: demo\ntest_command: python3 check.py\n",
        encoding="utf-8",
    )
    found = run_project_tests(workspace, None, 10, phase="agent")
    assert found.ok
    assert "ok" in found.output


def test_run_project_tests_prefers_the_command_written_in_config(tmp_path: Path) -> None:
    lbw = tmp_path / ".lbw"
    lbw.mkdir()
    (tmp_path / "check.py").write_text("print('from-config')\n", encoding="utf-8")
    (lbw / "config.yaml").write_text(
        "name: demo\ntest_command: python3 check.py\n",
        encoding="utf-8",
    )
    result = run_project_tests(
        Workspace(tmp_path),
        "python3 -c 'raise SystemExit(1)'",
        10,
        phase="agent",
    )
    assert result.ok
    assert "from-config" in result.output


def test_run_project_tests_rejects_a_command_that_does_not_run(tmp_path: Path) -> None:
    script = tmp_path / "check.sh"
    script.write_text(
        "#!/bin/sh\necho 'Tests would be executed in the engine'\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    result = run_project_tests(Workspace(tmp_path), "sh check.sh", 10, phase="agent")
    assert not result.ok
    assert "without running tests" in result.output


def test_run_project_tests_reruns_after_an_edit(tmp_path: Path) -> None:
    lbw = tmp_path / ".lbw"
    lbw.mkdir()
    marker = lbw / "runs.txt"
    (tmp_path / "check.py").write_text(
        "from pathlib import Path\n"
        "marker = Path('.lbw/runs.txt')\n"
        "count = int(marker.read_text()) if marker.exists() else 0\n"
        "marker.write_text(str(count + 1))\n",
        encoding="utf-8",
    )
    workspace = Workspace(tmp_path)
    command = "python3 check.py"
    first = run_project_tests(workspace, command, 10, phase="agent")
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    second = run_project_tests(workspace, command, 10, phase="after")
    assert first.ok
    assert second.ok
    assert marker.read_text(encoding="utf-8") == "2"


def test_run_project_tests_ignores_mtime_and_non_code(tmp_path: Path) -> None:
    lbw = tmp_path / ".lbw"
    lbw.mkdir()
    marker = lbw / "runs.txt"
    app = tmp_path / "app.py"
    app.write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "check.py").write_text(
        "from pathlib import Path\n"
        "marker = Path('.lbw/runs.txt')\n"
        "count = int(marker.read_text()) if marker.exists() else 0\n"
        "marker.write_text(str(count + 1))\n",
        encoding="utf-8",
    )
    workspace = Workspace(tmp_path)
    command = "python3 check.py"
    run_project_tests(workspace, command, 10, phase="agent")
    later = app.stat().st_mtime + 50
    os.utime(app, (later, later))
    (tmp_path / "shot.png").write_bytes(b"\x89PNG\r\n")
    run_project_tests(workspace, command, 10, phase="after")
    assert marker.read_text(encoding="utf-8") == "1"


def test_run_project_tests_reruns_when_another_project_changes(tmp_path: Path) -> None:
    lbw = tmp_path / ".lbw"
    lbw.mkdir()
    marker = lbw / "runs.txt"
    (tmp_path / "check.py").write_text(
        "from pathlib import Path\n"
        "marker = Path('.lbw/runs.txt')\n"
        "count = int(marker.read_text()) if marker.exists() else 0\n"
        "marker.write_text(str(count + 1))\n",
        encoding="utf-8",
    )
    workspace = Workspace(tmp_path)
    command = "python3 check.py"
    run_project_tests(workspace, command, 10, phase="agent")
    other = tmp_path / "other-app" / "src"
    other.mkdir(parents=True)
    (other / "main.py").write_text("print('hi')\n", encoding="utf-8")
    run_project_tests(workspace, command, 10, phase="after")
    assert marker.read_text(encoding="utf-8") == "2"
