from __future__ import annotations

from pathlib import Path

from agent_lbw.sandbox import Workspace
from agent_lbw.tools import build_tools
from agent_lbw.tools.shell import run_command


def test_git_push_omitted_when_publish_disabled(tmp_path: Path) -> None:
    tools = build_tools(
        Workspace(tmp_path),
        test_command=None,
        command_timeout_seconds=10,
        git_author_name=None,
        git_author_email=None,
        allow_publish=False,
    )
    assert "git_push" not in {tool.name for tool in tools}


def test_git_push_available_when_publish_enabled(tmp_path: Path) -> None:
    tools = build_tools(
        Workspace(tmp_path),
        test_command=None,
        command_timeout_seconds=10,
        git_author_name=None,
        git_author_email=None,
        allow_publish=True,
    )
    assert "git_push" in {tool.name for tool in tools}


def test_run_command_refuses_pr_create_when_publish_disabled(tmp_path: Path) -> None:
    result = run_command(
        Workspace(tmp_path),
        "gh pr create --title demo",
        timeout_seconds=5,
        allow_publish=False,
    )
    assert result.ok is False
    assert "publish is disabled" in result.output


def test_run_command_refuses_push_when_publish_disabled(tmp_path: Path) -> None:
    result = run_command(
        Workspace(tmp_path),
        "git push origin feature",
        timeout_seconds=5,
        allow_publish=False,
    )
    assert result.ok is False
    assert "publish is disabled" in result.output


def test_run_command_refuses_git_commit(tmp_path: Path) -> None:
    result = run_command(
        Workspace(tmp_path),
        "git commit -m 'skip review'",
        timeout_seconds=5,
        allow_publish=False,
    )
    assert result.ok is False
    assert "cycle commits once" in result.output


def test_run_command_refuses_push_to_main_even_when_publish_enabled(tmp_path: Path) -> None:
    result = run_command(
        Workspace(tmp_path),
        "git push origin main",
        timeout_seconds=5,
        allow_publish=True,
    )
    assert result.ok is False
    assert "refusing to push directly to main" in result.output
