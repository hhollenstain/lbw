"""Where the next run of a failed proposal can start, like re-running failed jobs."""

from __future__ import annotations

RESTART_PHASES = ("build", "publish")
_NOTES = "\n\nAgent notes:\n"


def restart_phase(
    *,
    status: str,
    decision: str,
    plan: str,
    branch: str,
    agent_kind: str,
    committed: bool = False,
) -> str:
    """The phase a failed run can start the next run at, or '' to run every phase.

    A run that failed while publishing an approved change starts again at
    publishing, which runs the tests first: the approval and the held change
    carry over. A runner job whose commits LBW applied on its branch, then
    failed the tests or publishing, starts there too. It reached that point
    without a person's approval only because its job asks for none. A runner
    job that failed after its plan was approved starts again at building from
    that plan. LBW's own loop plans again on a build decision, so it only
    resumes publishing.
    """
    if status not in {"failed", "error"}:
        return ""
    if decision == "approve" and branch.strip():
        return "publish"
    if agent_kind == "claude_code" and committed and branch.strip():
        return "publish"
    if decision == "build" and plan.strip() and agent_kind == "claude_code":
        return "build"
    return ""


def carried_summary(summary: str) -> str:
    """What the agent wrote, without the failed run's own "Failed: ..." lead.

    A run that starts at a later phase keeps the agent's summary, and a
    success must not open with the earlier failure.
    """
    text = (summary or "").strip()
    if not text.startswith("Failed:"):
        return text
    _, found, notes = text.partition(_NOTES)
    return notes.strip() if found else ""
