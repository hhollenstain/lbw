"""A reviewer reads the parts of a long file that LBW shortened out of the evidence."""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests.support import init_git_repo

from agent_lbw.llm.client import AssistantTurn, ScriptedClient, ToolCall
from agent_lbw.runtime.review import review_goal
from agent_lbw.runtime.review_index import (
    MAX_READS,
    ReviewReader,
    blob_sha,
    outline,
    review_index,
)
from agent_lbw.runtime.roles import list_roles, review_with_role
from agent_lbw.runtime.workdiff import collect_work_diff
from agent_lbw.sandbox import Workspace
from agent_lbw.tools.git import current_sha


def _agents_file(sections: int = 400) -> str:
    parts = ["# AGENTS.md", ""]
    for number in range(sections):
        parts += [f"## Section {number}", f"Rule {number}: keep the tests green.", ""]
    parts += ["## Marketplace submission", "Submit through the partner portal.", ""]
    return "\n".join(parts)


def _repo_with_agents(root: Path) -> tuple[Workspace, str | None]:
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    init_git_repo(root)
    workspace = Workspace(root)
    sha = current_sha(workspace)
    (root / "AGENTS.md").write_text(_agents_file(), encoding="utf-8")
    return workspace, sha


def test_blob_sha_is_the_git_object_id(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_text(_agents_file(3), encoding="utf-8")
    git = subprocess.run(
        ["git", "hash-object", str(path)], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert blob_sha(path.read_bytes()) == git


def test_outline_names_line_ranges() -> None:
    markdown = "# Title\nintro\n## Layout\nx\n```\n# not a heading\n```\n## Git\ny\n"
    assert outline("AGENTS.md", markdown) == [
        (1, 2, "# Title"),
        (3, 7, "## Layout"),
        (8, 9, "## Git"),
    ]
    python = (
        "import os\n\n\nclass Box:\n    def open(self):\n        pass\n\n\ndef run():\n    pass\n"
    )
    assert outline("app.py", python) == [
        (1, 3, "(top of file)"),
        (4, 4, "class Box"),
        (5, 8, "  def Box.open"),
        (9, 10, "def run"),
    ]
    script = "const a = 1;\nexport function go() {}\nclass Thing {}\n"
    assert [label for _first, _last, label in outline("app.js", script)] == [
        "(top of file)",
        "export function go() {}",
        "class Thing {}",
    ]
    long = outline("AGENTS.md", _agents_file())
    assert len(long) <= 40
    assert long[0][0] == 1 and long[-1][1] == len(_agents_file().splitlines())
    assert all(a[1] + 1 == b[0] for a, b in zip(long, long[1:], strict=False))
    assert long[-1][2].endswith("## Marketplace submission")
    plain = "\n".join(f"row {n}" for n in range(250))
    assert outline("data.csv", plain) == [
        (1, 100, "lines"),
        (101, 200, "lines"),
        (201, 250, "lines"),
    ]


def test_a_shortened_diff_ends_with_a_review_index(tmp_path: Path) -> None:
    workspace, sha = _repo_with_agents(tmp_path)
    diff = collect_work_diff(workspace, sha, goal="Write AGENTS.md")
    assert "[LBW shortened this review copy" in diff
    assert "Submit through the partner portal." not in diff
    index = diff[diff.index("Review index: ") :]
    blob = blob_sha((tmp_path / "AGENTS.md").read_bytes())[:12]
    assert f"AGENTS.md · blob {blob} · " in index
    assert "## Marketplace submission" in index
    assert "read_file(path, start_line, end_line)" in index

    small = tmp_path / "small"
    small.mkdir()
    (small / "README.md").write_text("demo\n", encoding="utf-8")
    init_git_repo(small)
    quiet = Workspace(small)
    before = current_sha(quiet)
    (small / "NOTES.md").write_text("# Notes\nshort\n", encoding="utf-8")
    assert "Review index" not in collect_work_diff(quiet, before, goal="notes")


def test_the_reader_opens_only_indexed_files_as_they_were_reviewed(tmp_path: Path) -> None:
    workspace, sha = _repo_with_agents(tmp_path)
    (tmp_path / "secret.txt").write_text("not for review\n", encoding="utf-8")
    reader = ReviewReader.from_evidence(
        tmp_path, "diff\n\n" + review_index(tmp_path, ["AGENTS.md"])
    )
    assert reader is not None
    total = len(_agents_file().splitlines())
    tail = reader.read("AGENTS.md", total - 3, total)
    assert tail.ok and "Submit through the partner portal." in tail.output
    assert f"{total:>5}|Submit through the partner portal." in tail.output
    refused = reader.read("secret.txt")
    assert not refused.ok and "not in the review index" in refused.output
    assert not reader.read("../README.md").ok
    (tmp_path / "AGENTS.md").write_text("# Rewritten\n", encoding="utf-8")
    changed = reader.read("AGENTS.md", 1, 5)
    assert not changed.ok and "changed after the review began" in changed.output
    for _ in range(MAX_READS):
        reader.read("AGENTS.md")
    assert "No reads left" in reader.read("AGENTS.md").output
    assert ReviewReader.from_evidence(tmp_path, "a diff with no index") is None


def test_the_goal_reviewer_reads_past_the_cut_before_deciding(tmp_path: Path) -> None:
    workspace, sha = _repo_with_agents(tmp_path)
    diff = collect_work_diff(workspace, sha, goal="Write AGENTS.md with marketplace steps")
    total = len(_agents_file().splitlines())
    llm = ScriptedClient(
        [
            AssistantTurn(
                text=None,
                tool_calls=[
                    ToolCall(
                        id="r1",
                        name="read_file",
                        arguments={"path": "AGENTS.md", "start_line": total - 5},
                    )
                ],
            ),
            AssistantTurn(
                text='{"met": true, "reason": "AGENTS.md covers marketplace submission"}'
            ),
        ]
    )
    verdict = review_goal(
        llm,
        "Write AGENTS.md with marketplace steps",
        diff=diff,
        summary="Wrote AGENTS.md",
        tests_passed=True,
        reader=ReviewReader.from_evidence(tmp_path, diff),
    )
    assert verdict.met is True
    sent = llm.calls[-1]
    answer = next(message for message in sent if message.get("role") == "tool")
    assert "Submit through the partner portal." in answer["content"]
    assert answer["tool_call_id"] == "r1"


def test_a_role_reviewer_can_read_through_a_text_tool_call(tmp_path: Path) -> None:
    workspace, sha = _repo_with_agents(tmp_path)
    diff = collect_work_diff(workspace, sha, goal="Write AGENTS.md")
    qa = next(role for role in list_roles(tmp_path) if role.name == "qa")
    llm = ScriptedClient(
        [
            AssistantTurn(
                text='{"name": "read_file", "arguments": {"path": "AGENTS.md", "start_line": 1}}'
            ),
            AssistantTurn(text='{"verdict": "pass", "summary": "complete"}'),
        ]
    )
    verdict = review_with_role(
        llm,
        qa,
        goal="Write AGENTS.md",
        diff=diff,
        summary="Wrote it",
        tests_passed=True,
        reader=ReviewReader.from_evidence(tmp_path, diff),
    )
    assert verdict.verdict == "pass"
    replied = [m for m in llm.calls[-1] if str(m.get("content", "")).startswith("Tool read_file")]
    assert replied and "# AGENTS.md" in replied[0]["content"]
