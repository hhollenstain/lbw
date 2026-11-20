from __future__ import annotations

from pathlib import Path

from agent_lbw.agent.prompts import SYSTEM_PROMPT
from agent_lbw.llm.client import AssistantTurn, ScriptedClient
from agent_lbw.progress import bind_progress, record_event, reset_progress
from agent_lbw.runtime.improve import _pr_body, _pr_screenshot_names, _review_goal
from agent_lbw.runtime.project import load_project
from agent_lbw.runtime.review import REVIEW_SYSTEM, review_goal
from agent_lbw.runtime.uireview import (
    UiEvidence,
    _from_eval,
    _playwright_probe,
    classify_network_failure,
    format_ui_evidence,
    is_generic_resource_console,
    resolve_ui_screenshot,
    ui_review_needed,
    unverified_interactive_ui,
)
from agent_lbw.sandbox import Workspace
from agent_lbw.tools import build_tools, execute_tool


def test_ui_review_needed_for_template_diff_and_ui_goals(tmp_path: Path) -> None:
    (tmp_path / "src" / "agent_lbw" / "templates").mkdir(parents=True)
    (tmp_path / "src" / "agent_lbw" / "templates" / "index.html").write_text(
        "<html></html>\n", encoding="utf-8"
    )
    assert ui_review_needed(
        "Add a progress bar",
        "diff --git a/src/agent_lbw/templates/index.html",
    )
    assert ui_review_needed("Add a progress bar", "", tmp_path)
    assert ui_review_needed("The game has no rendering and the start button does nothing", "")
    assert not ui_review_needed("update discord.py", "Pipfile.lock hashes changed")
    catalog = "diff --git a/.omp-plugin/marketplace.json b/.omp-plugin/marketplace.json\n"
    assert not ui_review_needed("Make the marketplace more full featured", catalog)
    assert ui_review_needed("Ship the widget", "diff --git a/web/app.js b/web/app.js\n")
    assert ui_review_needed("Ship the widget", "diff --git a/web/App.jsx b/web/App.jsx\n")


def test_format_ui_evidence_lists_errors_and_tiny_controls() -> None:
    text = format_ui_evidence(
        UiEvidence(
            ok=False,
            url="http://127.0.0.1:9/",
            title="lbw",
            page_errors=["label is not defined"],
            smashed=["Init (0x0)"],
            dead_controls=['[data-task-pane="screenshots"] did not show the screenshots pane'],
            snapshot="button: Queue task (120x32)",
            interactive=False,
        )
    )
    assert "label is not defined" in text
    assert "Init (0x0)" in text
    assert "Queue task" in text
    assert "did not show the screenshots pane" in text
    assert "static dump-dom" in text


def test_directory_listing_is_not_treated_as_the_application() -> None:
    evidence = _from_eval(
        "http://127.0.0.1:9/",
        {
            "title": "Directory listing for /",
            "text": "Directory listing for /\nindex.html\nsrc/",
            "errors": [],
            "elements": [{"tag": "a", "name": "src/", "w": 40, "h": 16}],
        },
        page_errors=[],
        console_errors=[],
        screenshot=None,
    )
    assert evidence.ok is False
    assert evidence.title == ""
    assert evidence.snapshot == ""
    assert "not evidence the goal is unmet" in evidence.notes
    text = format_ui_evidence(evidence)
    assert "Directory listing for /" not in text
    assert "not evidence the goal is unmet" in text


def test_from_eval_marks_js_errors_and_zero_size_controls() -> None:
    evidence = _from_eval(
        "http://127.0.0.1:9/",
        {
            "title": "lbw",
            "text": "Queue task",
            "errors": ["label is not defined"],
            "tabs": [
                {
                    "name": "Screenshots",
                    "pane": "screenshots",
                    "selected": False,
                    "panelHidden": True,
                }
            ],
            "elements": [
                {"tag": "button", "name": "Init", "w": 0, "h": 0},
                {"tag": "button", "name": "Queue task", "w": 120, "h": 32},
            ],
        },
        page_errors=[],
        console_errors=[],
        screenshot=None,
    )
    assert evidence.ok is False
    assert "label is not defined" in evidence.page_errors
    assert "Init (0x0)" in evidence.smashed
    assert "Queue task (120x32)" in evidence.snapshot
    assert "tab: Screenshots" in evidence.snapshot
    hidden = _from_eval(
        "http://127.0.0.1:9/",
        {
            "title": "lbw",
            "text": "Queue task",
            "errors": [],
            "elements": [
                {"tag": "button", "name": "Init", "w": 0, "h": 0, "hidden": True},
                {"tag": "button", "name": "Queue task", "w": 120, "h": 32},
            ],
        },
        page_errors=[],
        console_errors=[],
        screenshot=None,
    )
    assert hidden.ok is True
    assert hidden.smashed == []
    crushed_tab = _from_eval(
        "http://127.0.0.1:9/",
        {
            "title": "lbw",
            "text": "Past runs",
            "errors": [],
            "tabs": [
                {
                    "name": "Past runs",
                    "pane": "history",
                    "selected": True,
                    "panelHidden": False,
                    "panelHeight": 0,
                }
            ],
            "elements": [{"tag": "button", "name": "Past runs", "w": 90, "h": 32}],
        },
        page_errors=[],
        console_errors=[],
        screenshot=None,
    )
    assert crushed_tab.ok is False
    assert crushed_tab.dead_controls
    assert "Past runs" in crushed_tab.dead_controls[0]


def test_from_eval_marks_overlapping_rerun_and_line_stats() -> None:
    overlap = _from_eval(
        "http://127.0.0.1:9/",
        {
            "title": "lbw",
            "text": "Past runs",
            "errors": [],
            "elements": [
                {
                    "tag": "button",
                    "name": "↻ Rerun",
                    "cls": "rerun-btn",
                    "w": 72,
                    "h": 24,
                    "x": 16,
                    "y": 400,
                },
                {
                    "tag": "span",
                    "name": "+12 −3",
                    "cls": "history-line-stats",
                    "w": 48,
                    "h": 16,
                    "x": 20,
                    "y": 404,
                },
            ],
        },
        page_errors=[],
        console_errors=[],
        screenshot=None,
    )
    assert overlap.ok is False
    assert any("overlaps" in item for item in overlap.smashed)

    separated = _from_eval(
        "http://127.0.0.1:9/",
        {
            "title": "lbw",
            "text": "Past runs",
            "errors": [],
            "elements": [
                {
                    "tag": "button",
                    "name": "↻ Rerun",
                    "cls": "rerun-btn",
                    "w": 72,
                    "h": 24,
                    "x": 900,
                    "y": 400,
                },
                {
                    "tag": "span",
                    "name": "+12 −3",
                    "cls": "history-line-stats",
                    "w": 48,
                    "h": 16,
                    "x": 16,
                    "y": 400,
                },
            ],
        },
        page_errors=[],
        console_errors=[],
        screenshot=None,
    )
    assert separated.ok is True
    assert separated.smashed == []


def test_classify_network_failure_ignores_favicon_and_screenshots() -> None:
    assert classify_network_failure("http://127.0.0.1:9/favicon.ico", 404) == "ignore"
    assert classify_network_failure("http://127.0.0.1:9/apple-touch-icon.png", 404) == "ignore"
    assert (
        classify_network_failure("http://127.0.0.1:9/api/ui-screenshot?name=ui-review.png", 404)
        == "note"
    )
    assert classify_network_failure("http://127.0.0.1:9/static/app.js", 404) == "block"
    assert is_generic_resource_console(
        "Failed to load resource: the server responded with a status of 404 (Not Found)"
    )


def test_format_ui_evidence_keeps_optional_404s_out_of_js_errors() -> None:
    text = format_ui_evidence(
        UiEvidence(
            ok=True,
            url="http://127.0.0.1:9/",
            console_errors=[
                "Failed to load resource: the server responded with a status of 404 (Not Found)"
            ],
            network_notes=["404 http://127.0.0.1:9/api/ui-screenshot?name=ui-review.png"],
            snapshot="button: Queue task (120x32)",
        )
    )
    assert "Optional resources missing" in text
    assert "JavaScript errors:" not in text
    assert "ui-screenshot" in text


def test_review_goal_includes_rendered_ui_section() -> None:
    llm = ScriptedClient(
        [AssistantTurn(text='{"met": false, "reason": "progress stages have no labels"}')]
    )
    token = bind_progress()
    try:
        verdict = review_goal(
            llm,
            "Add a task stage progress bar",
            diff="--- a/index.html\n+++ b/index.html\n",
            summary="updated css",
            tests_passed=True,
            ui_evidence="Rendered UI:\nJavaScript errors:\n- label is not defined",
        )
    finally:
        reset_progress(token)
    assert verdict.met is False
    user = llm.calls[0][-1]["content"]
    assert "Rendered UI:" in user
    assert "label is not defined" in user


def test_review_goal_overrides_met_when_rendered_ui_throws(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "agent_lbw.runtime.improve.collect_ui_evidence",
        lambda *args, **kwargs: UiEvidence(
            ok=False,
            page_errors=["label is not defined"],
            smashed=["Init (0x0)"],
        ),
    )
    (tmp_path / ".lbw").mkdir()
    (tmp_path / ".lbw" / "config.yaml").write_text("name: fixture\n", encoding="utf-8")
    llm = ScriptedClient([AssistantTurn(text='{"met": true, "reason": "css layout looks fine"}')])
    token = bind_progress()
    try:
        verdict = _review_goal(
            Workspace(tmp_path),
            load_project(tmp_path),
            llm,
            "Add a progress bar",
            "diff --git a/src/agent_lbw/templates/index.html",
            "updated css",
            True,
        )
    finally:
        reset_progress(token)
    assert verdict.met is False
    assert "label is not defined" in verdict.reason


def test_review_goal_does_not_override_met_for_favicon_or_screenshot_404s(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "agent_lbw.runtime.improve.collect_ui_evidence",
        lambda *args, **kwargs: UiEvidence(
            ok=True,
            console_errors=[
                "Failed to load resource: the server responded with a status of 404 (Not Found)"
            ],
            network_notes=["404 http://127.0.0.1:9/favicon.ico"],
        ),
    )
    (tmp_path / ".lbw").mkdir()
    (tmp_path / ".lbw" / "config.yaml").write_text("name: fixture\n", encoding="utf-8")
    llm = ScriptedClient(
        [AssistantTurn(text='{"met": true, "reason": "pagination is at the top"}')]
    )
    token = bind_progress()
    try:
        verdict = _review_goal(
            Workspace(tmp_path),
            load_project(tmp_path),
            llm,
            "Add task pagination at the top",
            "diff --git a/src/agent_lbw/templates/index.html",
            "added top pagination",
            True,
        )
    finally:
        reset_progress(token)
    assert verdict.met is True


def test_review_goal_overrides_met_when_required_script_is_missing(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "agent_lbw.runtime.improve.collect_ui_evidence",
        lambda *args, **kwargs: UiEvidence(
            ok=False,
            network_failures=["404 http://127.0.0.1:9/static/app.js"],
        ),
    )
    (tmp_path / ".lbw").mkdir()
    (tmp_path / ".lbw" / "config.yaml").write_text("name: fixture\n", encoding="utf-8")
    llm = ScriptedClient(
        [AssistantTurn(text='{"met": true, "reason": "template includes the script tag"}')]
    )
    token = bind_progress()
    try:
        verdict = _review_goal(
            Workspace(tmp_path),
            load_project(tmp_path),
            llm,
            "Add a progress bar",
            "diff --git a/src/agent_lbw/templates/index.html",
            "added script",
            True,
        )
    finally:
        reset_progress(token)
    assert verdict.met is False
    assert "app.js" in verdict.reason


def test_review_goal_overrides_met_when_screenshot_tab_is_dead(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "agent_lbw.runtime.improve.collect_ui_evidence",
        lambda *args, **kwargs: UiEvidence(
            ok=False,
            dead_controls=['[data-task-pane="screenshots"] did not show the screenshots pane'],
        ),
    )
    (tmp_path / ".lbw").mkdir()
    (tmp_path / ".lbw" / "config.yaml").write_text("name: fixture\n", encoding="utf-8")
    llm = ScriptedClient(
        [AssistantTurn(text='{"met": true, "reason": "screenshots tab is in the template"}')]
    )
    token = bind_progress()
    try:
        verdict = _review_goal(
            Workspace(tmp_path),
            load_project(tmp_path),
            llm,
            "Show UI screenshots in another tab",
            "diff --git a/src/agent_lbw/templates/index.html",
            "added screenshots tab markup",
            True,
        )
    finally:
        reset_progress(token)
    assert verdict.met is False
    assert "did not show the screenshots pane" in verdict.reason


def test_review_goal_overrides_met_when_dump_dom_cannot_click_tabs(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "agent_lbw.runtime.improve.collect_ui_evidence",
        lambda *args, **kwargs: UiEvidence(
            ok=True,
            interactive=False,
            notes="Captured with Chrome --dump-dom",
        ),
    )
    (tmp_path / ".lbw").mkdir()
    (tmp_path / ".lbw" / "config.yaml").write_text("name: fixture\n", encoding="utf-8")
    llm = ScriptedClient(
        [AssistantTurn(text='{"met": true, "reason": "the screenshots tab exists in html"}')]
    )
    token = bind_progress()
    try:
        verdict = _review_goal(
            Workspace(tmp_path),
            load_project(tmp_path),
            llm,
            "Add a Screenshots tab next to Changes",
            "diff --git a/src/agent_lbw/templates/index.html",
            "added tab markup",
            True,
        )
    finally:
        reset_progress(token)
    assert verdict.met is False
    assert "dump-dom" in verdict.reason


def test_unverified_interactive_ui_requires_clicking_visible_screenshots_tab() -> None:
    reason = unverified_interactive_ui(
        "Show screenshots in a tab",
        UiEvidence(
            ok=True,
            snapshot="tab: Screenshots selected=False panelHidden=True",
            clicked=["#history-list button.task"],
        ),
    )
    assert reason is not None
    assert "Screenshots tab" in reason


def test_review_ui_tool_reports_capture(tmp_path: Path, monkeypatch) -> None:
    workspace = Workspace(tmp_path)
    (tmp_path / ".lbw").mkdir()
    (tmp_path / ".lbw" / "config.yaml").write_text("name: fixture\n", encoding="utf-8")
    monkeypatch.setattr(
        "agent_lbw.tools.browser.collect_ui_evidence",
        lambda *args, **kwargs: UiEvidence(
            ok=False,
            url="http://127.0.0.1:9/",
            page_errors=["label is not defined"],
            snapshot="progress-stage: Init (0x0)",
        ),
    )
    tools = build_tools(
        workspace,
        test_command=None,
        command_timeout_seconds=5,
        git_author_name=None,
        git_author_email=None,
    )
    result = execute_tool(tools, "review_ui", {})
    assert not result.ok
    assert "label is not defined" in result.output
    assert "Init (0x0)" in result.output


def test_agent_prompt_requires_review_ui_after_ui_edits() -> None:
    assert "review_ui" in SYSTEM_PROMPT
    assert "zero-size" in SYSTEM_PROMPT
    assert "Click new tabs" in SYSTEM_PROMPT
    assert "404" in SYSTEM_PROMPT
    assert "Finish the stated goal" in SYSTEM_PROMPT
    assert "later run can build on" not in SYSTEM_PROMPT
    assert "Markup for a Screenshots tab is not enough" in REVIEW_SYSTEM
    assert "0x0 or display:none" in REVIEW_SYSTEM
    assert "in a real implementation" in REVIEW_SYSTEM
    assert "owner/repo" in REVIEW_SYSTEM
    assert "directory listing" in REVIEW_SYSTEM
    assert "plan can name the wrong file" in REVIEW_SYSTEM


def test_resolve_ui_screenshot_serves_unique_and_legacy_pngs(tmp_path: Path) -> None:
    lbw = tmp_path / ".lbw"
    shots = lbw / "ui-screenshots"
    shots.mkdir(parents=True)
    unique = shots / "ui-review_goal_1.png"
    legacy = lbw / "ui-review.png"
    unique.write_bytes(b"unique")
    legacy.write_bytes(b"legacy")
    assert resolve_ui_screenshot(tmp_path, "ui-review_goal_1.png") == unique.resolve()
    assert resolve_ui_screenshot(tmp_path, "ui-review.png") == legacy.resolve()
    assert resolve_ui_screenshot(tmp_path, "../config.yaml") is None
    assert resolve_ui_screenshot(tmp_path, "missing.png") is None


def test_playwright_probe_marks_dead_and_working_tabs() -> None:
    class Locator:
        def __init__(self, count: int) -> None:
            self._count = count
            self.first = self

        def count(self) -> int:
            return self._count

        def click(self, timeout: int = 0) -> None:
            return None

    class Page:
        def __init__(self, *, hidden: bool, selected: str) -> None:
            self.hidden = hidden
            self.selected = selected

        def locator(self, _selector: str) -> Locator:
            return Locator(1)

        def get_by_role(self, *_args, **_kwargs) -> Locator:
            return Locator(0)

        def wait_for_timeout(self, _ms: int) -> None:
            return None

        def evaluate(self, _script: str, _payload: object = None) -> dict[str, object]:
            return {
                "selected": self.selected,
                "hidden": self.hidden,
                "display": "none" if self.hidden else "block",
                "visibility": "hidden" if self.hidden else "visible",
                "height": 0 if self.hidden else 120,
                "textLen": 0 if self.hidden else 40,
            }

    clicked, dead = _playwright_probe(
        Page(hidden=True, selected="false"),
        ['[data-task-pane="screenshots"]'],
        500,
    )
    assert clicked == ['[data-task-pane="screenshots"]']
    assert dead and "screenshots" in dead[0]

    clicked, dead = _playwright_probe(
        Page(hidden=False, selected="true"),
        ['[data-task-pane="screenshots"]'],
        500,
    )
    assert clicked
    assert dead == []

    clicked, dead = _playwright_probe(
        Page(hidden=False, selected="true"),
        ['[data-main-pane="history"]'],
        500,
    )
    assert clicked == ['[data-main-pane="history"]']
    assert dead == []


def test_playwright_probe_fails_when_pane_is_css_hidden() -> None:
    class Locator:
        def __init__(self) -> None:
            self.first = self

        def count(self) -> int:
            return 1

        def click(self, timeout: int = 0) -> None:
            return None

    class Page:
        def locator(self, _selector: str) -> Locator:
            return Locator()

        def get_by_role(self, *_args, **_kwargs) -> Locator:
            return Locator()

        def wait_for_timeout(self, _ms: int) -> None:
            return None

        def evaluate(self, _script: str, _payload: object = None) -> dict[str, object]:
            return {
                "selected": "true",
                "hidden": False,
                "display": "none",
                "visibility": "visible",
                "height": 0,
                "textLen": 0,
            }

    clicked, dead = _playwright_probe(
        Page(),
        ['[data-main-pane="history"]'],
        500,
    )
    assert clicked == ['[data-main-pane="history"]']
    assert dead and "history" in dead[0]
    assert "display=none" in dead[0]


def test_lbw_review_clicks_include_main_and_task_tabs(tmp_path: Path) -> None:
    from agent_lbw.runtime.uireview import _review_clicks

    (tmp_path / "src" / "agent_lbw" / "web_ui.py").parent.mkdir(parents=True)
    (tmp_path / "src" / "agent_lbw" / "web_ui.py").write_text("# lbw\n", encoding="utf-8")
    clicks = _review_clicks(tmp_path, '[data-task-pane="progress"]')
    assert '[data-status-filter="failed"]' in clicks
    assert '[data-status-filter=""]' in clicks
    assert '[data-task-pane="screenshots"]' in clicks
    assert '[data-task-pane="audit"]' in clicks
    assert '[data-task-pane="progress"]' in clicks
    assert clicks.index('[data-status-filter="failed"]') < clicks.index("#run-list button.task")
    assert clicks.index("#run-list button.task") < clicks.index('[data-task-pane="changes"]')
    assert clicks.index('[data-task-pane="screenshots"]') < clicks.index(
        '[data-task-pane="progress"]'
    )


def test_lbw_review_clicks_open_goal_panels_last(tmp_path: Path) -> None:
    from agent_lbw.runtime.uireview import _review_clicks

    (tmp_path / "src" / "agent_lbw" / "web_ui.py").parent.mkdir(parents=True)
    (tmp_path / "src" / "agent_lbw" / "web_ui.py").write_text("# lbw\n", encoding="utf-8")
    clicks = _review_clicks(
        tmp_path,
        None,
        "For overall user experience move the skills out of the settings cog",
    )
    assert clicks[-1] == '[data-rail="skills"]'
    assert "#open-settings" not in clicks
    assert '[data-task-pane="progress"]' in clicks
    assert clicks.index('[data-task-pane="progress"]') < clicks.index('[data-rail="skills"]')

    rule_clicks = _review_clicks(tmp_path, None, "Move rules into the left pane")
    assert rule_clicks[-1] == '[data-rail="rules"]'


def test_file_manager_goal_leaves_the_changes_pane_open(tmp_path: Path) -> None:
    from agent_lbw.runtime.uireview import _review_clicks

    (tmp_path / "src" / "agent_lbw" / "web_ui.py").parent.mkdir(parents=True)
    (tmp_path / "src" / "agent_lbw" / "web_ui.py").write_text("# lbw\n", encoding="utf-8")
    clicks = _review_clicks(
        tmp_path,
        None,
        "In the filemanager for changes there should be file type icons",
    )
    assert clicks[-1] == '[data-task-pane="changes"]'
    assert clicks.index('[data-task-pane="progress"]') < clicks.index('[data-task-pane="changes"]')


def test_unverified_interactive_ui_requires_skills_click() -> None:
    evidence = UiEvidence(
        ok=True,
        interactive=True,
        clicked=['[data-main-pane="current"]', "#task-list button.task"],
        snapshot="overlay: skills-panel hidden=True 0x0",
    )
    reason = unverified_interactive_ui(
        "Move the skills out of the settings cog into the left pane",
        evidence,
    )
    assert reason and "Skills" in reason


def test_unverified_interactive_ui_ignores_panels_the_page_does_not_have() -> None:
    skills = unverified_interactive_ui(
        "Enable workspace skills from the repo",
        UiEvidence(ok=True, interactive=True, snapshot="div: settings (200x40)"),
    )
    rules = unverified_interactive_ui(
        "Move guidelines into a settings menu",
        UiEvidence(ok=True, interactive=True, snapshot="div: settings (200x40)"),
    )
    assert skills is None
    assert rules is None


def test_unverified_interactive_ui_requires_rules_click_when_panel_is_present() -> None:
    reason = unverified_interactive_ui(
        "Move guidelines into a settings menu",
        UiEvidence(
            ok=True,
            interactive=True,
            snapshot="overlay: guidelines-panel hidden=True 0x0",
            clicked=['[data-main-pane="current"]'],
        ),
    )
    assert reason and "Rules" in reason


def test_playwright_probe_marks_dead_aria_controls_panel() -> None:
    class Locator:
        def __init__(self) -> None:
            self.first = self

        def count(self) -> int:
            return 1

        def click(self, timeout: int = 0) -> None:
            return None

    class Page:
        def locator(self, _selector: str) -> Locator:
            return Locator()

        def get_by_role(self, *_args, **_kwargs) -> Locator:
            return Locator()

        def wait_for_timeout(self, _ms: int) -> None:
            return None

        def evaluate(self, script: str, payload: object = None) -> object:
            if "aria-controls" in script and "getElementById" not in script:
                return "skills-panel"
            return {
                "missing": False,
                "expanded": "true",
                "hidden": True,
                "display": "none",
                "visibility": "hidden",
                "height": 0,
                "textLen": 0,
            }

    clicked, dead = _playwright_probe(Page(), ["#open-skills"], 500)
    assert clicked == ["#open-skills"]
    assert dead and "skills-panel" in dead[0]


def test_review_ui_tool_passes_cycle_goal(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_collect(*args, **kwargs):
        captured["goal"] = kwargs.get("goal") or (args[2] if len(args) > 2 else None)
        return UiEvidence(ok=True, snapshot="overlay: skills-panel hidden=False 320x640")

    monkeypatch.setattr("agent_lbw.tools.browser.collect_ui_evidence", fake_collect)
    workspace = Workspace(tmp_path)
    (tmp_path / ".lbw").mkdir()
    (tmp_path / ".lbw" / "config.yaml").write_text("name: fixture\n", encoding="utf-8")
    tools = build_tools(
        workspace,
        test_command=None,
        command_timeout_seconds=5,
        git_author_name=None,
        git_author_email=None,
        goal="Move the skills into the sidebar",
    )
    result = execute_tool(tools, "review_ui", {})
    assert result.ok
    assert captured["goal"] == "Move the skills into the sidebar"


def test_unverified_interactive_ui_requires_main_tab_clicks() -> None:
    evidence = UiEvidence(
        ok=True,
        interactive=True,
        clicked=['[data-task-pane="changes"]'],
        snapshot="tab: Past runs selected=false panelHidden=True",
    )
    reason = unverified_interactive_ui(
        "Move current tasks and past runs onto their own tabs",
        evidence,
    )
    assert reason and "workspace run list" in reason


def test_pr_screenshot_names_keeps_final_successful_change() -> None:
    token = bind_progress()
    try:
        record_event(
            kind="ui",
            ok=True,
            screenshot_filename="ui-review_review-ui_20260920_1.png",
        )
        record_event(
            kind="ui",
            ok=False,
            screenshot_filename="ui-review_goal_fail.png",
        )
        record_event(
            kind="ui",
            ok=True,
            screenshot_filename="ui-review_goal_a.png",
        )
        record_event(
            kind="ui",
            ok=True,
            screenshot_filename="ui-review_goal_b.png",
        )
        record_event(
            kind="ui",
            ok=True,
            screenshot_filename="ui-review_review-ui_20260920_2.png",
        )
        assert _pr_screenshot_names() == ["ui-review_goal_b.png"]
    finally:
        reset_progress(token)


def test_pr_screenshot_names_falls_back_to_last_successful_review() -> None:
    token = bind_progress()
    try:
        record_event(
            kind="ui",
            ok=True,
            screenshot_filename="ui-review_review-ui_one.png",
        )
        record_event(
            kind="ui",
            ok=False,
            screenshot_filename="ui-review_review-ui_bad.png",
        )
        record_event(
            kind="ui",
            ok=True,
            screenshot_filename="ui-review_review-ui_two.png",
        )
        assert _pr_screenshot_names() == ["ui-review_review-ui_two.png"]
    finally:
        reset_progress(token)


def test_pr_body_embeds_hosted_screenshot_urls() -> None:
    body = _pr_body(
        "Show screenshots",
        "added tab",
        tests_passed=True,
        commit_sha="abc",
        branch="lbw/x",
        screenshots=["ui-review_goal.png", "ui-review.png"],
        image_base="https://github.com/acme/repo/raw/abc",
    )
    assert (
        "![ui-review_goal.png](https://github.com/acme/repo/raw/abc/"
        ".lbw/ui-screenshots/ui-review_goal.png)"
    ) in body
    assert "![ui-review.png](https://github.com/acme/repo/raw/abc/.lbw/ui-review.png)" in body
    assert "](.lbw/ui-screenshots/" not in body


def test_pr_body_omits_unhosted_screenshots() -> None:
    body = _pr_body(
        "Show screenshots",
        "added tab",
        tests_passed=True,
        commit_sha="abc",
        branch="lbw/x",
        screenshots=["ui-review_goal.png"],
    )
    assert "## Screenshots" not in body
    assert ".lbw/ui-screenshots" not in body


def test_start_preview_uses_a_command_written_after_load(tmp_path: Path, monkeypatch) -> None:
    from agent_lbw.runtime.project import load_project, save_project_commands
    from agent_lbw.runtime.uireview import start_preview

    lbw = tmp_path / ".lbw"
    lbw.mkdir()
    (lbw / "config.yaml").write_text("name: demo\n", encoding="utf-8")
    project = load_project(tmp_path)
    assert project.preview_command is None
    save_project_commands(tmp_path, preview_command="python3 -m http.server {port}")
    seen: dict[str, str] = {}

    def fake_start(workspace, command, port):
        del workspace, port
        seen["command"] = command
        return None

    monkeypatch.setattr("agent_lbw.runtime.uireview._start_command_preview", fake_start)
    start_preview(Workspace(tmp_path), project)
    assert seen["command"] == "python3 -m http.server {port}"


def test_load_project_reads_preview_command(tmp_path: Path) -> None:
    lbw = tmp_path / ".lbw"
    lbw.mkdir()
    (lbw / "config.yaml").write_text(
        "preview_command: python3 -m http.server {port}\n",
        encoding="utf-8",
    )
    project = load_project(tmp_path)
    assert project.preview_command == "python3 -m http.server {port}"


def test_lbw_template_paths_use_the_running_app(tmp_path: Path, monkeypatch) -> None:
    from agent_lbw.runtime.project import load_project
    from agent_lbw.runtime.uireview import _is_lbw_template_path, start_preview

    (tmp_path / "src" / "agent_lbw" / "templates").mkdir(parents=True)
    (tmp_path / "src" / "agent_lbw" / "web_ui.py").write_text("# lbw\n", encoding="utf-8")
    template = tmp_path / "src" / "agent_lbw" / "templates" / "index.html"
    template.write_text("<html>{{ ws.name }}</html>\n", encoding="utf-8")
    workspace = Workspace(tmp_path)
    assert _is_lbw_template_path(workspace, "src/agent_lbw/templates/index.html")
    calls: list[str] = []

    def fake_lbw(ws, port):
        calls.append(f"lbw:{port}")

        class Preview:
            url = f"http://127.0.0.1:{port}/"

            def close(self) -> None:
                return None

        return Preview()

    monkeypatch.setattr("agent_lbw.runtime.uireview._start_lbw_preview", fake_lbw)
    preview = start_preview(
        workspace,
        load_project(tmp_path),
        path="src/agent_lbw/templates/index.html",
        port=9,
    )
    assert calls == ["lbw:9"]
    assert preview is not None
    assert preview.url == "http://127.0.0.1:9/"


def test_lbw_preview_ignores_a_static_file_server(tmp_path: Path, monkeypatch) -> None:
    from agent_lbw.runtime.project import load_project, save_project_commands
    from agent_lbw.runtime.uireview import start_preview

    (tmp_path / "src" / "agent_lbw").mkdir(parents=True)
    (tmp_path / "src" / "agent_lbw" / "web_ui.py").write_text("# lbw\n", encoding="utf-8")
    lbw = tmp_path / ".lbw"
    lbw.mkdir()
    (lbw / "config.yaml").write_text("name: lbw\n", encoding="utf-8")
    save_project_commands(tmp_path, preview_command="python3 -m http.server {port}")
    calls: list[str] = []

    def fake_lbw(ws, port):
        del ws
        calls.append(f"lbw:{port}")

        class Preview:
            url = f"http://127.0.0.1:{port}/"

            def close(self) -> None:
                return None

        return Preview()

    def fake_command(workspace, command, port):
        del workspace, port
        calls.append(f"command:{command}")
        return None

    monkeypatch.setattr("agent_lbw.runtime.uireview._start_lbw_preview", fake_lbw)
    monkeypatch.setattr("agent_lbw.runtime.uireview._start_command_preview", fake_command)
    preview = start_preview(Workspace(tmp_path), load_project(tmp_path), port=9)
    assert calls == ["lbw:9"]
    assert preview is not None
    assert preview.url == "http://127.0.0.1:9/"


def test_godot_preview_serves_the_captured_frame(tmp_path: Path, monkeypatch) -> None:
    import urllib.request

    from agent_lbw.runtime.uireview import start_preview

    (tmp_path / "project.godot").write_text('config/name="Demo"\n', encoding="utf-8")
    lbw = tmp_path / ".lbw"
    lbw.mkdir()
    (lbw / "config.yaml").write_text(
        'preview_command: echo "listening" && sleep 3600\n',
        encoding="utf-8",
    )
    fake = tmp_path / "fake-godot"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "movie = Path(args[args.index('--write-movie') + 1])\n"
        "movie.parent.mkdir(parents=True, exist_ok=True)\n"
        "(movie.parent / 'frame00000000.png').write_bytes(b'\\x89PNG\\r\\n')\n"
        "print('SCRIPT ERROR: Node not found: Player', file=sys.stderr)\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setattr("agent_lbw.runtime.uireview._godot_binary", lambda: str(fake))
    preview = start_preview(Workspace(tmp_path), load_project(tmp_path))
    assert preview is not None
    try:
        body = urllib.request.urlopen(preview.url, timeout=5).read().decode()
    finally:
        preview.close()
    assert "Node not found: Player" in body
    assert 'src="frame.png"' in body


def _solid_png(path: Path, width: int, height: int, spot: int = 0) -> None:
    import struct
    import zlib

    rows = b""
    for y in range(height):
        row = bytearray(b"\x00")
        for x in range(width):
            row += b"\xff\x00\x00" if x < spot and y < spot else b"\x4d\x4d\x4d"
        rows += bytes(row)

    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(kind + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def test_frame_blank_share_measures_a_single_color_frame(tmp_path: Path) -> None:
    from agent_lbw.runtime.uireview import frame_blank_share

    blank = tmp_path / "blank.png"
    _solid_png(blank, 20, 20)
    drawn = tmp_path / "drawn.png"
    _solid_png(drawn, 20, 20, spot=10)
    assert frame_blank_share(blank) == 1.0
    assert frame_blank_share(drawn) == 0.75


def test_godot_engine_errors_keep_the_location() -> None:
    from agent_lbw.runtime.uireview import godot_engine_errors

    output = (
        'ERROR: Node not found: "Player" (relative to "/root/MainScene").\n'
        "   at: get_node (scene/main/node.cpp:1976)\n"
        "SCRIPT ERROR: Invalid access to property or key 'position'.\n"
        "          at: MainGame._process (res://scripts/main_game.gd:60)\n"
        'ERROR: Node not found: "Player" (relative to "/root/MainScene").\n'
        "   at: get_node (scene/main/node.cpp:1976)\n"
        "Flappy Gary game initialized!\n"
        "ERROR: 1 resources still in use at exit (run with --verbose for details).\n"
        "   at: clear (core/io/resource.cpp:822)\n"
    )
    errors = godot_engine_errors(output)
    assert len(errors) == 2
    assert errors[0].endswith("(at get_node (scene/main/node.cpp:1976))")
    assert "res://scripts/main_game.gd:60" in errors[1]


def test_godot_preview_runs_the_scene_the_goal_names(tmp_path: Path, monkeypatch) -> None:
    from agent_lbw.runtime.uireview import start_preview

    (tmp_path / "project.godot").write_text('run/main_scene="res://scenes/menu.tscn"\n')
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "main_game.gd").write_text("extends Node2D\n")
    (tmp_path / "scenes").mkdir()
    (tmp_path / "scenes" / "menu.tscn").write_text('[gd_scene format=3]\n\n[node name="Menu"]\n')
    (tmp_path / "scenes" / "main_scene.tscn").write_text(
        "[gd_scene format=3]\n\n"
        '[node name="MainScene" type="Node2D"]\n\n'
        '[node name="Player" type="Sprite2D" parent="GameArea"]\n'
        "position = Vector2(100, 300)\n\n"
        '[node name="Logo" type="Sprite2D" parent="."]\n'
        'texture = ExtResource("2")\n'
    )
    args_file = tmp_path / "args.txt"
    fake = tmp_path / "fake-godot"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"Path({str(args_file)!r}).write_text(' '.join(sys.argv[1:]))\n"
        'print(\'ERROR: Node not found: "Player" (relative to "/root/MainScene").\')\n'
        "print('   at: get_node (scene/main/node.cpp:1976)')\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setattr("agent_lbw.runtime.uireview._godot_binary", lambda: str(fake))
    goal = 'Not rendering. ERROR: Node not found: "Player" (relative to "/root/MainScene").'
    preview = start_preview(Workspace(tmp_path), load_project(tmp_path), goal=goal)
    assert preview is not None
    preview.close()
    assert args_file.read_text().endswith("res://scenes/main_scene.tscn")
    assert preview.errors == [
        'ERROR: Node not found: "Player" (relative to "/root/MainScene"). '
        "(at get_node (scene/main/node.cpp:1976))"
    ]
    assert any('Sprite2D "Player"' in gap for gap in preview.gaps)
    assert not any("Logo" in gap for gap in preview.gaps)
    assert any("did not write a frame" in gap for gap in preview.gaps)
    assert preview.notes == "Godot ran res://scenes/main_scene.tscn for 8 frames."


def test_godot_preview_runs_the_level_instead_of_the_menu(tmp_path: Path, monkeypatch) -> None:
    from agent_lbw.runtime.uireview import start_preview

    (tmp_path / "project.godot").write_text('run/main_scene="res://scenes/menu.tscn"\n')
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "menu.gd").write_text(
        "func _on_start_pressed() -> void:\n"
        '\tget_tree().change_scene_to_file("res://scenes/main_scene.tscn")\n',
        encoding="utf-8",
    )
    (tmp_path / "scripts" / "game.gd").write_text(
        'func spawn_pipe():\n\tvar pipe = preload("res://scenes/pipe.tscn").instantiate()\n',
        encoding="utf-8",
    )
    (tmp_path / "scenes").mkdir()
    (tmp_path / "scenes" / "menu.tscn").write_text(
        "[gd_scene format=3]\n\n"
        '[ext_resource type="Script" path="res://scripts/menu.gd" id="1"]\n\n'
        '[node name="Menu" type="Control"]\n'
        'script = ExtResource("1")\n\n'
        '[node name="StartButton" type="Button" parent="."]\n'
        'text = "Start Game"\n',
        encoding="utf-8",
    )
    (tmp_path / "scenes" / "main_scene.tscn").write_text(
        "[gd_scene format=3]\n\n"
        '[ext_resource type="Script" path="res://scripts/game.gd" id="1"]\n\n'
        '[node name="MainScene" type="Node2D"]\n'
        'script = ExtResource("1")\n\n'
        '[node name="Player" type="Sprite2D" parent="."]\n',
        encoding="utf-8",
    )
    (tmp_path / "scenes" / "pipe.tscn").write_text(
        '[gd_scene format=3]\n\n[node name="Pipe" type="Node2D"]\n\n'
        '[node name="TopPipe" type="Sprite2D" parent="."]\n',
        encoding="utf-8",
    )
    args_file = tmp_path / "args.txt"
    fake = tmp_path / "fake-godot"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"Path({str(args_file)!r}).write_text(' '.join(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setattr("agent_lbw.runtime.uireview._godot_binary", lambda: str(fake))
    preview = start_preview(
        Workspace(tmp_path),
        load_project(tmp_path),
        goal="The player and pipes are a blank screen after you start",
    )
    assert preview is not None
    preview.close()
    command = args_file.read_text()
    assert command.endswith("res://scenes/main_scene.tscn")
    assert "--quit-after 120" in command
    assert any('Sprite2D "Player"' in gap for gap in preview.gaps)
    assert any('Sprite2D "TopPipe"' in gap for gap in preview.gaps)


def test_review_rejects_a_scene_that_still_prints_engine_errors(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "agent_lbw.runtime.improve.collect_ui_evidence",
        lambda *args, **kwargs: UiEvidence(
            ok=True,
            notes="Godot ran res://scenes/main_scene.tscn for 8 frames.",
            screenshot="frame.png",
            engine_errors=['ERROR: Node not found: "Player"'],
        ),
    )
    (tmp_path / ".lbw").mkdir()
    (tmp_path / ".lbw" / "config.yaml").write_text("name: fixture\n", encoding="utf-8")
    llm = ScriptedClient([AssistantTurn(text='{"met": true, "reason": "fixed the path"}')])
    token = bind_progress()
    try:
        verdict = _review_goal(
            Workspace(tmp_path),
            load_project(tmp_path),
            llm,
            'The scene is not rendering. ERROR: Node not found: "Player"',
            "diff --git a/scripts/main_game.gd b/scripts/main_game.gd\n",
            "Fixed it.",
            True,
        )
    finally:
        reset_progress(token)
    assert verdict.met is False
    assert verdict.reason.startswith("Running the scene still prints an engine error")
    assert "Engine errors while the scene ran:" in llm.calls[0][-1]["content"]


def test_review_rejects_a_render_goal_when_nodes_draw_nothing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "agent_lbw.runtime.improve.collect_ui_evidence",
        lambda *args, **kwargs: UiEvidence(
            ok=True,
            notes="Godot ran res://scenes/main_scene.tscn for 8 frames.",
            screenshot="frame.png",
            visual_gaps=['Sprite2D "Player" in scenes/main_scene.tscn has no texture'],
        ),
    )
    (tmp_path / ".lbw").mkdir()
    (tmp_path / ".lbw" / "config.yaml").write_text("name: fixture\n", encoding="utf-8")
    llm = ScriptedClient([AssistantTurn(text='{"met": true, "reason": "no more errors"}')])
    token = bind_progress()
    try:
        verdict = _review_goal(
            Workspace(tmp_path),
            load_project(tmp_path),
            llm,
            "The player and background are not rendering",
            "diff --git a/scripts/main_game.gd b/scripts/main_game.gd\n",
            "Fixed it.",
            True,
        )
    finally:
        reset_progress(token)
    assert verdict.met is False
    assert "draws nothing" in verdict.reason
    assert 'Sprite2D "Player"' in verdict.ui_errors[0]


def test_godot_preview_reports_a_missing_engine(tmp_path: Path, monkeypatch) -> None:
    from agent_lbw.runtime.uireview import start_preview

    (tmp_path / "project.godot").write_text('config/name="Demo"\n', encoding="utf-8")
    monkeypatch.setattr("agent_lbw.runtime.uireview._godot_binary", lambda: None)
    try:
        start_preview(Workspace(tmp_path), load_project(tmp_path))
    except RuntimeError as exc:
        assert "Godot was not found" in str(exc)
    else:
        raise AssertionError("expected a missing Godot error")


def test_review_goal_keeps_met_when_iteration_limit_but_ui_verified(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "agent_lbw.runtime.improve.collect_ui_evidence",
        lambda *args, **kwargs: UiEvidence(
            ok=True,
            snapshot="button: ↻ Rerun (72x24 @ 900,400)\nspan: +12 −3 (48x16 @ 16,400)",
            screenshot="ok.png",
            clicked=['[data-main-pane="history"]'],
        ),
    )
    (tmp_path / ".lbw").mkdir()
    (tmp_path / ".lbw" / "config.yaml").write_text("name: fixture\n", encoding="utf-8")
    llm = ScriptedClient(
        [AssistantTurn(text='{"met": true, "reason": "rerun no longer overlaps +/- stats"}')]
    )
    token = bind_progress()
    try:
        verdict = _review_goal(
            Workspace(tmp_path),
            load_project(tmp_path),
            llm,
            "Fix the overlapping of the past runs rerun button",
            "diff --git a/src/agent_lbw/templates/index.html",
            "Stopped after reaching the iteration limit.",
            True,
            stopped_reason="max_iterations",
        )
    finally:
        reset_progress(token)
    assert verdict.met is True
