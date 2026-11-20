"""The steps agent teams own, as a graph.

Each agent team owns one or more steps. A step plans, builds, or reviews, and
it can wait for other steps with ``after``. The proposal still moves plan,
then build, then review, with a person's pause between them. Inside a phase,
a step starts once every step it waits for is done, so steps that do not wait
on each other run side by side.

Build steps share one checkout, so they run one at a time in graph order. A
build step can own part of the plan with ``paths``: plan steps whose file
matches one of those globs go to it.

This module is plain Python so the graph rules can be checked without a
database or a model.
"""

from __future__ import annotations

import contextvars
import fnmatch
import re
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any

JOBS = ("plan", "build", "review")
_PHASE = {job: index for index, job in enumerate(JOBS)}
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_PARALLEL = 4


class GraphError(ValueError):
    """The steps do not form a graph the proposal can run."""


@dataclass(frozen=True)
class Step:
    key: str
    job: str
    team_id: str = ""
    team_name: str = ""
    instruction: str = ""
    model_id: str = ""
    after: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    position: int = 0
    extra: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    @property
    def title(self) -> str:
        return self.team_name or self.key


def step_key(value: str) -> str:
    """A step key from a team or step name: lower case, digits, and dashes."""
    key = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")[:64]
    return key.strip("-")


def clean_step(raw: dict[str, Any], *, fallback_key: str = "", position: int = 0) -> Step:
    """Check one step a person wrote. Raises GraphError with a readable reason."""
    key = step_key(str(raw.get("key") or fallback_key))
    if not key or not _KEY_RE.match(key):
        raise GraphError("a step needs a name of letters, digits, or dashes")
    job = str(raw.get("job") or "").strip().lower()
    if job not in _PHASE:
        raise GraphError(f"step {key} must plan, build, or review")
    after = tuple(dict.fromkeys(step_key(str(item)) for item in _strings(raw.get("after"))))
    if key in after:
        raise GraphError(f"step {key} cannot wait for itself")
    paths = tuple(
        dict.fromkeys(item.strip() for item in _strings(raw.get("paths")) if item.strip())
    )
    if paths and job != "build":
        raise GraphError(f"step {key}: only a build step owns paths")
    if len(paths) > 32 or any(len(item) > 200 for item in paths):
        raise GraphError(f"step {key}: keep paths to 32 globs of 200 characters")
    return Step(key=key, job=job, after=after, paths=paths, position=position)


def validate(steps: Sequence[Step], *, one_planner: bool = True) -> None:
    """Raise GraphError unless the steps form one runnable graph.

    Keys are unique, every ``after`` names a step, a step waits only for steps
    in its own phase or an earlier one, and nothing waits for itself through a
    loop. With ``one_planner`` the plan phase must also end in exactly one step
    that writes the plan; an organization's full set of teams skips that rule
    because each project turns on its own planners.
    """
    by_key: dict[str, Step] = {}
    for step in steps:
        if step.key in by_key:
            raise GraphError(f"two steps are named {step.key}")
        by_key[step.key] = step
    for step in steps:
        for name in step.after:
            other = by_key.get(name)
            if other is None:
                raise GraphError(f"step {step.key} waits for {name}, which is not a step")
            if _PHASE[other.job] > _PHASE[step.job]:
                raise GraphError(
                    f"step {step.key} ({step.job}) cannot wait for {name} ({other.job}); "
                    "a proposal plans, then builds, then reviews"
                )
    _order(steps)
    planners = [step for step in steps if step.job == "plan"]
    if planners and one_planner:
        waited_on = {name for step in planners for name in step.after}
        leads = [step.key for step in planners if step.key not in waited_on]
        if len(leads) != 1:
            raise GraphError(
                "the plan steps must end in one step that writes the plan; "
                f"make {', '.join(sorted(leads))} run before one of them"
            )


def prepare(steps: Sequence[Step]) -> list[Step]:
    """The steps one project turned on, ready to run.

    A wait on a step this project did not turn on is dropped. When more than
    one plan step ends the plan phase, the last of them writes the plan and
    waits for the others, which become its opinions. Raises GraphError for a
    loop or a wait on a later phase.
    """
    keys = {step.key for step in steps}
    kept = [
        replace(step, after=tuple(name for name in step.after if name in keys)) for step in steps
    ]
    validate(kept, one_planner=False)
    planners = [step for step in kept if step.job == "plan"]
    waited_on = {name for step in planners for name in step.after}
    leads = [step for step in ordered(kept, "plan") if step.key not in waited_on]
    if len(leads) > 1:
        lead = leads[-1]
        joined = tuple(dict.fromkeys([*lead.after, *(step.key for step in leads[:-1])]))
        kept = [replace(step, after=joined) if step.key == lead.key else step for step in kept]
    validate(kept)
    return kept


def generations(steps: Sequence[Step], job: str) -> list[list[Step]]:
    """Steps of one phase in waves. Every step in a wave can run at the same time.

    A step's wait on an earlier phase is already met when its phase starts.
    """
    phase = [step for step in steps if step.job == job]
    keys = {step.key for step in phase}
    waiting = {step.key: {name for name in step.after if name in keys} for step in phase}
    done: set[str] = set()
    waves: list[list[Step]] = []
    remaining = sorted(phase, key=_sort_key)
    while remaining:
        ready = [step for step in remaining if waiting[step.key] <= done]
        if not ready:
            raise GraphError(f"the {job} steps wait for each other in a loop")
        waves.append(ready)
        done.update(step.key for step in ready)
        remaining = [step for step in remaining if step.key not in done]
    return waves


def ordered(steps: Sequence[Step], job: str) -> list[Step]:
    """Steps of one phase in an order that respects every wait."""
    return [step for wave in generations(steps, job) for step in wave]


def lead_planner(steps: Sequence[Step]) -> Step | None:
    """The plan step whose plan the build follows."""
    order = ordered(steps, "plan")
    return order[-1] if order else None


def owner_of(file: str, builders: Sequence[Step]) -> Step | None:
    """The build step in charge of a file, by its paths, else the first without paths."""
    if not builders:
        return None
    name = (file or "").strip().removeprefix("./")
    if name:
        for step in builders:
            if any(_matches(name, pattern) for pattern in step.paths):
                return step
    for step in builders:
        if not step.paths:
            return step
    return builders[0]


def assign_plan_steps(files: Sequence[str], builders: Sequence[Step]) -> dict[str, list[int]]:
    """Plan step indexes (0-based) for each build step key, in plan order."""
    owned: dict[str, list[int]] = {step.key: [] for step in builders}
    for index, file in enumerate(files):
        owner = owner_of(file, builders)
        if owner is not None:
            owned[owner.key].append(index)
    return owned


def run_wave[T](
    items: Sequence[T],
    work: Callable[[T], Any],
    *,
    lane: Callable[[T], object] = id,
    max_workers: int = MAX_PARALLEL,
) -> list[Any]:
    """Run one wave and return results in the items' order.

    Items in the same lane run one after another; lanes run side by side.
    Steps that share one model client share a lane, so a single local model
    server is not asked to answer two prompts at once. Each lane runs in a copy
    of the caller's context, so progress events reach the running task.
    """
    if not items:
        return []
    lanes: dict[int, list[int]] = {}
    marks: dict[int, object] = {}
    for index, item in enumerate(items):
        mark = lane(item)
        slot = next((key for key, value in marks.items() if value is mark), None)
        if slot is None:
            slot = len(marks)
            marks[slot] = mark
            lanes[slot] = []
        lanes[slot].append(index)
    results: list[Any] = [None] * len(items)

    def run_lane(indexes: list[int]) -> None:
        for index in indexes:
            results[index] = work(items[index])

    if len(lanes) == 1:
        run_lane(next(iter(lanes.values())))
        return results
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(lanes)))) as pool:
        futures = [
            pool.submit(contextvars.copy_context().run, run_lane, indexes)
            for indexes in lanes.values()
        ]
        for future in futures:
            future.result()
    return results


def describe(steps: Sequence[Step]) -> list[dict[str, Any]]:
    """Waves per phase, for the UI and the CLI."""
    shown: list[dict[str, Any]] = []
    for job in JOBS:
        for number, wave in enumerate(generations(steps, job), start=1):
            shown.append(
                {
                    "job": job,
                    "wave": number,
                    "steps": [step.key for step in wave],
                    "parallel": len(wave) > 1 and job != "build",
                }
            )
    return shown


def _order(steps: Sequence[Step]) -> list[Step]:
    keys = {step.key for step in steps}
    waiting = {step.key: {name for name in step.after if name in keys} for step in steps}
    done: set[str] = set()
    out: list[Step] = []
    remaining = list(steps)
    while remaining:
        ready = [step for step in remaining if waiting[step.key] <= done]
        if not ready:
            stuck = ", ".join(sorted(step.key for step in remaining))
            raise GraphError(f"these steps wait for each other in a loop: {stuck}")
        out.extend(ready)
        done.update(step.key for step in ready)
        remaining = [step for step in remaining if step.key not in done]
    return out


def _sort_key(step: Step) -> tuple[int, str]:
    return (step.position, step.key)


def _matches(name: str, pattern: str) -> bool:
    pattern = pattern.strip().removeprefix("./")
    if pattern.endswith("/"):
        pattern += "**"
    if fnmatch.fnmatchcase(name, pattern):
        return True
    if pattern.endswith("/**"):
        return name.startswith(pattern[:-2])
    return False


def _strings(value: object) -> Iterable[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item for item in re.split(r"[,\n]", value) if item.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    raise GraphError("after and paths are lists")
