"""Instruction files that live in the repository.

The body stays in git. LBW reads it from the primary branch and records only
which files this workspace has turned off.
"""

from __future__ import annotations

import json
from pathlib import Path

KNOWN_REPO_RULES = (
    "AGENTS.md",
    "CLAUDE.md",
    "CURSOR.md",
    "GEMINI.md",
    "COPILOT.md",
    ".github/copilot-instructions.md",
)
_PREFS = ".lbw/repo-rules.json"
_FILE_CAP = 12_000
_PROMPT_CAP = 24_000


def list_repo_rules(root: Path) -> dict:
    """Files present on the primary branch, and whether this workspace uses each one."""
    place = Path(root).expanduser().resolve()
    branch = _primary_branch(place)
    disabled = _disabled(place)
    files = []
    for rel in KNOWN_REPO_RULES:
        body = _read(place, branch, rel)
        if body is None:
            continue
        clipped = _clip(body, _FILE_CAP)
        files.append(
            {
                "path": rel,
                "enabled": rel not in disabled,
                "body": clipped,
                "html": _html(clipped),
            }
        )
    return {"branch": branch, "files": files}


def set_repo_rule(root: Path, path: str, *, enabled: bool) -> dict:
    """Turn one known instruction file on or off for this workspace."""
    rel = path.strip()
    if rel not in KNOWN_REPO_RULES:
        raise ValueError("that file is not a repository instruction")
    place = Path(root).expanduser().resolve()
    disabled = _disabled(place)
    if enabled:
        disabled.discard(rel)
    else:
        disabled.add(rel)
    _write_disabled(place, disabled)
    return list_repo_rules(place)


def text_for_prompt(root: Path, *, line_limit: int = 0) -> str:
    """Enabled instruction files, for a cycle. A turned-off file is left out."""
    listed = list_repo_rules(root)
    chosen = [item for item in listed["files"] if item["enabled"] and item["body"].strip()]
    if not chosen:
        return ""
    branch = listed["branch"] or "the project folder"
    lines = [f"Repository instructions from {branch}:"]
    lines.append("A file that is not listed here was turned off. Do not read it back in.")
    for item in chosen:
        body = item["body"]
        if line_limit > 0:
            body = "\n".join(body.splitlines()[:line_limit])
        lines.append(f"### {item['path']}")
        lines.append(body.strip())
    text = "\n".join(lines)
    if len(text) <= _PROMPT_CAP:
        return text
    return text[:_PROMPT_CAP].rstrip() + "\n…"


def _html(body: str) -> str:
    from agent_lbw.runtime.skills import render_skill_html

    return render_skill_html(body)


def _primary_branch(root: Path) -> str:
    if not (root / ".git").exists():
        return ""
    from agent_lbw.sandbox import Workspace
    from agent_lbw.tools.git import default_base_branch

    return default_base_branch(Workspace(root)) or ""


def _read(root: Path, branch: str, rel: str) -> str | None:
    target = (root / rel).resolve()
    if not target.is_relative_to(root):
        return None
    if branch and (root / ".git").exists():
        from agent_lbw.sandbox import Workspace
        from agent_lbw.tools.git import run_git

        shown = run_git(Workspace(root), ["show", f"{branch}:{rel}"])
        if shown.returncode != 0:
            return None
        return shown.stdout
    if not target.is_file():
        return None
    try:
        return target.read_text(encoding="utf-8")
    except OSError:
        return None


def _disabled(root: Path) -> set[str]:
    path = root / _PREFS
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    raw = payload.get("disabled") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return set()
    return {item for item in raw if item in KNOWN_REPO_RULES}


def _write_disabled(root: Path, disabled: set[str]) -> None:
    path = root / _PREFS
    path.parent.mkdir(parents=True, exist_ok=True)
    kept = [rel for rel in KNOWN_REPO_RULES if rel in disabled]
    path.write_text(json.dumps({"disabled": kept}, indent=2) + "\n", encoding="utf-8")


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n…"
