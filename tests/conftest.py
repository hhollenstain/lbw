from __future__ import annotations

import pytest

from agent_lbw.config import Settings
from agent_lbw.runtime.palace import NullPalace
from agent_lbw.runtime.uireview import UiEvidence


def _skipped_ui_evidence(*_args, **_kwargs) -> UiEvidence:
    return UiEvidence(ok=True, notes="skipped live browser in tests")


@pytest.fixture(autouse=True)
def _skip_workspace_palace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep cycle tests off Chroma and the embedding model."""
    monkeypatch.setattr(
        "agent_lbw.runtime.palace.open_palace",
        lambda root, **_kwargs: NullPalace(),
    )


@pytest.fixture(autouse=True)
def _skip_live_ui_review(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_lbw.runtime.improve.collect_ui_evidence", _skipped_ui_evidence)
    monkeypatch.setattr("agent_lbw.runtime.uireview.collect_ui_evidence", _skipped_ui_evidence)
    monkeypatch.setattr("agent_lbw.tools.browser.collect_ui_evidence", _skipped_ui_evidence)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        model_name="scripted",
        model_base_url="http://127.0.0.1:9/v1",
        auto_commit=True,
        require_tests=True,
        create_pr=False,
        max_iterations=8,
        command_timeout_seconds=30,
        plan_goals=False,
        git_author_name="lbw-test",
        git_author_email="lbw@test.local",
    )
