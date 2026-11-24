"""Where the next run of a failed proposal can start, like re-running failed jobs."""

from __future__ import annotations

RESTART_PHASES = ("build", "publish")


def restart_phase(*, status: str, decision: str, plan: str, branch: str, agent_kind: str) -> str:
    """The phase a failed run can start the next run at, or '' to run every phase.

    A run that failed while publishing an approved change starts again at
    publishing: the approval and the held change carry over. A runner job that
    failed after its plan was approved starts again at building from that plan.
    LBW's own loop plans again on a build decision, so it only resumes publishing.
    """
    if status not in {"failed", "error"}:
        return ""
    if decision == "approve" and branch.strip():
        return "publish"
    if decision == "build" and plan.strip() and agent_kind == "claude_code":
        return "build"
    return ""
