from __future__ import annotations

from pathlib import Path

from tests.support import init_git_repo

from agent_lbw.agent.prompts import SYSTEM_PROMPT, user_prompt
from agent_lbw.runtime.project import collect_context, load_project, write_default_project_files
from agent_lbw.runtime.skills import (
    add_skill_source,
    bundled_skills_root,
    compose_system_prompt,
    enabled_skill_names,
    format_enabled_skills,
    list_skills,
    parse_skill_markdown,
    render_skill_html,
    save_enabled_skills,
    skill_documents,
    skill_markdown_for_kiwi,
    sources_root,
    sync_skill_source,
)


def _skill_repo(root: Path, *, name: str = "code-review") -> Path:
    remote = root / "skill-origin"
    skill = remote / "skills" / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        (
            "---\n"
            f"name: {name}\n"
            "description: Review the public API.\n"
            "---\n\n"
            "Check the HTTP interface, not private helpers.\n"
        ),
        encoding="utf-8",
    )
    init_git_repo(remote)
    return remote


def test_parse_skill_markdown_reads_frontmatter() -> None:
    name, description, body = parse_skill_markdown(
        "---\nname: TDD\ndescription: Red then green.\n---\n\n# Loop\nWrite a failing test.\n",
        fallback_name="other",
    )
    assert name == "tdd"
    assert description == "Red then green."
    assert "failing test" in body


def test_skill_markdown_quotes_a_colon_for_kiwi(tmp_path: Path) -> None:
    raw = (
        "---\nname: review\ndescription: Use when: the change is ready\n---\n\nLook at the diff.\n"
    )
    stored = skill_markdown_for_kiwi(raw)
    name, description, body = parse_skill_markdown(stored, fallback_name="review")
    assert name == "review"
    assert description == "Use when: the change is ready"
    assert "Look at the diff." in body
    assert "description: Use when: the change is ready\n" not in stored

    folded = "---\nname: review\ndescription: >-\n  Use when: the change is ready\n---\n\nBody.\n"
    assert skill_markdown_for_kiwi(folded) == folded

    skill = tmp_path / "skills" / "review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(raw, encoding="utf-8")
    documents = skill_documents(tmp_path)
    assert documents[0][0] == "skills/review/SKILL.md"
    assert documents[0][1] == stored


def test_render_skill_html_formats_markdown() -> None:
    html = render_skill_html("# Loop\n\nWrite a **failing** test and call `run_tests`.\n")
    assert "<h1>Loop</h1>" in html
    assert "<strong>failing</strong>" in html
    assert "<code>run_tests</code>" in html
    escaped = render_skill_html("<script>alert(1)</script>\n[xss](javascript:alert(1))\n")
    assert "<script>" not in escaped
    assert 'href="javascript:' not in escaped
    assert "&lt;script&gt;" in escaped
    assert render_skill_html("   ") == ""


def test_skill_summary_uses_first_sentence() -> None:
    from agent_lbw.runtime.skills import skill_summary

    assert skill_summary("") == "No description."
    assert skill_summary("Test-driven development. Use when fixing bugs.") == (
        "Test-driven development."
    )
    long_one = "A " + ("very " * 20) + "long first sentence without a period"
    clipped = skill_summary(long_one)
    assert clipped.endswith("…")
    assert len(clipped) <= 72


def test_bundled_style_skill_is_discoverable(tmp_path: Path) -> None:
    skill_path = bundled_skills_root() / "style" / "SKILL.md"
    assert skill_path.is_file()
    skills = {item.name: item for item in list_skills(tmp_path)}
    assert "style" in skills
    assert skills["style"].origin == "bundled"
    body = skills["style"].body.lower()
    assert "when an engineer changes the style" in body
    assert "themes.css" in body
    assert "updates this skill in" in body
    assert "same change" in body


def test_bundled_ui_skill_is_discoverable(tmp_path: Path) -> None:
    skill_path = bundled_skills_root() / "ui" / "SKILL.md"
    assert skill_path.is_file()
    skills = {item.name: item for item in list_skills(tmp_path)}
    assert "ui" in skills
    assert skills["ui"].origin == "bundled"
    assert "overlap" in skills["ui"].body.lower()
    assert "review_ui" in skills["ui"].body


def test_bundled_tdd_skill_is_discoverable(tmp_path: Path) -> None:
    skill_path = bundled_skills_root() / "tdd" / "SKILL.md"
    assert skill_path.is_file()
    skills = {item.name: item for item in list_skills(tmp_path)}
    assert "tdd" in skills
    assert skills["tdd"].origin == "bundled"
    assert skills["tdd"].enabled is False
    assert "red" in skills["tdd"].body.lower()
    listed = skills["tdd"].public_dict()
    assert listed["summary"] == "Test-driven development."
    assert "body" not in listed


def test_enable_skill_injects_into_system_prompt_and_context(tmp_path: Path) -> None:
    write_default_project_files(tmp_path)
    assert "tdd" in enabled_skill_names(tmp_path)
    prompt = compose_system_prompt(tmp_path)
    assert "Enabled skills" in prompt
    assert "### tdd" in prompt
    assert "red" in prompt.lower()
    project = load_project(tmp_path)
    context = collect_context(tmp_path, project)
    assert "Enabled skills:" in context
    assert "debug, explore, implement, research" in context
    assert "review, security, polloco, tdd, ui, style, kiwi, rules" in context
    save_enabled_skills(tmp_path, [])
    assert enabled_skill_names(tmp_path) == []
    assert "Enabled skills" not in compose_system_prompt(tmp_path)
    assert "Enabled skills" not in collect_context(tmp_path, load_project(tmp_path))


def test_clone_and_pull_skill_source_into_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "app"
    workspace.mkdir()
    remote = _skill_repo(tmp_path)
    source = add_skill_source(workspace, str(remote))
    cloned = Path(source.path)
    assert cloned.is_dir()
    assert cloned == sources_root(workspace) / source.slug
    assert (cloned / "skills" / "code-review" / "SKILL.md").is_file()
    names = {item.name: item for item in list_skills(workspace)}
    assert names["code-review"].origin == "source"
    assert names["code-review"].enabled is False
    save_enabled_skills(workspace, ["code-review"])
    assert "HTTP interface" in format_enabled_skills(workspace)

    extra = remote / "skills" / "docs"
    extra.mkdir()
    (extra / "SKILL.md").write_text(
        "---\nname: docs\ndescription: Write docs.\n---\n\nKeep README accurate.\n",
        encoding="utf-8",
    )
    init_git_repo(remote)
    synced = sync_skill_source(workspace, source.slug)
    assert synced.slug == source.slug
    assert "docs" in {item.name for item in list_skills(workspace)}


def test_local_skill_overrides_bundled_name(tmp_path: Path) -> None:
    local = tmp_path / ".lbw" / "skills" / "local" / "tdd"
    local.mkdir(parents=True)
    (local / "SKILL.md").write_text(
        "---\nname: tdd\ndescription: Workspace TDD.\n---\n\nUse the workspace test command.\n",
        encoding="utf-8",
    )
    save_enabled_skills(tmp_path, ["tdd"])
    skills = {item.name: item for item in list_skills(tmp_path)}
    assert skills["tdd"].origin == "local"
    assert "workspace test command" in compose_system_prompt(tmp_path)


def test_default_prompts_do_not_hardcode_tdd() -> None:
    assert "Follow enabled workspace skills" in SYSTEM_PROMPT
    assert "red → green" not in SYSTEM_PROMPT
    prompt = user_prompt("Fix the adder", "")
    assert "enabled workspace skills" in prompt.lower()
    assert "one failing test at the public seam" not in prompt.lower()


def test_discover_repo_skills_from_git_repo_root(tmp_path: Path) -> None:
    from agent_lbw.runtime.skills import list_skills

    # Create a repo with a SKILL.md file but no .lbw directory
    workspace = tmp_path / "my_project"
    workspace.mkdir()

    skill_path = workspace / "SKILL.md"
    skill_path.write_text(
        "---\nname: repo-tdd\ndescription: Repo-specific TDD.\n---\n\nWrite tests first.\n",
        encoding="utf-8",
    )

    init_git_repo(workspace)

    skills = {item.name: item for item in list_skills(workspace)}
    assert "repo-tdd" in skills
    assert skills["repo-tdd"].origin == "repo"
    assert skills["repo-tdd"].description == "Repo-specific TDD."

    save_enabled_skills(workspace, ["repo-tdd"])
    assert "repo-tdd" in enabled_skill_names(workspace)


def test_repo_skill_is_injected_when_not_enabled(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    skill = tmp_path / "skills" / "agent-lbw"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: agent-lbw\ndescription: Conventions.\n---\n\n"
        "Run ruff format then ruff check before finishing.\n",
        encoding="utf-8",
    )
    save_enabled_skills(tmp_path, [])
    prompt = compose_system_prompt(tmp_path)
    assert "### agent-lbw" in prompt
    assert "ruff format" in prompt.lower()


def test_agent_lbw_repo_skill_is_discoverable() -> None:
    root = Path(__file__).resolve().parents[1]
    skill_path = root / "skills" / "agent-lbw" / "SKILL.md"
    assert skill_path.is_file()
    skills = {item.name: item for item in list_skills(root)}
    assert "agent-lbw" in skills
    assert skills["agent-lbw"].origin == "repo"
    assert skills["tdd"].origin != "repo"
    assert "public seam" in skills["agent-lbw"].body.lower()
    assert "ruff format" in skills["agent-lbw"].body.lower()
    listed = skills["agent-lbw"].public_dict()
    assert listed["summary"] == "Conventions for changing this agent-lbw repository."
    assert "body" not in listed
    prompt = compose_system_prompt(root)
    assert "### agent-lbw" in prompt
    assert "ruff format" in prompt.lower()


def test_discover_repo_skills_skips_lbw_subdirectories(tmp_path: Path) -> None:
    """Skills discovery should skip the .lbw subdirectory in the repo."""
    from agent_lbw.runtime.skills import list_skills

    workspace = tmp_path / "my_workspace"
    workspace.mkdir()

    # Add .lbw subdirectory
    lbws_dir = workspace / ".lbw"
    lbws_dir.mkdir()

    # Add SKILL.md in .lbw - should not be discovered
    (lbws_dir / "SKILL.md").write_text(
        "---\nname: should-not-discover\ndescription: Should not be discovered.\n---\n",
        encoding="utf-8",
    )

    # Add SKILL.md at root - should be discovered
    skill_path = workspace / "SKILL.md"
    skill_path.write_text(
        "---\nname: should-discover\ndescription: Should be discovered.\n---\n",
        encoding="utf-8",
    )

    init_git_repo(workspace)

    skills = {item.name: item for item in list_skills(workspace)}
    assert "should-discover" in skills
    assert skills["should-discover"].origin == "repo"
    assert "should-not-discover" not in skills
