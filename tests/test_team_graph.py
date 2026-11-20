"""Agent team steps form a graph: what runs together, and what waits."""

from __future__ import annotations

import contextvars
import threading
import time

import pytest

from agent_lbw.runtime.team_graph import (
    GraphError,
    Step,
    assign_plan_steps,
    clean_step,
    describe,
    generations,
    lead_planner,
    ordered,
    owner_of,
    prepare,
    run_wave,
    step_key,
    validate,
)


def _step(key: str, job: str, *after: str, paths: tuple[str, ...] = ()) -> Step:
    return Step(key=key, job=job, after=tuple(after), paths=paths)


def test_steps_that_do_not_wait_run_in_the_same_wave() -> None:
    steps = [
        _step("architect", "plan"),
        _step("builder", "build"),
        _step("qa", "review"),
        _step("security", "review"),
        _step("release", "review", "qa", "security"),
    ]
    validate(steps)
    waves = generations(steps, "review")
    assert [[step.key for step in wave] for wave in waves] == [["qa", "security"], ["release"]]
    assert describe(steps)[-2] == {
        "job": "review",
        "wave": 1,
        "steps": ["qa", "security"],
        "parallel": True,
    }


def test_a_loop_is_refused() -> None:
    steps = [_step("a", "review", "b"), _step("b", "review", "a")]
    with pytest.raises(GraphError, match="loop"):
        validate(steps)


def test_a_step_cannot_wait_for_a_later_phase() -> None:
    steps = [_step("plan", "plan", "qa"), _step("qa", "review")]
    with pytest.raises(GraphError, match="plans, then builds, then reviews"):
        validate(steps)


def test_an_unknown_wait_is_refused() -> None:
    with pytest.raises(GraphError, match="not a step"):
        validate([_step("qa", "review", "ghost")])


def test_plan_steps_end_in_one_lead_planner() -> None:
    two_leads = [_step("arch", "plan"), _step("threats", "plan")]
    with pytest.raises(GraphError, match="one step that writes the plan"):
        validate(two_leads)
    joined = [_step("arch", "plan", "threats"), _step("threats", "plan")]
    validate(joined)
    assert lead_planner(joined).key == "arch"
    assert [step.key for step in ordered(joined, "plan")] == ["threats", "arch"]


def test_build_steps_own_plan_steps_by_path() -> None:
    builders = [
        _step("backend", "build", paths=("src/**",)),
        _step("frontend", "build", paths=("web/src/",)),
        _step("general", "build"),
    ]
    assert owner_of("web/src/main.tsx", builders).key == "frontend"
    assert owner_of("./src/agent_lbw/cli.py", builders).key == "backend"
    assert owner_of("README.md", builders).key == "general"
    assert owner_of(".github/workflows/ci.yml", builders).key == "general"
    owned = assign_plan_steps(["src/a.py", "README.md", "web/src/x.tsx", "src/b.py"], builders)
    assert owned == {"backend": [0, 3], "frontend": [2], "general": [1]}


def test_unmatched_plan_steps_go_to_the_first_builder_when_all_have_paths() -> None:
    builders = [_step("backend", "build", paths=("src/**",))]
    assert assign_plan_steps(["docs/a.md"], builders) == {"backend": [0]}


def test_clean_step_checks_what_a_person_wrote() -> None:
    step = clean_step(
        {"key": "Front End", "job": "BUILD", "after": "plan, plan", "paths": ["web/**"]}
    )
    assert step == Step(key="front-end", job="build", after=("plan",), paths=("web/**",))
    with pytest.raises(GraphError, match="plan, build, or review"):
        clean_step({"key": "x", "job": "ship"})
    with pytest.raises(GraphError, match="only a build step owns paths"):
        clean_step({"key": "x", "job": "review", "paths": ["src/**"]})
    with pytest.raises(GraphError, match="itself"):
        clean_step({"key": "x", "job": "review", "after": ["x"]})
    assert step_key("  Plan desk! ") == "plan-desk"


def test_steps_on_different_clients_run_side_by_side() -> None:
    marker: contextvars.ContextVar[str] = contextvars.ContextVar("marker", default="")
    marker.set("task-1")
    seen: list[str] = []
    running = 0
    peak = 0
    lock = threading.Lock()

    def work(item: tuple[str, object]) -> str:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.05)
        with lock:
            running -= 1
            seen.append(marker.get())
        return item[0]

    first, second = object(), object()
    items = [("qa", first), ("security", second), ("quality", first)]
    out = run_wave(items, work, lane=lambda item: item[1])
    assert out == ["qa", "security", "quality"]
    assert peak == 2
    assert seen == ["task-1"] * 3


def test_steps_on_one_client_run_one_after_another() -> None:
    order: list[str] = []
    shared = object()
    out = run_wave(
        [("a", shared), ("b", shared)],
        lambda item: order.append(item[0]) or item[0],
        lane=lambda item: item[1],
    )
    assert out == ["a", "b"]
    assert order == ["a", "b"]


def test_a_project_drops_waits_on_steps_it_did_not_turn_on() -> None:
    steps = [_step("arch", "plan"), _step("qa", "review", "security")]
    ready = prepare(steps)
    assert [step.after for step in ready] == [(), ()]


def test_a_project_with_two_planners_lets_the_last_write_the_plan() -> None:
    steps = [_step("arch", "plan"), _step("threats", "plan"), _step("builder", "build")]
    with pytest.raises(GraphError):
        validate(steps)
    validate(steps, one_planner=False)
    ready = prepare(steps)
    assert lead_planner(ready).key == "threats"
    assert [[step.key for step in wave] for wave in generations(ready, "plan")] == [
        ["arch"],
        ["threats"],
    ]
