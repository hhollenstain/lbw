"""Bundled process skills that match Claude-style coding agents."""

from pathlib import Path

import yaml

from agent_lbw.runtime.project import write_default_project_files
from agent_lbw.runtime.skills import (
    bundled_skills_root,
    compose_system_prompt,
    enabled_skill_names,
    list_skills,
    save_enabled_skills,
)

DEFAULT_ENABLED = [
    "debug",
    "explore",
    "implement",
    "research",
    "review",
    "security",
    "polloco",
    "tdd",
    "ui",
    "style",
    "kiwi",
    "rules",
]


def test_init_enables_process_skills(tmp_path: Path) -> None:
    write_default_project_files(tmp_path)
    config = yaml.safe_load((tmp_path / ".lbw" / "config.yaml").read_text(encoding="utf-8"))
    enabled = config.get("skills", {}).get("enabled", [])
    assert enabled == DEFAULT_ENABLED
    assert enabled_skill_names(tmp_path) == DEFAULT_ENABLED


def test_process_skills_are_bundled_and_injected(tmp_path: Path) -> None:
    expected = {
        "debug": ("Reproduce", "run_tests"),
        "explore": ("Match what is here", "search_text"),
        "review": ("Spec", "unused helper"),
        "security": ("Untrusted input", "secrets"),
        "polloco": ("review_changes", "Do not ask the user"),
        "rules": ("write_rule", "another organization"),
    }
    skills = {item.name: item for item in list_skills(tmp_path)}
    write_default_project_files(tmp_path)
    for name, phrases in expected.items():
        path = bundled_skills_root() / name / "SKILL.md"
        assert path.is_file(), name
        assert name in skills
        assert skills[name].origin == "bundled"
        body = skills[name].body.lower()
        for phrase in phrases:
            assert phrase.lower() in body, f"{name}: {phrase}"
        save_enabled_skills(tmp_path, [name])
        prompt = compose_system_prompt(tmp_path)
        assert f"### {name}" in prompt
        assert phrases[0] in prompt or phrases[0].lower() in prompt.lower()
    steward = bundled_skills_root() / "steward" / "SKILL.md"
    assert steward.is_file()
    save_enabled_skills(tmp_path, ["steward"])
    prompt = compose_system_prompt(tmp_path)
    assert "### steward" in prompt
    assert "Keep the product closer" in prompt
    save_enabled_skills(tmp_path, [])
    forced = compose_system_prompt(tmp_path, extra_names=("steward",))
    assert "### steward" in forced
