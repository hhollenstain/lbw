"""Spec-driven proposals: write the spec a proposal starts from.

The spec's shape and the rules for writing it are in the bundled `sdd` skill.
This module asks the model for a spec, names its file in `specs/`, reads its
acceptance criteria back, and writes it into the checkout so it is committed
with the change.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from agent_lbw.llm.client import LLMClient
from agent_lbw.progress import timed_complete
from agent_lbw.runtime.skills import bundled_skills_root, parse_skill_markdown

SPEC_DIR = "specs"
SPEC_LIMIT = 40_000
_CONTEXT_LIMIT = 24_000
_FENCE = re.compile(r"\A```(?:markdown|md)?\s*\n(.*?)\n```\s*\Z", re.DOTALL | re.IGNORECASE)
_HEADING = re.compile(r"^#{2,}\s+(.*?)\s*$")
_ITEM = re.compile(r"^\s*[-*]\s+(?:\[[ xX]\]\s+)?(.+?)\s*$")
_SLUG = re.compile(r"[^a-z0-9]+")


class SpecError(RuntimeError):
    """The model did not return a spec."""


def spec_instructions() -> str:
    """The `sdd` skill, which says how to write a spec and what shape it takes."""
    path = bundled_skills_root() / "sdd" / "SKILL.md"
    _name, _description, body = parse_skill_markdown(
        path.read_text(encoding="utf-8"), fallback_name="sdd"
    )
    return body


def write_spec(
    llm: LLMClient,
    goal: str,
    *,
    context: str = "",
    files: str = "",
    previous: str = "",
    note: str = "",
) -> str:
    """Ask the model for the spec. A note from a reviewer revises the previous spec."""
    parts = [f"Request:\n{goal.strip()}"]
    if context.strip():
        parts.append(f"Project:\n{context.strip()[:_CONTEXT_LIMIT]}")
    if files.strip():
        parts.append(f"Files named in the request:\n{files.strip()[:_CONTEXT_LIMIT]}")
    if previous.strip() and note.strip():
        parts.append(f"Your previous spec:\n{previous.strip()[:SPEC_LIMIT]}")
        parts.append(f"The reviewer sent it back with this note:\n{note.strip()[:4000]}")
    turn = timed_complete(
        llm,
        [
            {"role": "system", "content": spec_instructions()},
            {"role": "user", "content": "\n\n".join(parts)},
        ],
        [],
        purpose="spec",
    )
    text = clean_spec(turn.text or "")
    if not text:
        raise SpecError("the model did not write a spec")
    return text


def clean_spec(text: str) -> str:
    """The spec without a wrapping code fence, trimmed to the size LBW keeps."""
    body = (text or "").replace("\r\n", "\n").strip()
    fenced = _FENCE.match(body)
    if fenced:
        body = fenced.group(1).strip()
    return body[:SPEC_LIMIT].strip()


def spec_path_for(goal: str, *, now: datetime | None = None) -> str:
    """Where a new spec is committed: `specs/<date>-<slug>.md`."""
    first = next((line.strip() for line in (goal or "").splitlines() if line.strip()), "")
    slug = _SLUG.sub("-", first.lower()).strip("-")[:48].strip("-") or "change"
    day = (now or datetime.now(UTC)).strftime("%Y-%m-%d")
    return f"{SPEC_DIR}/{day}-{slug}.md"


def acceptance_criteria(spec: str) -> list[str]:
    """The items under the spec's "Acceptance criteria" heading."""
    found: list[str] = []
    inside = False
    for line in (spec or "").splitlines():
        heading = _HEADING.match(line)
        if heading:
            inside = heading.group(1).strip().lower().startswith("acceptance criteria")
            continue
        if not inside:
            continue
        item = _ITEM.match(line)
        if item:
            found.append(item.group(1))
    return found[:20]


def save_spec(root: Path, path: str, text: str) -> Path:
    """Write the spec into the checkout. The path must stay inside `specs/`."""
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] != (SPEC_DIR,):
        raise ValueError(f"a spec lives in {SPEC_DIR}/")
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(clean_spec(text) + "\n", encoding="utf-8")
    return target


def spec_context(path: str, text: str) -> str:
    """The spec as the planner, builder, and reviewers read it."""
    return (
        f"Spec ({path}). This is the source of truth for the change. "
        "Build exactly what it requires, and prove each acceptance criterion.\n\n"
        f"{clean_spec(text)}"
    )
