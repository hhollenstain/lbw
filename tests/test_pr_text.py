from __future__ import annotations

from pathlib import Path

import pytest
from tests.support import init_git_repo

from agent_lbw.agent.prompts import user_prompt
from agent_lbw.config import Settings
from agent_lbw.runtime.improve import _publish_pull_request
from agent_lbw.runtime.pr_text import written_pull_request
from agent_lbw.runtime.project import load_project
from agent_lbw.sandbox import Workspace
from agent_lbw.tools.base import ToolResult
from agent_lbw.tools.git import run_git

WRITTEN = """I ran the tests and they pass.

PR title: Reject expired session cookies at sign-in

## Summary
Sign-in accepted a cookie past its expiry. It now checks the expiry and asks
the person to sign in again.

## Changes
- `auth/session.py`: compare the cookie's expiry with the clock.
- `tests/test_session.py`: cover an expired and a fresh cookie.

## How to test
1. Run `uv run pytest tests/test_session.py`.
2. Sign in, set the cookie's expiry in the past, and reload.

## What to check
- A fresh cookie still signs in without a prompt.
"""


def _project(root: Path) -> None:
    (root / "app.py").write_text("print('base')\n", encoding="utf-8")
    lbw = root / ".lbw"
    lbw.mkdir()
    (lbw / "config.yaml").write_text("name: fixture\n", encoding="utf-8")
    (root / ".gitignore").write_text(".lbw/\n", encoding="utf-8")
    init_git_repo(root)


def test_the_build_prompt_asks_for_the_pull_request() -> None:
    text = user_prompt("Fix the login", "")
    for heading in ("PR title:", "## Summary", "## Changes", "## How to test", "## What to check"):
        assert heading in text


def test_a_reply_without_the_shape_has_no_written_pull_request() -> None:
    assert written_pull_request("Fixed the login.") is None
    assert written_pull_request("PR title: Fix the login\n\nNo sections here.") is None
    fenced = written_pull_request("```\n**PR title:** Fix the login\n\n## Summary\nDone.\n```")
    assert fenced is not None
    assert fenced.title == "Fix the login"
    assert fenced.body == "## Summary\nDone."


def test_the_pull_request_uses_the_title_and_description_the_agent_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project(tmp_path)
    workspace = Workspace(tmp_path)
    run_git(workspace, ["checkout", "-b", "lbw/runner-1"])
    (tmp_path / "app.py").write_text("print('changed')\n", encoding="utf-8")
    opened: dict = {}

    def fake_pr(_workspace, title, body, **_kwargs):
        opened.update(title=title, body=body)
        return ToolResult(True, "https://github.com/acme/repo/pull/7")

    monkeypatch.setattr("agent_lbw.runtime.improve.existing_pull_request", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "agent_lbw.runtime.improve.push_changes", lambda *_a: ToolResult(True, "pushed")
    )
    monkeypatch.setattr("agent_lbw.runtime.improve.create_pull_request", fake_pr)
    published, pr_url, failed, _ = _publish_pull_request(
        workspace,
        load_project(tmp_path),
        "the login keeps people signed in forever\n\nfix it",
        WRITTEN,
        tests_passed=True,
        sha=None,
        committed=False,
        screenshot_files=[],
        token="ghs_test",
        settings=Settings(model_name="m", model_base_url="http://127.0.0.1:9/v1"),
        store_root=tmp_path,
        title="the login keeps people signed in forever",
    )
    assert failed is None and published is True
    assert pr_url == "https://github.com/acme/repo/pull/7"
    assert opened["title"] == "Reject expired session cookies at sign-in"
    body = opened["body"]
    assert body.startswith("## Summary\nSign-in accepted a cookie past its expiry.")
    for part in ("## Changes", "## How to test", "## What to check", "## Checked by LBW"):
        assert part in body
    assert "- [x] Project tests passed locally" in body
    assert "I ran the tests" not in body
    subject = run_git(workspace, ["log", "-1", "--format=%s"]).stdout.strip()
    assert subject == "Reject expired session cookies at sign-in"
