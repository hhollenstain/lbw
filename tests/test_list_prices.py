"""Public list prices: hosted models are priced without typing a price, and an
organization's own price still wins."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_lbw.config import Settings
from agent_lbw.runtime import list_prices
from agent_lbw.runtime.account import load_account
from agent_lbw.runtime.db import database
from agent_lbw.runtime.list_prices import (
    ListPrice,
    find_price,
    merged,
    normalize_model,
    parse_catalog,
)
from agent_lbw.runtime.store.usage import (
    clear_cache,
    load_pricing,
    record_run,
    refresh_list_prices,
    save_price,
)
from agent_lbw.runtime.tasks import TaskManager
from agent_lbw.web_ui import create_app


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    clear_cache()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("anthropic/claude-sonnet-4-5-20250929", "claude-sonnet-4-5"),
        ("claude-sonnet-4-5@20250929", "claude-sonnet-4-5"),
        ("us.anthropic.claude-sonnet-4-5-v1:0", "claude-sonnet-4-5"),
        ("claude-4.5-sonnet-thinking", "claude-sonnet-4-5-thinking"),
        ("sonnet", "claude-sonnet-5-5"),
        ("models/gemini-2.5-pro", "gemini-2.5-pro"),
        ("openai/GPT-5.4-mini-2026-03-17", "gpt-5.4-mini"),
        ("xai/grok-4.20-0309", "grok-4.20"),
    ],
)
def test_gateway_cursor_and_claude_code_names_fold_onto_one_name(raw: str, expected: str) -> None:
    assert normalize_model(raw) == expected


def test_an_unknown_suffix_falls_back_to_the_model_it_names() -> None:
    table = merged({})
    assert find_price(table, "claude-4.5-sonnet-thinking").model == "claude-sonnet-4-5"
    assert find_price(table, "gpt-6-sol-high").model == "gpt-6-sol"
    assert find_price(table, "llama3.1-8b") is None
    assert find_price(table, "claude-sonnet") is None


def test_the_catalog_keeps_sane_chat_prices_and_the_provider_page_wins() -> None:
    rows = parse_catalog(
        {
            "sample_spec": {"mode": "chat"},
            "gpt-9": {
                "litellm_provider": "openai",
                "mode": "chat",
                "input_cost_per_token": 2e-06,
                "output_cost_per_token": 8e-06,
                "cache_read_input_token_cost": 5e-07,
            },
            "gpt-9-2027-01-01": {
                "litellm_provider": "openai",
                "mode": "chat",
                "input_cost_per_token": 9,
                "output_cost_per_token": 9,
            },
            "text-embedding-9": {
                "litellm_provider": "openai",
                "mode": "embedding",
                "input_cost_per_token": 1e-07,
            },
            "gemini/gemini-9-pro": {
                "litellm_provider": "gemini",
                "mode": "chat",
                "input_cost_per_token": 1e-06,
                "output_cost_per_token": "free",
            },
            "bedrock/claude-x": {
                "litellm_provider": "bedrock",
                "mode": "chat",
                "input_cost_per_token": 1e-06,
                "output_cost_per_token": 1e-06,
            },
            "claude-sonnet-5-5": {
                "litellm_provider": "anthropic",
                "mode": "chat",
                "input_cost_per_token": 2e-06,
                "output_cost_per_token": 1e-05,
                "cache_read_input_token_cost": 1e-07,
            },
        }
    )
    by_name = {row.model: row for row in rows}
    assert set(by_name) == {"gpt-9", "claude-sonnet-5-5"}
    assert by_name["gpt-9"].input_per_mtok == 2
    assert by_name["gpt-9"].cache_read_per_mtok == 0.5
    table = merged(by_name)
    # Anthropic's own page says $0.20 for Sonnet 5.5 cache reads; the catalog's $0.10 loses.
    assert table["claude-sonnet-5-5"].cache_read_per_mtok == 0.2
    assert table["claude-sonnet-5-5"].source == "anthropic.com"
    with pytest.raises(ValueError):
        parse_catalog(["not", "an", "object"])


def test_a_run_is_costed_at_list_price_unless_the_organization_set_one(tmp_path: Path) -> None:
    org = load_account(tmp_path)["org"]["id"]
    events = [
        {
            "kind": "llm",
            "purpose": "agent",
            "model_name": "claude-sonnet-4-5-20250929",
            "provider": "litellm",
            "prompt_tokens": 1_000_000,
            "completion_tokens": 100_000,
        }
    ]
    when = datetime(2026, 11, 20, tzinfo=UTC)
    with database(tmp_path) as db:
        listed = record_run(
            db, tenant_id=org, project_id="p", identity_id="ada", events=events, when=when
        )
        # $3 per million input and $15 per million output, from Anthropic's page.
        assert listed["cost_micros"] == 4_500_000
        save_price(
            db,
            org,
            provider="",
            model="claude-sonnet-4-5",
            input_per_mtok=1,
            output_per_mtok=1,
        )
        assert load_pricing(db, org).price("litellm", "claude-sonnet-4-5-20250929")[1] == (
            "override"
        )
        own = record_run(
            db, tenant_id=org, project_id="p", identity_id="ada", events=events, when=when
        )
        assert own["cost_micros"] == 1_100_000
        refresh_list_prices(db, org, [ListPrice("gpt-9", "openai", 2, 8)], when=1)
        assert load_pricing(db, org).price("openai", "openai/gpt-9")[0]["input_micros"] == (
            2_000_000
        )


def test_the_prices_page_lists_models_in_use_and_an_admin_refreshes_the_list(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = TestClient(create_app(TaskManager(settings), default_workspace=tmp_path))
    org = client.get("/api/account").json()["org"]["id"]
    with database(tmp_path) as db:
        for model_id, name, provider in (
            ("model_a", "claude-opus-5-5", "anthropic"),
            ("model_b", "sonnet", "anthropic"),
            ("model_c", "gpt-9", "openai"),
            ("model_d", "llama3", "ollama"),
        ):
            db.execute(
                "INSERT INTO models (id, tenant_id, name, provider, model_name) "
                "VALUES (?, ?, ?, ?, ?)",
                (model_id, org, name, provider, name),
            )

    body = client.get("/api/org/prices").json()
    rows = {row["model"]: row for row in body["models"]}
    assert rows["claude-opus-5-5"]["source"] == "list"
    assert rows["claude-opus-5-5"]["price"]["output_per_mtok"] == 20
    assert rows["sonnet"]["list_source"] == "anthropic.com"
    assert rows["gpt-9"]["source"] == "none"
    assert rows["llama3"]["price"] is None
    assert body["list"]["fetched"] == 0

    monkeypatch.setattr(
        "agent_lbw.integrations_http.fetch_catalog",
        lambda: [ListPrice("gpt-9", "openai", 2, 8, 0.5, 0, list_prices.CATALOG_LABEL)],
    )
    refreshed = client.post("/api/org/prices/refresh")
    assert refreshed.status_code == 200, refreshed.text
    rows = {row["model"]: row for row in refreshed.json()["models"]}
    assert rows["gpt-9"]["source"] == "list"
    assert rows["gpt-9"]["price"]["input_per_mtok"] == 2
    assert refreshed.json()["list"]["fetched_models"] == 1

    overridden = client.put(
        "/api/org/prices",
        json={"provider": "openai", "model": "gpt-9", "input_per_mtok": 1, "output_per_mtok": 4},
    ).json()
    rows = {row["model"]: row for row in overridden["models"]}
    assert rows["gpt-9"]["source"] == "override"
    assert rows["gpt-9"]["price"]["input_per_mtok"] == 1
    assert rows["gpt-9"]["list_price"]["input_per_mtok"] == 2

    def unreachable() -> list:
        raise ValueError("could not reach the public price list")

    monkeypatch.setattr("agent_lbw.integrations_http.fetch_catalog", unreachable)
    failed = client.post("/api/org/prices/refresh")
    assert failed.status_code == 400
    assert failed.json() == {"error": "could not reach the public price list"}
