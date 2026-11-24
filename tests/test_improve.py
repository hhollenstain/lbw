from __future__ import annotations

from pathlib import Path

import pytest
from tests.support import init_git_repo

from agent_lbw.config import Settings
from agent_lbw.llm.client import AssistantTurn, ScriptedClient, ToolCall
from agent_lbw.runtime.improve import resolve_create_pr, run_cycle
from agent_lbw.runtime.project import load_project
from agent_lbw.runtime.uireview import UiEvidence
from agent_lbw.sandbox import Workspace
from agent_lbw.tools.base import ToolResult
from agent_lbw.tools.git import CO_AUTHORED_BY, current_branch, current_sha, run_git


@pytest.fixture(autouse=True)
def _fake_agent_review_ui(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agent_lbw.tools.browser.collect_ui_evidence",
        lambda *args, **kwargs: UiEvidence(ok=True, snapshot="Queue task"),
    )


def _broken_project(root: Path) -> None:
    (root / "app.py").write_text(
        "def add(left, right):\n    raise NotImplementedError\n",
        encoding="utf-8",
    )
    (root / "check.py").write_text(
        "from app import add\nassert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    lbw = root / ".lbw"
    lbw.mkdir()
    (lbw / "config.yaml").write_text(
        "name: fixture\n"
        "test_command: python3 check.py\n"
        "max_repair_attempts: 0\n"
        "publish:\n  enabled: false\n"
        "goals_file: goals.md\n",
        encoding="utf-8",
    )
    (lbw / "goals.md").write_text("- [ ] Make the adder work\n", encoding="utf-8")
    (root / ".gitignore").write_text(".lbw/\n", encoding="utf-8")
    init_git_repo(root)


def test_cycle_starts_from_a_clean_head_branch(tmp_path: Path, settings: Settings) -> None:
    (tmp_path / "app.py").write_text("print('base')\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(".lbw/\n", encoding="utf-8")
    init_git_repo(tmp_path)
    workspace = Workspace(tmp_path)
    head = current_branch(workspace) or "master"
    sha = current_sha(workspace) or ""
    run_git(workspace, ["update-ref", f"refs/remotes/origin/{head}", sha])
    run_git(workspace, ["checkout", "-b", "feature/wip"])
    (tmp_path / "app.py").write_text("print('dirty')\n", encoding="utf-8")
    tight = settings.model_copy(update={"max_iterations": 1})
    llm = ScriptedClient([AssistantTurn(text="I looked around.")])
    result = run_cycle(tmp_path, tight, llm, goal="Leave the printed text alone")
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "print('base')\n"
    assert current_branch(workspace) == head
    steps = [event.get("message", "") for event in result.events if event["kind"] == "step"]
    assert any(f"Starting from a clean {head}" in message for message in steps)


def test_cycle_uses_the_branch_named_in_the_goal(tmp_path: Path, settings: Settings) -> None:
    (tmp_path / "app.py").write_text("print('base')\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(".lbw/\n", encoding="utf-8")
    init_git_repo(tmp_path)
    workspace = Workspace(tmp_path)
    head = current_branch(workspace) or "master"
    run_git(workspace, ["checkout", "-b", "feature/login"])
    (tmp_path / "app.py").write_text("print('feature')\n", encoding="utf-8")
    run_git(workspace, ["add", "app.py"])
    run_git(workspace, ["commit", "-m", "feature"])
    feature = current_sha(workspace) or ""
    run_git(workspace, ["update-ref", "refs/remotes/origin/feature/login", feature])
    run_git(workspace, ["checkout", head])
    tight = settings.model_copy(update={"max_iterations": 1})
    llm = ScriptedClient([AssistantTurn(text="I looked around.")])
    run_cycle(tmp_path, tight, llm, goal="Ship the login fix on branch feature/login")
    assert current_branch(workspace) == "feature/login"
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "print('feature')\n"


def test_cycle_dropdown_branch_overrides_the_goal(tmp_path: Path, settings: Settings) -> None:
    (tmp_path / "app.py").write_text("print('base')\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(".lbw/\n", encoding="utf-8")
    init_git_repo(tmp_path)
    workspace = Workspace(tmp_path)
    head = current_branch(workspace) or "master"
    run_git(workspace, ["checkout", "-b", "feature/login"])
    (tmp_path / "app.py").write_text("print('feature')\n", encoding="utf-8")
    run_git(workspace, ["add", "app.py"])
    run_git(workspace, ["commit", "-m", "feature"])
    feature = current_sha(workspace) or ""
    run_git(workspace, ["update-ref", "refs/remotes/origin/feature/login", feature])
    run_git(workspace, ["checkout", head])
    tight = settings.model_copy(update={"max_iterations": 1})
    llm = ScriptedClient([AssistantTurn(text="I looked around.")])
    run_cycle(
        tmp_path,
        tight,
        llm,
        goal=f"Start on branch {head}",
        base_branch="feature/login",
    )
    assert current_branch(workspace) == "feature/login"
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "print('feature')\n"


def test_cycle_rejects_a_null_check_that_hides_the_stated_error(
    tmp_path: Path, settings: Settings
) -> None:
    _green_project(tmp_path)
    llm = ScriptedClient(
        [
            _write_file_turn(
                "app.py",
                "def add(left, right):\n"
                "    return left + right\n"
                "def ready():\n"
                "    player = get_node_or_null('Player')\n"
                "    if player == null:\n"
                "        print('missing')\n",
            ),
            AssistantTurn(
                text=None,
                tool_calls=[
                    ToolCall(
                        id="search",
                        name="web_search",
                        arguments={
                            "query": (
                                'Godot ERROR: Node not found: "Player" at scripts/main_game.gd:60'
                            )
                        },
                    )
                ],
            ),
            AssistantTurn(
                text=None,
                tool_calls=[
                    ToolCall(
                        id="fetch",
                        name="fetch_url",
                        arguments={"url": "https://docs.godotengine.org/en/stable/"},
                    )
                ],
            ),
            AssistantTurn(text="Guarded the missing player."),
            _review_turn(True, "the blank screen is fixed"),
        ]
    )
    result = run_cycle(
        tmp_path,
        settings,
        llm,
        goal='ERROR: Node not found: "Player" at scripts/main_game.gd:60',
    )
    assert result.status == "failed"
    reason = (result.reason or "").lower()
    assert "null check" in reason or "guards" in reason


def test_cycle_does_not_fail_when_pytest_is_missing(tmp_path: Path, settings: Settings) -> None:
    _broken_project(tmp_path)
    (tmp_path / "missing.sh").write_text(
        "#!/bin/sh\n"
        "echo '/tmp/project/.venv/bin/python3: No module named pytest'\n"
        "echo 'make: *** [test] Error 1'\n"
        "exit 2\n",
        encoding="utf-8",
    )
    (tmp_path / "missing.sh").chmod(0o755)
    (tmp_path / ".lbw" / "config.yaml").write_text(
        "name: fixture\n"
        "test_command: sh missing.sh\n"
        "max_repair_attempts: 1\n"
        "publish:\n  enabled: false\n"
        "goals_file: goals.md\n",
        encoding="utf-8",
    )
    llm = ScriptedClient(
        [
            AssistantTurn(
                text=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="write_file",
                        arguments={
                            "path": "app.py",
                            "content": "def add(left, right):\n    return left + right\n",
                        },
                    )
                ],
            ),
            AssistantTurn(text="Implemented add."),
            _review_turn(True, "the adder returns the sum"),
        ]
    )
    result = run_cycle(tmp_path, settings, llm)
    assert result.status == "success"
    assert result.tests_passed is None
    assert result.committed is True
    assert result.reason != "tests failed; commit skipped"
    steps = [event.get("message", "") for event in result.events if event["kind"] == "step"]
    assert any("not a failing test" in message for message in steps)
    assert not any(message.startswith("Repair attempt") for message in steps)


def test_cycle_repairs_a_suite_that_actually_failed(tmp_path: Path, settings: Settings) -> None:
    _broken_project(tmp_path)
    (tmp_path / "fail.sh").write_text(
        "#!/bin/sh\n"
        "echo '============================= test session starts ============================='\n"
        "echo 'collected 1 item'\n"
        "echo 'FAILED tests/test_models.py::test_rollback - AssertionError: not rolled back'\n"
        "echo '1 failed in 0.02s'\n"
        "exit 1\n",
        encoding="utf-8",
    )
    (tmp_path / ".lbw" / "config.yaml").write_text(
        "name: fixture\n"
        "test_command: sh fail.sh\n"
        "max_repair_attempts: 1\n"
        "publish:\n  enabled: false\n"
        "goals_file: goals.md\n",
        encoding="utf-8",
    )
    llm = ScriptedClient(
        [
            AssistantTurn(
                text=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="write_file",
                        arguments={
                            "path": "app.py",
                            "content": "def add(left, right):\n    return left + right\n",
                        },
                    )
                ],
            ),
            AssistantTurn(text="Implemented add."),
            AssistantTurn(text="Tried to fix the failing test."),
        ]
    )
    result = run_cycle(tmp_path, settings, llm, goal="Make the adder work")
    assert result.status == "failed"
    assert result.tests_passed is False
    assert result.committed is False
    assert result.reason == "tests failed; commit skipped"
    steps = [event.get("message", "") for event in result.events if event["kind"] == "step"]
    assert any(message == "Repair attempt 1/1" for message in steps)


def test_cycle_commits_when_scripted_fix_passes(tmp_path: Path, settings: Settings) -> None:
    _broken_project(tmp_path)
    llm = ScriptedClient(
        [
            AssistantTurn(
                text=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="write_file",
                        arguments={
                            "path": "app.py",
                            "content": "def add(left, right):\n    return left + right\n",
                        },
                    )
                ],
            ),
            AssistantTurn(text="Implemented add and verified with python3 check.py."),
            _review_turn(True, "adder returns 5 and tests passed"),
        ]
    )
    result = run_cycle(tmp_path, settings, llm)
    assert result.status == "success"
    assert result.tests_passed is True
    assert result.committed is True
    assert result.published is False
    assert result.commit_sha
    kinds = [event["kind"] for event in result.events]
    assert "llm" in kinds
    assert "review" in kinds
    assert any(
        event["kind"] == "review" and event["parsed"] is True and event["met"] is True
        for event in result.events
    )
    assert any(
        event["kind"] == "file" and event["path"] == "app.py" and event["action"] == "updated"
        for event in result.events
    )
    file_event = next(event for event in result.events if event["kind"] == "file")
    assert "return left + right" in file_event["diff"]
    llm_event = next(event for event in result.events if event["kind"] == "llm")
    assert "elapsed_ms" in llm_event
    assert llm_event["at"].endswith("Z")
    branch = current_branch(Workspace(tmp_path))
    assert branch in {"main", "master"}
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == (
        "def add(left, right):\n    return left + right\n"
    )


def test_cycle_skips_commit_when_tests_still_fail(tmp_path: Path, settings: Settings) -> None:
    _broken_project(tmp_path)
    llm = ScriptedClient([AssistantTurn(text="I looked around and stopped.")])
    result = run_cycle(tmp_path, settings, llm, goal="Make the adder work")
    assert result.status == "failed"
    assert result.tests_passed is False
    assert result.committed is False
    tests = [event for event in result.events if event["kind"] == "test"]
    assert tests
    failed = [event for event in tests if event["ok"] is False]
    assert failed
    assert any("NotImplementedError" in (event.get("output") or "") for event in failed)
    assert any(event.get("phase") == "after" for event in tests)
    assert not any(event.get("phase") == "before" for event in tests)


def test_cycle_sets_up_tests_when_none_are_configured(tmp_path: Path, settings: Settings) -> None:
    (tmp_path / ".lbw").mkdir()
    (tmp_path / ".lbw" / "config.yaml").write_text(
        "name: fixture\nmax_repair_attempts: 1\npublish:\n  enabled: false\ngoals_file: goals.md\n",
        encoding="utf-8",
    )
    (tmp_path / ".gitignore").write_text(".lbw/\n", encoding="utf-8")
    init_git_repo(tmp_path)
    llm = ScriptedClient(
        [
            _write_file_turn(
                "app.py",
                "def add(left, right):\n    return left + right\n",
                call_id="write-app",
            ),
            AssistantTurn(text="Implemented add."),
            AssistantTurn(
                text=None,
                tool_calls=[
                    ToolCall(id="search-1", name="web_search", arguments={"query": "pytest"})
                ],
            ),
            AssistantTurn(
                text=None,
                tool_calls=[
                    ToolCall(
                        id="fetch-1",
                        name="fetch_url",
                        arguments={"url": "https://docs.pytest.org/en/stable/"},
                    )
                ],
            ),
            _write_file_turn(
                "check.py",
                "from app import add\nassert add(2, 3) == 5\n",
                call_id="write-check",
            ),
            _write_file_turn(
                ".lbw/config.yaml",
                "name: fixture\n"
                "test_command: python3 check.py\n"
                "max_repair_attempts: 1\n"
                "publish:\n  enabled: false\n"
                "goals_file: goals.md\n",
                call_id="write-config",
            ),
            AssistantTurn(
                text=None,
                tool_calls=[ToolCall(id="run-1", name="run_tests", arguments={})],
            ),
            AssistantTurn(text="Added a test command and the check passes."),
            _review_turn(True, "add is covered by python3 check.py"),
        ]
    )
    result = run_cycle(tmp_path, settings, llm, goal="Prove add(2, 3) is 5")
    assert result.status == "success"
    assert result.tests_passed is True
    assert load_project(tmp_path).test_command == "python3 check.py"
    assert any(
        event.get("kind") == "test" and event.get("phase") == "setup" and event.get("ok")
        for event in result.events
    )


def test_cycle_skips_commit_when_lint_fails(tmp_path: Path, settings: Settings) -> None:
    _broken_project(tmp_path)
    config = tmp_path / ".lbw" / "config.yaml"
    config.write_text(
        "name: fixture\n"
        "test_command: python3 check.py\n"
        "lint_command: python3 -c \"raise SystemExit('F401 unused import')\"\n"
        "max_repair_attempts: 0\n"
        "publish:\n  enabled: false\n"
        "goals_file: goals.md\n",
        encoding="utf-8",
    )
    llm = ScriptedClient(
        [
            AssistantTurn(
                text=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="write_file",
                        arguments={
                            "path": "app.py",
                            "content": "def add(left, right):\n    return left + right\n",
                        },
                    )
                ],
            ),
            AssistantTurn(text="Implemented add and verified with python3 check.py."),
            _review_turn(True, "adder returns 5 and tests passed"),
        ]
    )
    result = run_cycle(tmp_path, settings, llm)
    assert result.status == "failed"
    assert result.tests_passed is True
    assert result.committed is False
    assert result.published is False
    assert result.reason.startswith("lint failed; commit skipped:")
    assert "F401" in (result.reason or "")
    assert "F401" in (result.summary or "")
    lint_events = [event for event in result.events if event["kind"] == "lint"]
    assert lint_events
    assert any(event["ok"] is False for event in lint_events)
    assert any("F401" in (event.get("output") or "") for event in lint_events)


def test_cycle_attempts_lint_repair(tmp_path: Path, settings: Settings) -> None:
    _broken_project(tmp_path)
    config = tmp_path / ".lbw" / "config.yaml"
    config.write_text(
        "name: fixture\n"
        "test_command: python3 check.py\n"
        "lint_command: python3 -c \"raise SystemExit('F401 unused import')\"\n"
        "max_repair_attempts: 1\n"
        "publish:\n  enabled: false\n"
        "goals_file: goals.md\n",
        encoding="utf-8",
    )
    llm = ScriptedClient(
        [
            AssistantTurn(
                text=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="write_file",
                        arguments={
                            "path": "app.py",
                            "content": "def add(left, right):\n    return left + right\n",
                        },
                    )
                ],
            ),
            AssistantTurn(text="Implemented add and verified with python3 check.py."),
            _review_turn(True, "adder returns 5 and tests passed"),
            AssistantTurn(text="Tried to fix lint."),
        ]
    )
    result = run_cycle(tmp_path, settings, llm)
    assert result.status == "failed"
    assert any(
        event.get("kind") == "step" and "Lint repair 1/4" in (event.get("message") or "")
        for event in result.events
    )
    assert result.reason.startswith("lint failed; commit skipped:")


def test_cycle_repairs_lint_in_untouched_files(tmp_path: Path, settings: Settings) -> None:
    (tmp_path / "leftover.py").write_text("x = 1\n", encoding="utf-8")
    _broken_project(tmp_path)
    (tmp_path / "lint_fail.py").write_text(
        "raise SystemExit('F821 Undefined name `self`\\n   --> leftover.py:55:10')\n",
        encoding="utf-8",
    )
    config = tmp_path / ".lbw" / "config.yaml"
    config.write_text(
        "name: fixture\n"
        "test_command: python3 check.py\n"
        "lint_command: python3 lint_fail.py\n"
        "max_repair_attempts: 1\n"
        "publish:\n  enabled: false\n"
        "goals_file: goals.md\n",
        encoding="utf-8",
    )
    llm = ScriptedClient(
        [
            AssistantTurn(
                text=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="write_file",
                        arguments={
                            "path": "app.py",
                            "content": "def add(left, right):\n    return left + right\n",
                        },
                    )
                ],
            ),
            AssistantTurn(text="Implemented add and verified with python3 check.py."),
            _review_turn(True, "adder returns 5 and tests passed"),
            AssistantTurn(text="Tried to fix lint."),
        ]
    )
    result = run_cycle(tmp_path, settings, llm)
    assert result.status == "failed"
    assert any(
        event.get("kind") == "step" and "Lint repair 1/4" in (event.get("message") or "")
        for event in result.events
    )
    assert any(
        event.get("kind") == "step" and "Undefined name `self`" in (event.get("message") or "")
        for event in result.events
    )
    assert result.reason.startswith("lint failed; commit skipped:")
    assert "leftover.py" in (result.reason or "")


def test_cycle_skips_before_tests_when_a_goal_is_given(tmp_path: Path, settings: Settings) -> None:
    _broken_project(tmp_path)
    llm = ScriptedClient([AssistantTurn(text="I looked around and stopped.")])
    result = run_cycle(tmp_path, settings, llm, goal="Make the adder work")
    phases = [event.get("phase") for event in result.events if event["kind"] == "test"]
    assert "before" not in phases
    assert "after" in phases


def test_no_create_pr_flag_wins_over_project_config(tmp_path: Path, settings: Settings) -> None:
    _broken_project(tmp_path)
    config = tmp_path / ".lbw" / "config.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "publish:\n  enabled: false\n",
            "publish:\n  enabled: true\n",
        ),
        encoding="utf-8",
    )
    project = load_project(tmp_path)
    assert project.publish_enabled is True
    assert resolve_create_pr(settings, project, cli_create_pr=False) is False
    assert resolve_create_pr(settings, project, cli_create_pr=None) is True
    assert resolve_create_pr(settings, project, cli_create_pr=True) is True


def test_git_repo_defaults_to_creating_a_pr(tmp_path: Path, settings: Settings) -> None:
    (tmp_path / ".git").mkdir()
    project = load_project(tmp_path)
    assert project.is_git is True
    assert project.publish_enabled is True
    assert resolve_create_pr(settings, project, cli_create_pr=None) is True
    assert resolve_create_pr(settings, project, cli_create_pr=False) is False


def test_explicit_publish_off_still_disables_pr_on_a_git_repo(
    tmp_path: Path, settings: Settings
) -> None:
    (tmp_path / ".git").mkdir()
    lbw = tmp_path / ".lbw"
    lbw.mkdir()
    (lbw / "config.yaml").write_text(
        "name: fixture\npublish:\n  enabled: false\n",
        encoding="utf-8",
    )
    project = load_project(tmp_path)
    assert project.is_git is True
    assert project.publish_enabled is False
    assert resolve_create_pr(settings, project, cli_create_pr=None) is False


def _green_project(root: Path) -> None:
    (root / "app.py").write_text(
        "def add(left, right):\n    return left + right\n",
        encoding="utf-8",
    )
    (root / "check.py").write_text(
        "from app import add\nassert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    lbw = root / ".lbw"
    lbw.mkdir()
    (lbw / "config.yaml").write_text(
        "name: fixture\n"
        "test_command: python3 check.py\n"
        "max_repair_attempts: 0\n"
        "publish:\n  enabled: false\n"
        "goals_file: goals.md\n",
        encoding="utf-8",
    )
    (lbw / "goals.md").write_text("- [ ] Improve the UI\n", encoding="utf-8")
    (root / ".gitignore").write_text(".lbw/\n", encoding="utf-8")
    init_git_repo(root)
    runs = lbw / "runs"
    runs.mkdir()
    (runs / "old.json").write_text('{"status": "success"}\n', encoding="utf-8")


def test_cycle_does_not_commit_run_logs_or_plans(tmp_path: Path, settings: Settings) -> None:
    _green_project(tmp_path)
    llm = ScriptedClient(
        [
            AssistantTurn(text="I will add a copy field next."),
            AssistantTurn(text="Here is the write_file JSON I would send."),
            AssistantTurn(text="Summary of the planned UI changes."),
            AssistantTurn(text="Still only describing the work."),
            _review_turn(False, "the UI was not changed"),
        ]
    )
    result = run_cycle(tmp_path, settings, llm, goal="Improve the UI", cli_create_pr=True)
    assert result.status == "skipped"
    assert result.committed is False
    assert result.published is False
    assert "no files changed and the goal is not already met" in (result.reason or "")
    tracked = run_git(Workspace(tmp_path), ["ls-files", ".lbw/runs"])
    assert tracked.stdout.strip() == ""
    assert (tmp_path / ".lbw" / "runs" / "old.json").exists()


def test_cycle_retries_when_agent_inspects_but_goal_is_unmet(
    tmp_path: Path, settings: Settings
) -> None:
    _green_project(tmp_path)
    config = tmp_path / ".lbw" / "config.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "max_repair_attempts: 0\n",
            "max_repair_attempts: 1\n",
        ),
        encoding="utf-8",
    )
    llm = ScriptedClient(
        [
            AssistantTurn(text="I will inspect the UI first."),
            AssistantTurn(text="The templates exist; I will add a progress bar next."),
            AssistantTurn(text="Here is the write_file JSON I would send."),
            AssistantTurn(text="Done looking at the current UI."),
            _review_turn(False, "no progress bar was added"),
            _write_file_turn("ui.html", "<div class='task-stages'>progress</div>\n"),
            _review_ui_turn(),
            AssistantTurn(text="Added a task stage progress bar."),
            _review_turn(True, "progress bar with stages is present"),
        ]
    )
    result = run_cycle(tmp_path, settings, llm, goal="Add a task progress bar")
    assert result.status == "success"
    assert "task-stages" in (tmp_path / "ui.html").read_text(encoding="utf-8")
    assert result.committed is True


def test_cycle_repairs_tests_broken_by_empty_diff_retry(tmp_path: Path, settings: Settings) -> None:
    (tmp_path / "ui.html").write_text(
        '<textarea id="guidelines"></textarea>\n',
        encoding="utf-8",
    )
    (tmp_path / "check.py").write_text(
        "from pathlib import Path\nassert 'id=\"guidelines\"' in Path('ui.html').read_text()\n",
        encoding="utf-8",
    )
    lbw = tmp_path / ".lbw"
    lbw.mkdir()
    (lbw / "config.yaml").write_text(
        "name: fixture\n"
        "test_command: python3 check.py\n"
        "max_repair_attempts: 1\n"
        "publish:\n  enabled: false\n"
        "goals_file: goals.md\n",
        encoding="utf-8",
    )
    (lbw / "goals.md").write_text(
        "- [ ] Move guidelines into a settings menu\n",
        encoding="utf-8",
    )
    init_git_repo(tmp_path)
    llm = ScriptedClient(
        [
            AssistantTurn(text="I will inspect the UI first."),
            AssistantTurn(text="The templates exist; I will move guidelines next."),
            AssistantTurn(text="Here is the write_file JSON I would send."),
            AssistantTurn(text="Done looking at the current UI."),
            _review_turn(False, "guidelines are still in the sidebar"),
            _write_file_turn(
                "ui.html",
                '<div id="settings-panel"><textarea id="rules"></textarea></div>\n',
            ),
            _review_ui_turn(),
            AssistantTurn(text="Moved guidelines into settings."),
            _write_file_turn(
                "check.py",
                "from pathlib import Path\n"
                "assert 'id=\"settings-panel\"' in Path('ui.html').read_text()\n",
                call_id="call-2",
            ),
            AssistantTurn(text="Updated the HTML assertion."),
            _review_turn(True, "guidelines are in the settings panel"),
        ]
    )
    result = run_cycle(tmp_path, settings, llm, goal="Move guidelines into a settings menu")
    assert result.status == "success"
    html = (tmp_path / "ui.html").read_text(encoding="utf-8")
    assert "settings-panel" in html
    assert 'id="guidelines"' not in html
    assert "settings-panel" in (tmp_path / "check.py").read_text(encoding="utf-8")
    assert result.committed is True
    tests = [event for event in result.events if event["kind"] == "test"]
    assert any(event.get("phase") == "retry" and event.get("ok") is False for event in tests)
    assert any(event.get("phase") == "repair" and event.get("ok") is True for event in tests)


def test_retry_prompts_forbid_placeholder_work() -> None:
    from agent_lbw.runtime.improve import (
        _empty_diff_retry_prompt,
        _goal_retry_prompt,
        _test_repair_prompt,
    )

    empty = _empty_diff_retry_prompt(
        "Add a task progress bar",
        "no progress bar was added",
    )
    assert "Add a task progress bar" in empty
    assert "placeholder" in empty.lower()
    assert "stub" in empty.lower()
    assert "str_replace" in empty
    assert "update those tests" in empty
    retry = _goal_retry_prompt(
        "Add a task progress bar",
        "only a verification test was added",
        "diff --git a/tests/test_write_verification.py",
    )
    assert "Add a task progress bar" in retry
    assert "verification" in retry.lower()
    assert "unrelated" in retry.lower()
    assert "stub" in retry.lower()
    assert "update those tests" in retry
    assert "enabled workspace skills" in retry.lower()
    broken = _goal_retry_prompt(
        "Extract CSS into files",
        "CSS files are returning 404",
        "diff --git a/src/app/static/theme.css",
        ui_errors=["404 http://127.0.0.1:9/static/theme.css"],
        stalled=True,
    )
    assert "did not change files" in broken
    assert "404 http://127.0.0.1:9/static/theme.css" in broken
    assert "StaticFiles" in broken
    assert "review_ui" in broken
    same = _goal_retry_prompt(
        "Fix the player",
        "still not found",
        "diff --git a/scripts/main_game.gd",
        ui_errors=['ERROR: Node not found: "Player"'],
        repeated=True,
    )
    assert "did not change what the review sees" in same
    assert "different approach" in same
    repair = _test_repair_prompt('AssertionError: id="guidelines"')
    assert "update that test" in repair
    assert "do not revert the goal" in repair.lower()
    assert 'id="guidelines"' in repair


def test_cycle_create_pr_uses_feature_branch_not_main(
    tmp_path: Path, settings: Settings, monkeypatch
) -> None:
    _broken_project(tmp_path)
    workspace = Workspace(tmp_path)
    protected = current_branch(workspace)
    initial = current_sha(workspace)
    captured: dict[str, str | None] = {}

    def fake_push(ws, remote="origin", branch=None, token=None):
        captured["push_branch"] = branch
        captured["push_current"] = current_branch(ws)
        return ToolResult(True, "pushed")

    def fake_pr(ws, title, body, *, base=None, token=None):
        captured["title"] = title
        captured["body"] = body
        captured["base"] = base
        captured["pr_branch"] = current_branch(ws)
        return ToolResult(True, "https://example.test/pull/1")

    monkeypatch.setattr("agent_lbw.runtime.improve.push_changes", fake_push)
    monkeypatch.setattr("agent_lbw.runtime.improve.create_pull_request", fake_pr)
    llm = ScriptedClient(
        [
            AssistantTurn(
                text=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="write_file",
                        arguments={
                            "path": "app.py",
                            "content": "def add(left, right):\n    return left + right\n",
                        },
                    )
                ],
            ),
            AssistantTurn(text="Implemented add and verified with python3 check.py."),
            _review_turn(True, "adder returns 5 and tests passed"),
        ]
    )
    result = run_cycle(tmp_path, settings, llm, cli_create_pr=True)
    assert result.status == "success"
    assert result.committed is True
    assert result.published is True
    assert result.pr_url == "https://example.test/pull/1"
    assert any(
        event.get("kind") == "pr" and event.get("url") == result.pr_url for event in result.events
    )
    assert captured["push_branch"]
    assert str(captured["push_branch"]).startswith("lbw/")
    assert captured["push_branch"] != protected
    assert captured["push_current"] == captured["push_branch"]
    assert captured["pr_branch"] == captured["push_branch"]
    assert captured["base"] == protected
    assert "## What Changed" in str(captured["body"])
    assert "To address the stated goal" not in str(captured["body"])
    assert "## Test plan" in str(captured["body"])
    assert "Start from" not in str(captured["title"])
    assert len(str(captured["title"])) <= 72
    assert CO_AUTHORED_BY in str(captured["body"])
    assert result.merged is False
    log = run_git(workspace, ["log", "-1", "--format=%B"]).stdout
    assert CO_AUTHORED_BY in log
    assert current_branch(workspace).startswith("lbw/")
    assert run_git(workspace, ["rev-parse", protected or "HEAD"]).stdout.strip() == initial
    assert "Goal review confirmed the requested outcome" in str(captured["body"])


def test_pull_request_title_describes_the_change_not_the_schedule_prompt() -> None:
    from agent_lbw.progress import bind_progress, record_event, reset_progress
    from agent_lbw.runtime.improve import _commit_message, _pr_body
    from agent_lbw.runtime.schedules import project_task_goal

    goal = project_task_goal("Review the login flow for unsigned session cookies.", "master")
    title = _commit_message(goal, "Reject unsigned session cookies.")
    assert title == "Reject unsigned session cookies."
    assert "Start from" not in title
    bare = _commit_message(goal, "")
    assert bare == "Review the login flow for unsigned session cookies."
    prompt = (
        "Start from the master branch. If this project already meets the goal, "
        "change nothing and report what you checked."
    )
    clipped = _commit_message(prompt, "")
    assert clipped == "Update the project"
    assert "ch-" not in clipped

    body = _pr_body(
        goal,
        "Reject unsigned session cookies.\n\nWhy: cookies were accepted without a signature.",
        tests_passed=True,
        commit_sha="abc",
        branch="lbw/login",
    )
    assert "## Why This Update" in body
    assert "cookies were accepted without a signature" in body
    assert body.count("Review the login flow for unsigned session cookies.") == 1
    assert "Start from the master" not in body

    repeated = _pr_body(
        goal,
        "Review the login flow for unsigned session cookies.",
        tests_passed=None,
        commit_sha=None,
        branch="lbw/login",
    )
    assert "## Why This Update" not in repeated
    assert repeated.count("Review the login flow for unsigned session cookies.") == 1

    token = bind_progress()
    try:
        record_event(kind="test", command="pytest -q", ok=True)
        record_event(kind="test", command="pytest -q", ok=True)
        record_event(kind="test", command="pytest -q tests/test_login.py", ok=False)
        noisy = "\n".join(
            [
                "Verified the login flow and checked the cookie parser.",
                "Steps to test:",
                "1. Sign in and confirm the session cookie is rejected.",
                "2. Sign in and confirm the session cookie is rejected.",
                "3. " + ("repeat the same check " * 20),
            ]
        )
        steps_body = _pr_body(
            goal,
            noisy,
            tests_passed=True,
            commit_sha=None,
            branch="",
        )
    finally:
        reset_progress(token)
    section = steps_body.split("## Steps to Test", 1)[1]
    assert section.count("`pytest -q` — passed") == 1
    assert "`pytest -q tests/test_login.py` — failed" in section
    assert "Verified the login flow" not in section
    assert section.count("Sign in and confirm the session cookie is rejected.") == 1
    assert "repeat the same check" not in section


def test_cycle_continuous_proposes_when_goals_are_empty(tmp_path: Path, settings: Settings) -> None:
    _green_project(tmp_path)
    (tmp_path / ".lbw" / "goals.md").write_text("- [x] Already done\n", encoding="utf-8")
    llm = ScriptedClient(
        [
            AssistantTurn(text=("GOAL: Add a module docstring to app.py\n\nDocument add().")),
            AssistantTurn(
                text='{"accept": true, "reason": "app.py has no module docstring", "goal": ""}'
            ),
            AssistantTurn(
                text=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="str_replace",
                        arguments={
                            "path": "app.py",
                            "old_string": "def add(left, right):\n    return left + right\n",
                            "new_string": (
                                '"""Tiny adder used by the fixture tests."""\n'
                                "def add(left, right):\n    return left + right\n"
                            ),
                        },
                    )
                ],
            ),
            AssistantTurn(text="Added a module docstring."),
            _review_turn(True, "app.py has a module docstring"),
        ]
    )
    result = run_cycle(tmp_path, settings, llm, continuous=True)
    assert result.status == "success"
    assert result.goal and "module docstring" in result.goal
    assert result.committed is True
    assert '"""Tiny adder' in (tmp_path / "app.py").read_text(encoding="utf-8")


def test_cycle_continuous_skips_when_nothing_to_do(tmp_path: Path, settings: Settings) -> None:
    _green_project(tmp_path)
    (tmp_path / ".lbw" / "goals.md").write_text("- [x] Already done\n", encoding="utf-8")
    llm = ScriptedClient([AssistantTurn(text="NOTHING_TO_DO")])
    result = run_cycle(tmp_path, settings, llm, continuous=True)
    assert result.status == "skipped"
    assert result.merged is False


def test_cycle_continuous_merges_after_review(
    tmp_path: Path, settings: Settings, monkeypatch
) -> None:
    _broken_project(tmp_path)
    captured: dict[str, str | None] = {}

    def fake_push(ws, remote="origin", branch=None, token=None):
        return ToolResult(True, "pushed")

    def fake_pr(ws, title, body, *, base=None, token=None):
        captured["pr"] = "https://example.test/pull/9"
        return ToolResult(True, captured["pr"])

    def fake_merge(ws, url=None, *, squash=True, token=None):
        captured["merged"] = url
        return ToolResult(True, "merged")

    def fake_checkout(ws, base, remote="origin", *, token=None):
        captured["base"] = base
        return ToolResult(True, f"checked out {base}")

    monkeypatch.setattr("agent_lbw.runtime.improve.push_changes", fake_push)
    monkeypatch.setattr("agent_lbw.runtime.improve.create_pull_request", fake_pr)
    monkeypatch.setattr("agent_lbw.runtime.improve.merge_pull_request", fake_merge)
    monkeypatch.setattr("agent_lbw.runtime.improve.checkout_base_branch", fake_checkout)
    llm = ScriptedClient(
        [
            AssistantTurn(
                text=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="write_file",
                        arguments={
                            "path": "app.py",
                            "content": "def add(left, right):\n    return left + right\n",
                        },
                    )
                ],
            ),
            AssistantTurn(text="Implemented add and verified with python3 check.py."),
            _review_turn(True, "adder returns 5 and tests passed"),
        ]
    )
    result = run_cycle(
        tmp_path,
        settings,
        llm,
        cli_create_pr=True,
        continuous=True,
    )
    assert result.status == "success"
    assert result.published is True
    assert result.merged is True
    assert captured["merged"] == "https://example.test/pull/9"
    assert captured["base"] in {"main", "master"}


def test_cycle_pr_hosts_only_the_change_screenshot(
    tmp_path: Path, settings: Settings, monkeypatch
) -> None:
    _broken_project(tmp_path)
    shots = tmp_path / ".lbw" / "ui-screenshots"
    shots.mkdir(exist_ok=True)
    change = shots / "ui-review_goal_final.png"
    change.write_bytes(b"png-bytes")
    (shots / "ui-review_review-ui_dump.png").write_bytes(b"dump")
    workspace = Workspace(tmp_path)
    run_git(workspace, ["remote", "add", "origin", "git@github.com:acme/repo.git"])
    monkeypatch.setattr(
        "agent_lbw.runtime.improve._pr_screenshot_names",
        lambda: ["ui-review_goal_final.png"],
    )
    captured: dict[str, str | None] = {}

    def fake_push(ws, remote="origin", branch=None, token=None):
        captured["push_branch"] = branch
        return ToolResult(True, "pushed")

    def fake_pr(ws, title, body, *, base=None, token=None):
        captured["body"] = body
        captured["sha"] = current_sha(ws)
        return ToolResult(True, "https://example.test/pull/2")

    monkeypatch.setattr("agent_lbw.runtime.improve.push_changes", fake_push)
    monkeypatch.setattr("agent_lbw.runtime.improve.create_pull_request", fake_pr)
    llm = ScriptedClient(
        [
            AssistantTurn(
                text=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="write_file",
                        arguments={
                            "path": "app.py",
                            "content": "def add(left, right):\n    return left + right\n",
                        },
                    )
                ],
            ),
            AssistantTurn(text="Implemented add and verified with python3 check.py."),
            _review_turn(True, "adder returns 5 and tests passed"),
        ]
    )
    result = run_cycle(tmp_path, settings, llm, cli_create_pr=True)
    assert result.status == "success"
    assert result.published is True
    body = str(captured["body"])
    sha = captured["sha"]
    assert sha
    assert ".lbw/ui-screenshots" not in body
    assert "ui-review_goal_final.png" not in body
    assert "ui-review_review-ui_dump.png" not in body
    tracked = run_git(
        workspace,
        ["ls-files", "--", ".lbw"],
    ).stdout.splitlines()
    assert tracked == []


def _write_file_turn(path: str, content: str, call_id: str = "call-1") -> AssistantTurn:
    return AssistantTurn(
        text=None,
        tool_calls=[
            ToolCall(
                id=call_id,
                name="write_file",
                arguments={"path": path, "content": content},
            )
        ],
    )


def _review_ui_turn(call_id: str = "ui-1") -> AssistantTurn:
    return AssistantTurn(
        text=None,
        tool_calls=[ToolCall(id=call_id, name="review_ui", arguments={})],
    )


def _review_turn(met: bool, reason: str) -> AssistantTurn:
    payload = '{"met": true, "reason": "%s"}' if met else '{"met": false, "reason": "%s"}'
    return AssistantTurn(text=payload % reason)


def test_cycle_rejects_mock_implementation_even_if_reviewer_says_met(
    tmp_path: Path, settings: Settings
) -> None:
    _green_project(tmp_path)
    llm = ScriptedClient(
        [
            _write_file_turn(
                "issues.py",
                "def generate_mock_issues():\n"
                "    # In a real implementation this would call GitHub\n"
                "    return [{'title': 'Feature or fix'}]\n",
            ),
            AssistantTurn(text="Added issue listing from the repo."),
            _review_turn(True, "issues can now be listed from the repository"),
        ]
    )
    result = run_cycle(
        tmp_path,
        settings,
        llm,
        goal="Load GitHub issues from the current repository",
    )
    assert result.status == "failed"
    assert result.committed is False
    assert "unfinished work" in (result.reason or "")
    assert "generate_mock_issues" in (result.reason or "")


def test_cycle_rejects_unused_helper_even_if_reviewer_says_met(
    tmp_path: Path, settings: Settings
) -> None:
    _green_project(tmp_path)
    llm = ScriptedClient(
        [
            _write_file_turn(
                "app.py",
                "def add(left, right):\n"
                "    return left + right\n"
                "\n"
                "def list_for_workspace(workspace_id):\n"
                "    return []\n",
            ),
            AssistantTurn(text="Scoped task listing to the current workspace."),
            _review_turn(True, "workspace tasks are filtered"),
        ]
    )
    result = run_cycle(
        tmp_path,
        settings,
        llm,
        goal="The current task shouldn't show tasks from other workspaces",
    )
    assert result.status == "failed"
    assert result.committed is False
    assert "list_for_workspace" in (result.reason or "")
    assert "nothing calls" in (result.reason or "")


def test_cycle_rejects_invented_readme_commands_even_if_reviewer_says_met(
    tmp_path: Path, settings: Settings
) -> None:
    _green_project(tmp_path)
    llm = ScriptedClient(
        [
            _write_file_turn(
                "README.md",
                "docker clone git@github.com:user/repo.git /workspaces/my-repo\n",
            ),
            AssistantTurn(text="Documented how to clone a repo in Docker."),
            AssistantTurn(
                text=None,
                tool_calls=[ToolCall(id="test-1", name="run_tests", arguments={})],
            ),
            AssistantTurn(text="Tests passed."),
            _review_turn(True, "docker workflow is documented"),
        ]
    )
    result = run_cycle(
        tmp_path,
        settings,
        llm,
        goal="Setup app to run in docker/docker compose",
    )
    assert result.status == "failed"
    assert result.committed is False
    assert "cannot work" in (result.reason or "")
    assert "docker clone" in (result.reason or "")


def test_cycle_rejects_iteration_limit_even_if_reviewer_says_met(
    tmp_path: Path, settings: Settings
) -> None:
    _green_project(tmp_path)
    tight = settings.model_copy(update={"max_iterations": 1})
    llm = ScriptedClient(
        [
            _write_file_turn(
                "app.py",
                "def add(left, right):\n    return left + right\n# note\n",
            ),
            _review_turn(True, "adder still works"),
        ]
    )
    result = run_cycle(
        tmp_path,
        tight,
        llm,
        goal="Make the adder work",
    )
    assert result.status == "failed"
    assert result.committed is False
    assert "iteration limit" in (result.reason or "") or "max_iterations" in (result.reason or "")


def test_cycle_accepts_iteration_limit_when_rendered_ui_is_verified(
    tmp_path: Path, settings: Settings, monkeypatch
) -> None:
    html = (
        "<button class='rerun-btn'>Rerun</button>\n"
        "<script>document.querySelector('.rerun-btn')"
        ".addEventListener('click', () => fetch('/api/tasks/1/rerun'));</script>\n"
    )
    _green_project(tmp_path)
    (tmp_path / "src" / "agent_lbw" / "templates").mkdir(parents=True)
    (tmp_path / "src" / "agent_lbw" / "templates" / "index.html").write_text(
        html,
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "agent_lbw.runtime.improve.collect_ui_evidence",
        lambda *args, **kwargs: UiEvidence(
            ok=True,
            snapshot=("button.rerun-btn: ↻ Rerun (72x24 @ 900,400)\nspan: +12 −3 (48x16 @ 16,400)"),
            screenshot="ok.png",
            clicked=['[data-main-pane="history"]'],
        ),
    )
    tight = settings.model_copy(update={"max_iterations": 1})
    llm = ScriptedClient(
        [
            _write_file_turn(
                "src/agent_lbw/templates/index.html",
                html,
            ),
            _review_turn(True, "rerun button no longer overlaps the line stats"),
        ]
    )
    result = run_cycle(
        tmp_path,
        tight,
        llm,
        goal="Fix the overlapping of the past runs rerun button",
    )
    assert result.status == "success"
    assert result.committed is True


def _inspect_only_turns() -> list[AssistantTurn]:
    return [
        AssistantTurn(text="Looked at the current branch."),
        AssistantTurn(text="No files to change."),
        AssistantTurn(text="The work is already on this branch."),
        AssistantTurn(text="Stopping without edits."),
    ]


def _commit_on_feature_branch(root: Path, name: str = "lbw/existing-work") -> None:
    workspace = Workspace(root)
    run_git(workspace, ["checkout", "-b", name])
    app = root / "app.py"
    app.write_text(app.read_text(encoding="utf-8") + "# feature\n", encoding="utf-8")
    run_git(workspace, ["add", "app.py"])
    run_git(workspace, ["commit", "-m", "feature work"])


def test_cycle_opens_pr_for_existing_feature_branch_without_new_files(
    tmp_path: Path, settings: Settings, monkeypatch
) -> None:
    _green_project(tmp_path)
    _commit_on_feature_branch(tmp_path)
    captured: dict[str, str | None] = {}

    def fake_pr(ws, title, body, *, base=None, token=None):
        captured["branch"] = current_branch(ws)
        captured["base"] = base
        return ToolResult(True, "https://example.test/pull/11")

    monkeypatch.setattr(
        "agent_lbw.runtime.improve.push_changes",
        lambda *args, **kwargs: ToolResult(True, "pushed"),
    )
    monkeypatch.setattr("agent_lbw.runtime.improve.create_pull_request", fake_pr)
    monkeypatch.setattr(
        "agent_lbw.runtime.improve.existing_pull_request", lambda _ws, token=None: None
    )
    llm = ScriptedClient(
        [
            *_inspect_only_turns(),
            _review_turn(False, "the cycle has not opened a pull request yet"),
        ]
    )
    result = run_cycle(
        tmp_path,
        settings,
        llm,
        goal="from the current branch create a PR",
        cli_create_pr=True,
    )
    assert result.status == "success"
    assert result.committed is False
    assert result.published is True
    assert result.pr_url == "https://example.test/pull/11"
    assert captured["branch"] == "lbw/existing-work"
    assert captured["base"] in {"main", "master"}
    assert "without new files" in (result.reason or "")
    assert any(
        event.get("kind") == "pr" and event.get("url") == result.pr_url for event in result.events
    )


def test_cycle_reuses_existing_pr_without_creating_another(
    tmp_path: Path, settings: Settings, monkeypatch
) -> None:
    _green_project(tmp_path)
    _commit_on_feature_branch(tmp_path)
    created = {"count": 0}
    edited = {"count": 0}

    def fake_pr(*args, **kwargs):
        created["count"] += 1
        return ToolResult(True, "https://example.test/pull/8")

    def fake_edit(*args, **kwargs):
        edited["count"] += 1
        edited["title"] = args[1] if len(args) > 1 else kwargs.get("title")
        return ToolResult(True, "updated")

    monkeypatch.setattr(
        "agent_lbw.runtime.improve.push_changes",
        lambda *args, **kwargs: ToolResult(True, "pushed"),
    )
    monkeypatch.setattr("agent_lbw.runtime.improve.create_pull_request", fake_pr)
    monkeypatch.setattr("agent_lbw.runtime.improve.update_pull_request", fake_edit)
    monkeypatch.setattr(
        "agent_lbw.runtime.improve.existing_pull_request",
        lambda _ws, token=None: "https://example.test/pull/7",
    )
    llm = ScriptedClient(
        [
            *_inspect_only_turns(),
            _review_turn(True, "the feature branch already has the work"),
        ]
    )
    result = run_cycle(
        tmp_path,
        settings,
        llm,
        goal="from the current branch create a PR",
        cli_create_pr=True,
    )
    assert result.status == "success"
    assert result.committed is False
    assert result.published is True
    assert result.pr_url == "https://example.test/pull/7"
    assert created["count"] == 0
    assert edited["count"] == 1


def test_cycle_does_not_open_pr_from_main_without_new_files(
    tmp_path: Path, settings: Settings, monkeypatch
) -> None:
    _green_project(tmp_path)
    created = {"count": 0}

    def fake_pr(*args, **kwargs):
        created["count"] += 1
        return ToolResult(True, "https://example.test/pull/3")

    monkeypatch.setattr(
        "agent_lbw.runtime.improve.push_changes",
        lambda *args, **kwargs: ToolResult(True, "pushed"),
    )
    monkeypatch.setattr("agent_lbw.runtime.improve.create_pull_request", fake_pr)
    llm = ScriptedClient(
        [
            *_inspect_only_turns(),
            _review_turn(True, "the workspace already matches the goal"),
        ]
    )
    result = run_cycle(
        tmp_path,
        settings,
        llm,
        goal="from the current branch create a PR",
        cli_create_pr=True,
    )
    assert result.status == "success"
    assert result.committed is False
    assert result.published is False
    assert result.pr_url is None
    assert created["count"] == 0
    assert current_branch(Workspace(tmp_path)) in {"main", "master"}


def test_cycle_skips_pr_when_goal_is_not_met(
    tmp_path: Path, settings: Settings, monkeypatch
) -> None:
    _green_project(tmp_path)
    monkeypatch.setattr(
        "agent_lbw.runtime.improve.push_changes",
        lambda *args, **kwargs: ToolResult(True, "pushed"),
    )
    monkeypatch.setattr(
        "agent_lbw.runtime.improve.create_pull_request",
        lambda *args, **kwargs: ToolResult(True, "https://example.test/pull/9"),
    )
    llm = ScriptedClient(
        [
            _write_file_turn("app.py", "def add(left, right):\n    return left + right\n# todo\n"),
            AssistantTurn(text="Tweaked the adder."),
            _review_turn(False, "header is still present and the sidebar toggle is hidden"),
        ]
    )
    result = run_cycle(
        tmp_path,
        settings,
        llm,
        goal="Remove the top header and make the sidebar collapsible",
        cli_create_pr=True,
    )
    assert result.status == "failed"
    assert result.published is False
    assert result.committed is False
    assert "goal not met" in (result.reason or "")
    assert "header is still present and the sidebar toggle is hidden" in (result.reason or "")
    qa = [item for item in result.evidence if item.get("role") == "qa"]
    assert qa
    assert qa[0]["verdict"] == "send_back"
    assert qa[0]["summary"] == "header is still present and the sidebar toggle is hidden"
    assert (result.summary or "").startswith("Failed:")
    assert current_branch(Workspace(tmp_path)) in {"main", "master"}


def test_cycle_rejects_a_visual_goal_when_nothing_renders(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _broken_project(tmp_path)
    monkeypatch.setattr(
        "agent_lbw.runtime.improve.collect_ui_evidence",
        lambda *_args, **_kwargs: UiEvidence(
            ok=False,
            notes=(
                "No preview server for this workspace. Set preview_command in .lbw/config.yaml."
            ),
        ),
    )
    llm = ScriptedClient(
        [
            _write_file_turn("app.py", "def add(left, right):\n    return left + right\n"),
            AssistantTurn(text="I have successfully completed the task."),
            _review_turn(True, "the start button now runs the game"),
        ]
    )
    result = run_cycle(
        tmp_path,
        settings,
        llm,
        goal="The start button does not render the game",
    )
    assert result.status == "failed"
    assert "No preview server" in (result.reason or "")
    assert (result.summary or "").startswith("Failed:")
    assert not (result.summary or "").lower().startswith("i have successfully")


def test_cycle_looks_up_preview_command_for_a_visual_goal(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _green_project(tmp_path)
    config = tmp_path / ".lbw" / "config.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "max_repair_attempts: 0\n",
            "max_repair_attempts: 1\n",
        ),
        encoding="utf-8",
    )

    def fake_ui(workspace, project, goal, **_kwargs):
        del project, goal
        if load_project(workspace.root).preview_command:
            return UiEvidence(ok=True, snapshot="Start game", url="http://127.0.0.1:9/")
        return UiEvidence(
            ok=False,
            notes="No preview server for this workspace. Set preview_command in .lbw/config.yaml.",
        )

    monkeypatch.setattr("agent_lbw.runtime.improve.collect_ui_evidence", fake_ui)
    llm = ScriptedClient(
        [
            _write_file_turn(
                "app.py",
                "def add(left, right):\n    return left + right  # start\n",
            ),
            AssistantTurn(text="The start button still needs a preview."),
            _review_turn(True, "the start button now runs the game"),
            AssistantTurn(
                text=None,
                tool_calls=[
                    ToolCall(
                        id="search-1",
                        name="web_search",
                        arguments={"query": "Godot 4 headless screenshot"},
                    )
                ],
            ),
            AssistantTurn(
                text=None,
                tool_calls=[
                    ToolCall(
                        id="fetch-1",
                        name="fetch_url",
                        arguments={"url": "https://docs.godotengine.org/en/stable/"},
                    )
                ],
            ),
            AssistantTurn(
                text=None,
                tool_calls=[
                    ToolCall(
                        id="config-1",
                        name="configure_project",
                        arguments={"preview_command": "python3 -m http.server {port}"},
                    )
                ],
            ),
            AssistantTurn(text="Configured a preview from the Godot docs."),
            _review_turn(True, "the start screen is visible"),
        ]
    )
    result = run_cycle(
        tmp_path,
        settings,
        llm,
        goal="The start button does not render the game",
    )
    assert result.status == "success"
    assert load_project(tmp_path).preview_command == "python3 -m http.server {port}"
    assert load_project(tmp_path).test_command == "python3 check.py"
    steps = [event.get("message", "") for event in result.events if event["kind"] == "step"]
    assert any("Looking up how to test and preview" in message for message in steps)
    prompts = [
        message.get("content") or ""
        for call in llm.calls
        for message in call
        if isinstance(message.get("content"), str)
    ]
    assert any("web_search" in prompt and "configure_project" in prompt for prompt in prompts)


def test_cycle_retries_then_opens_pr_when_goal_is_met(
    tmp_path: Path, settings: Settings, monkeypatch
) -> None:
    _green_project(tmp_path)
    config = tmp_path / ".lbw" / "config.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "max_repair_attempts: 0\n",
            "max_repair_attempts: 1\n",
        ),
        encoding="utf-8",
    )
    captured: dict[str, str | None] = {}

    def fake_pr(ws, title, body, *, base=None, token=None):
        captured["body"] = body
        return ToolResult(True, "https://example.test/pull/4")

    monkeypatch.setattr(
        "agent_lbw.runtime.improve.push_changes",
        lambda *args, **kwargs: ToolResult(True, "pushed"),
    )
    monkeypatch.setattr("agent_lbw.runtime.improve.create_pull_request", fake_pr)
    llm = ScriptedClient(
        [
            _write_file_turn("ui.html", "<header>lbw</header>\n"),
            _review_ui_turn("ui-header"),
            AssistantTurn(text="Added a header."),
            _review_turn(False, "the header is still there"),
            _write_file_turn(
                "ui.html",
                "<aside id='sidebar'><button id='toggle-sidebar'>collapse</button></aside>\n",
                call_id="call-2",
            ),
            _review_ui_turn("ui-toggle"),
            AssistantTurn(text="Removed the header and added a collapse control."),
            _review_turn(True, "header gone and sidebar toggle is present"),
        ]
    )
    result = run_cycle(
        tmp_path,
        settings,
        llm,
        goal="Remove the top header and make the sidebar collapsible",
        cli_create_pr=True,
    )
    assert result.status == "success"
    assert result.committed is True
    assert result.published is True
    assert result.pr_url == "https://example.test/pull/4"
    assert any(
        event.get("kind") == "pr" and event.get("url") == result.pr_url for event in result.events
    )
    assert "Goal review confirmed" in str(captured["body"])
    assert "<header>" not in (tmp_path / "ui.html").read_text(encoding="utf-8")
    assert "toggle-sidebar" in (tmp_path / "ui.html").read_text(encoding="utf-8")


def test_cycle_retries_unparsed_review(tmp_path: Path, settings: Settings) -> None:
    _green_project(tmp_path)
    config = tmp_path / ".lbw" / "config.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "max_repair_attempts: 0\n",
            "max_repair_attempts: 1\n",
        ),
        encoding="utf-8",
    )
    llm = ScriptedClient(
        [
            _write_file_turn("app.py", "def add(left, right):\n    return left + right\n# note\n"),
            AssistantTurn(text="Tweaked the adder."),
            AssistantTurn(text="Looks done to me."),
            AssistantTurn(text="Still looks done."),
            _write_file_turn(
                "ui.html",
                "<aside id='sidebar'><button id='toggle-sidebar'>collapse</button></aside>\n",
                call_id="call-2",
            ),
            _review_ui_turn("ui-toggle"),
            AssistantTurn(text="Removed the header and added a collapse control."),
            _review_turn(True, "sidebar toggle is present"),
        ]
    )
    result = run_cycle(
        tmp_path,
        settings,
        llm,
        goal="Remove the top header and make the sidebar collapsible",
    )
    assert result.status == "success"
    assert result.committed is True
    assert "toggle-sidebar" in (tmp_path / "ui.html").read_text(encoding="utf-8")
    reviews = [event for event in result.events if event["kind"] == "review"]
    assert reviews[0]["parsed"] is False
    assert any(event.get("parsed") is True and event.get("met") is True for event in reviews)


def test_cycle_keeps_retrying_after_noop_goal_retry(tmp_path: Path, settings: Settings) -> None:
    _green_project(tmp_path)
    config = tmp_path / ".lbw" / "config.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "max_repair_attempts: 0\n",
            "max_repair_attempts: 1\n",
        ),
        encoding="utf-8",
    )
    tight = settings.model_copy(update={"max_iterations": 4})
    llm = ScriptedClient(
        [
            _write_file_turn("ui.html", "<header>lbw</header>\n"),
            _review_ui_turn("ui-header"),
            AssistantTurn(text="Added a header."),
            _review_turn(False, "CSS files are returning 404"),
            AssistantTurn(text="I will inspect the static mount next."),
            AssistantTurn(text="Still looking at the server."),
            AssistantTurn(text="Checking the server config."),
            AssistantTurn(text="No edits this round."),
            _review_turn(False, "CSS files are still returning 404"),
            _write_file_turn(
                "ui.html",
                "<aside id='sidebar'><button id='toggle-sidebar'>collapse</button></aside>\n",
                call_id="call-2",
            ),
            _review_ui_turn("ui-toggle"),
            AssistantTurn(text="Served the CSS and added the toggle."),
            _review_turn(True, "css loads and sidebar toggle is present"),
        ]
    )
    result = run_cycle(
        tmp_path,
        tight,
        llm,
        goal="Remove the top header and make the sidebar collapsible",
    )
    assert result.status == "success"
    assert "toggle-sidebar" in (tmp_path / "ui.html").read_text(encoding="utf-8")
    steps = [event.get("message", "") for event in result.events if event["kind"] == "step"]
    assert any("continuing instead of giving up" in message for message in steps)


def test_cycle_review_prompt_includes_lockfile_versions(tmp_path: Path, settings: Settings) -> None:
    _green_project(tmp_path)
    hashes = ",\n".join(f'                "sha256:{index:064x}"' for index in range(80))
    (tmp_path / "Pipfile.lock").write_text(
        "{\n"
        '    "default": {\n'
        '        "discord.py": {\n'
        f'            "hashes": [\n{hashes}\n            ],\n'
        '            "version": "==2.3.2"\n'
        "        }\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "setup.py").write_text("INSTALL = ['discord.py==2.3.2']\n", encoding="utf-8")
    run_git(Workspace(tmp_path), ["add", "-A"])
    run_git(Workspace(tmp_path), ["commit", "-m", "lockfiles"])
    new_lock = (
        "{\n"
        '    "default": {\n'
        '        "discord.py": {\n'
        f'            "hashes": [\n{hashes}\n            ],\n'
        '            "version": "==2.7.1"\n'
        "        }\n"
        "    }\n"
        "}\n"
    )
    llm = ScriptedClient(
        [
            _write_file_turn("setup.py", "INSTALL = ['discord.py==2.7.1']\n"),
            AssistantTurn(
                text=None,
                tool_calls=[
                    ToolCall(
                        id="call-2",
                        name="write_file",
                        arguments={"path": "Pipfile.lock", "content": new_lock},
                    )
                ],
            ),
            AssistantTurn(text="Updated discord.py to 2.7.1."),
            _review_turn(True, "discord.py 2.3.2 -> 2.7.1"),
        ]
    )
    result = run_cycle(
        tmp_path,
        settings,
        llm,
        goal="This repo is using a really outdated version of discord.py",
    )
    assert result.status == "success"
    review_messages = [
        message["content"]
        for batch in llm.calls
        for message in batch
        if message.get("role") == "user" and "Diff:" in str(message.get("content") or "")
    ]
    assert review_messages
    diff_text = str(review_messages[0])
    assert "discord.py: 2.3.2 -> 2.7.1" in diff_text
    assert "setup.py" in diff_text
    assert diff_text.count("sha256") < 3


def _discord_lockfile(version: str) -> str:
    return (
        "{\n"
        '    "default": {\n'
        '        "discord.py": {\n'
        '            "hashes": [\n'
        '                "sha256:00"\n'
        "            ],\n"
        f'            "version": "=={version}"\n'
        "        }\n"
        "    }\n"
        "}\n"
    )


def test_cycle_already_done_when_dependency_is_current(tmp_path: Path, settings: Settings) -> None:
    _green_project(tmp_path)
    goal = "Update the outdated discord.py library"
    (tmp_path / ".lbw" / "goals.md").write_text(f"- [ ] {goal}\n", encoding="utf-8")
    (tmp_path / "setup.py").write_text("INSTALL = ['discord.py==2.7.1']\n", encoding="utf-8")
    (tmp_path / "Pipfile.lock").write_text(_discord_lockfile("2.7.1"), encoding="utf-8")
    run_git(Workspace(tmp_path), ["add", "-A"])
    run_git(Workspace(tmp_path), ["commit", "-m", "discord.py 2.7.1"])
    llm = ScriptedClient(
        [
            AssistantTurn(text="discord.py is already 2.7.1."),
            AssistantTurn(text="No files to change."),
            AssistantTurn(text="The lockfile already has the current version."),
            AssistantTurn(text="Stopping without edits."),
            _review_turn(True, "discord.py is already 2.7.1 in Pipfile.lock"),
        ]
    )
    result = run_cycle(tmp_path, settings, llm, goal=goal)
    assert result.status == "success"
    assert result.committed is False
    assert result.published is False
    assert "no changes needed" in (result.reason or "")
    assert "2.7.1" in (result.reason or "")
    assert "not pushed" in (result.reason or "")
    assert "No work to do" in (result.summary or "")
    assert "- [x] Update the outdated discord.py library" in (
        tmp_path / ".lbw" / "goals.md"
    ).read_text(encoding="utf-8")
    review_messages = [
        message["content"]
        for batch in llm.calls
        for message in batch
        if message.get("role") == "user"
        and "Current workspace:" in str(message.get("content") or "")
    ]
    assert review_messages
    assert "discord.py" in str(review_messages[0])
    assert "2.7.1" in str(review_messages[0])


def test_cycle_already_done_notes_when_head_matches_upstream(
    tmp_path: Path, settings: Settings
) -> None:
    _green_project(tmp_path)
    workspace = Workspace(tmp_path)
    (tmp_path / "setup.py").write_text("INSTALL = ['discord.py==2.7.1']\n", encoding="utf-8")
    run_git(workspace, ["add", "-A"])
    run_git(workspace, ["commit", "-m", "discord.py 2.7.1"])
    branch = current_branch(workspace)
    sha = current_sha(workspace)
    run_git(workspace, ["update-ref", f"refs/remotes/origin/{branch}", sha or ""])
    run_git(workspace, ["branch", f"--set-upstream-to=origin/{branch}"])
    llm = ScriptedClient(
        [
            AssistantTurn(text="Already updated."),
            AssistantTurn(text="Nothing to edit."),
            AssistantTurn(text="Tree already matches the goal."),
            AssistantTurn(text="Done."),
            _review_turn(True, "setup.py already pins discord.py 2.7.1"),
        ]
    )
    result = run_cycle(
        tmp_path,
        settings,
        llm,
        goal="Update discord.py",
    )
    assert result.status == "success"
    assert "no changes needed" in (result.reason or "")
    assert "pushed" in (result.reason or "")
    assert "not pushed" not in (result.reason or "")


def test_an_approved_change_publishes_with_the_github_token(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_lbw.runtime.improve import _publish_held
    from agent_lbw.runtime.project import load_project

    _broken_project(tmp_path)
    workspace = Workspace(tmp_path)
    run_git(workspace, ["checkout", "-b", "lbw/feature"])
    (tmp_path / "app.py").write_text(
        "def add(left, right):\n    return left + right\n",
        encoding="utf-8",
    )
    seen: dict[str, str | None] = {}

    def fake_publish(*_args, **kwargs):
        seen["token"] = kwargs.get("token")
        return True, "https://github.com/acme/repo/pull/1", None, True

    monkeypatch.setattr("agent_lbw.runtime.improve._publish_pull_request", fake_publish)
    monkeypatch.setattr("agent_lbw.runtime.improve._github_token", lambda *_args: "ghs_test")
    monkeypatch.setattr("agent_lbw.runtime.improve._maybe_test", lambda *_args, **_kwargs: None)
    result = _publish_held(
        workspace,
        settings,
        load_project(tmp_path),
        "Make the adder work",
        "The adder returns the sum.",
        allow_create_pr=True,
        mark_checkbox=False,
        store_root=tmp_path,
    )
    assert seen["token"] == "ghs_test"
    assert result.published is True
    assert result.pr_url == "https://github.com/acme/repo/pull/1"


def test_publish_commits_the_change_before_it_pushes(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_lbw.runtime.improve import _publish_pull_request

    _broken_project(tmp_path)
    workspace = Workspace(tmp_path)
    run_git(workspace, ["checkout", "-b", "lbw/feature"])
    (tmp_path / "app.py").write_text(
        "def add(left, right):\n    return left + right\n",
        encoding="utf-8",
    )
    order: list[str] = []

    def fake_push(workspace, remote, branch, token):
        order.append("push")
        from agent_lbw.tools.git import has_changes

        assert has_changes(workspace) is False
        assert branch == "lbw/feature"
        return ToolResult(True, "pushed")

    def fake_pr(*_args, **_kwargs):
        order.append("pr")
        return ToolResult(True, "https://github.com/acme/repo/pull/3")

    monkeypatch.setattr("agent_lbw.runtime.improve.existing_pull_request", lambda *_a, **_k: None)
    monkeypatch.setattr("agent_lbw.runtime.improve.push_changes", fake_push)
    monkeypatch.setattr("agent_lbw.runtime.improve.create_pull_request", fake_pr)
    published, pr_url, failed, committed = _publish_pull_request(
        workspace,
        load_project(tmp_path),
        "Make the adder work",
        "The adder returns the sum.",
        tests_passed=True,
        sha=None,
        committed=False,
        screenshot_files=[],
        token="ghs_test",
        settings=settings,
        store_root=tmp_path,
    )
    assert order == ["push", "pr"]
    assert failed is None
    assert committed is True
    assert published is True
    assert pr_url == "https://github.com/acme/repo/pull/3"


def test_a_runner_pull_request_is_titled_from_the_request(tmp_path: Path, monkeypatch) -> None:
    """A runner's summary is Claude Code's report, so it must not become the title."""
    from agent_lbw.runtime import improve

    assert improve.request_title("- upgrade python \n- rewrite the frontend in React") == (
        "upgrade python; rewrite the frontend in React"
    )
    assert improve.request_title("Fix the login page\n\nIt 500s on submit.") == (
        "Fix the login page"
    )
    seen: dict = {}

    def capture(workspace, project, goal, summary, **kwargs):
        seen.update(kwargs, summary=summary)
        return True, "https://github.com/acme/demo/pull/1", None, True

    monkeypatch.setattr(improve, "_publish_pull_request", capture)
    monkeypatch.setattr(improve, "_maybe_test", lambda *args, **kwargs: None)
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
    init_git_repo(tmp_path)
    improve.publish_runner_change(
        tmp_path,
        Settings(model_name="m", model_base_url="http://127.0.0.1:9/v1", create_pr=True),
        "- upgrade python\n- rewrite the frontend in React",
        "I couldn't run any tests this session. Neither uv sync nor npm ci worked.",
        base_sha=None,
        create_pr=True,
    )
    assert seen["title"] == "upgrade python; rewrite the frontend in React"
    assert seen["summary"].startswith("I couldn't run any tests")
