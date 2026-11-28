"""The organization site and the engineer dashboard find each other by host."""

from __future__ import annotations

import pytest
from lbw_shared.hosts import (
    crossing_url,
    github_callback_url,
    is_crossing_host,
    organization_name,
)


@pytest.fixture
def no_configured_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LBW_ORGANIZATION_HOST", raising=False)
    monkeypatch.delenv("LBW_CROSSING_HOST", raising=False)


@pytest.fixture
def prod_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LBW_ORGANIZATION_HOST", "agent.lookbothways.ai")
    monkeypatch.setenv("LBW_CROSSING_HOST", "crossing.lookbothways.ai")


@pytest.mark.usefixtures("no_configured_hosts")
def test_hosts_pair_by_name_by_default() -> None:
    assert (
        crossing_url("www.lookbothways.dev", scheme="https") == "https://crossing.lookbothways.dev/"
    )
    assert (
        crossing_url("www-local.lookbothways.dev", scheme="https")
        == "https://crossing-local.lookbothways.dev/"
    )
    assert organization_name("crossing-local.lookbothways.dev") == "www-local.lookbothways.dev"
    assert crossing_url("127.0.0.1:8080", scheme="http") == "/crossingguard"
    assert is_crossing_host("crossing.lookbothways.dev")
    assert not is_crossing_host("agent.lookbothways.ai")


@pytest.mark.usefixtures("prod_hosts")
def test_configured_hosts_replace_the_name_pairing() -> None:
    assert (
        crossing_url("agent.lookbothways.ai", scheme="https") == "https://crossing.lookbothways.ai/"
    )
    assert crossing_url("crossing.lookbothways.ai", scheme="https") == "/"
    assert is_crossing_host("crossing.lookbothways.ai")
    assert not is_crossing_host("agent.lookbothways.ai")
    assert organization_name("crossing.lookbothways.ai") == "agent.lookbothways.ai"
    # GitHub sends the dashboard's sign-in back to the organization site, not the landing page.
    assert (
        github_callback_url("crossing.lookbothways.ai", scheme="https")
        == "https://agent.lookbothways.ai/api/github/callback"
    )
    assert (
        github_callback_url("agent.lookbothways.ai", scheme="https")
        == "https://agent.lookbothways.ai/api/github/callback"
    )


@pytest.mark.usefixtures("prod_hosts")
def test_other_hosts_keep_the_name_pairing() -> None:
    assert crossing_url("www-local.lookbothways.dev", scheme="https") == (
        "https://crossing-local.lookbothways.dev/"
    )
    assert crossing_url("localhost:8080", scheme="http") == "/crossingguard"


def test_one_configured_host_alone_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LBW_ORGANIZATION_HOST", "agent.lookbothways.ai")
    monkeypatch.delenv("LBW_CROSSING_HOST", raising=False)
    assert crossing_url("agent.lookbothways.ai", scheme="https") == (
        "https://crossing-agent.lookbothways.ai/"
    )
