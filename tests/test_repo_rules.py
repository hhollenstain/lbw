"""Repository instruction files are read from the primary branch and can be turned off."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from agent_lbw.config import Settings
from agent_lbw.runtime.repo_rules import list_repo_rules, text_for_prompt
from agent_lbw.runtime.tasks import TaskManager
from agent_lbw.web_ui import create_app


def _git(root: Path, *args: str) -> None:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "lbw-test",
            "GIT_AUTHOR_EMAIL": "lbw@test.local",
            "GIT_COMMITTER_NAME": "lbw-test",
            "GIT_COMMITTER_EMAIL": "lbw@test.local",
        }
    )
    subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )


def test_repo_rules_follow_the_primary_branch_and_a_switch_leaves_one_out(
    settings: Settings, tmp_path: Path
) -> None:
    _git(tmp_path, "init", "-b", "main")
    (tmp_path / "AGENTS.md").write_text("Follow main.\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("Use Claude.\n", encoding="utf-8")
    _git(tmp_path, "add", "AGENTS.md", "CLAUDE.md")
    _git(tmp_path, "commit", "-m", "Add instruction files")
    _git(tmp_path, "checkout", "-b", "feature")
    (tmp_path / "AGENTS.md").write_text("Follow the feature branch.\n", encoding="utf-8")

    client = TestClient(create_app(TaskManager(settings), default_workspace=tmp_path))
    page = client.get("/")
    assert page.status_code == 200
    assert b'id="rules-tab-lbw"' in page.content
    assert b'id="rules-tab-repo"' in page.content
    assert b'class="release-count"' in page.content

    listed = client.get("/api/repo-rules", params={"workspace": str(tmp_path)})
    assert listed.status_code == 200
    body = listed.json()
    assert body["branch"] == "main"
    by_path = {item["path"]: item for item in body["files"]}
    assert by_path["AGENTS.md"]["body"].strip() == "Follow main."
    assert by_path["AGENTS.md"]["html"] == "<p>Follow main.</p>"
    assert by_path["AGENTS.md"]["enabled"] is True
    assert by_path["CLAUDE.md"]["enabled"] is True

    turned = client.put(
        "/api/repo-rules",
        json={"workspace": str(tmp_path), "path": "CLAUDE.md", "enabled": False},
    )
    assert turned.status_code == 200
    again = {item["path"]: item for item in turned.json()["files"]}
    assert again["CLAUDE.md"]["enabled"] is False
    prompt = text_for_prompt(tmp_path)
    assert "Follow main." in prompt
    assert "Use Claude." not in prompt

    refused = client.put(
        "/api/repo-rules",
        json={"workspace": str(tmp_path), "path": "README.md", "enabled": False},
    )
    assert refused.status_code == 400


def test_a_folder_without_git_reads_the_instruction_file(tmp_path: Path) -> None:
    (tmp_path / "CURSOR.md").write_text(
        "# Cursor\n\n- Keep the names.\n\n```\nmake web\n```\n",
        encoding="utf-8",
    )
    listed = list_repo_rules(tmp_path)
    assert listed["branch"] == ""
    html = listed["files"][0]["html"]
    assert listed["files"][0]["path"] == "CURSOR.md"
    assert listed["files"][0]["enabled"] is True
    assert "<h1>Cursor</h1>" in html
    assert "<li>Keep the names.</li>" in html
    assert "<pre><code>make web\n</code></pre>" in html
    assert "<script" not in html
