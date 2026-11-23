"""Public list prices for hosted models, so cost shows without typing a price.

Three sources, most trusted first:

1. ``FIRST_PARTY``: prices copied from the provider's own pricing page
   (Anthropic, and Cursor's own Composer models). These win over the
   catalog because a community file can lag or slip a digit.
2. The public catalog: LiteLLM's ``model_prices_and_context_window.json``,
   fetched when an owner or admin presses Refresh. It covers OpenAI, Google,
   xAI, and Anthropic.
3. ``SNAPSHOT``: a copy of that catalog shipped with LBW, so a fresh install
   or an offline one still has prices.

An organization's own price beats all three; see ``runtime/store/usage.py``.

Prices are dollars per million tokens: input, output, cache read, and cache
write. Cache write is the five-minute write where a provider has two.
Names are matched after ``normalize_model``, which folds the spellings that
gateways, Cursor, Bedrock, Vertex, and Claude Code use onto one name, and
ignores the provider: a gateway or Cursor bills a model at its list price.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

CATALOG_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
CATALOG_LABEL = "LiteLLM public price list"
SNAPSHOT_AS_OF = "2026-11-22"
_MAX_BYTES = 32 * 1024 * 1024
_MAX_PRICE = 10_000.0
_PER_MILLION = 1_000_000

# The catalog's provider names that LBW prices, and the name LBW gives each.
_CATALOG_PROVIDERS = {
    "anthropic": "anthropic",
    "openai": "openai",
    "gemini": "google",
    "xai": "xai",
}


@dataclass(frozen=True)
class ListPrice:
    model: str
    provider: str
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float = 0.0
    cache_write_per_mtok: float = 0.0
    source: str = ""

    def micros(self) -> dict[str, int]:
        return {
            "input_micros": _micros(self.input_per_mtok),
            "output_micros": _micros(self.output_per_mtok),
            "cache_read_micros": _micros(self.cache_read_per_mtok),
            "cache_write_micros": _micros(self.cache_write_per_mtok),
        }


def _rows(source: str, table: dict[str, tuple]) -> dict[str, ListPrice]:
    return {
        name: ListPrice(name, provider, inp, out, read, write, source)
        for name, (provider, inp, out, read, write) in table.items()
    }


# Anthropic: platform.claude.com pricing. Cache writes are 1.25x input.
# Cursor: cursor.com/docs/models-and-pricing. It lists no cache write price,
# so a write is counted at the input price.
FIRST_PARTY: dict[str, ListPrice] = {
    **_rows(
        "anthropic.com",
        {
            "claude-fable-5-1": ("anthropic", 10, 50, 0.25, 12.5),
            "claude-mythos-5-1": ("anthropic", 10, 50, 0.25, 12.5),
            "claude-fable-5": ("anthropic", 10, 50, 1, 12.5),
            "claude-opus-5-5": ("anthropic", 4, 20, 0.2, 5),
            "claude-opus-5": ("anthropic", 5, 25, 0.5, 6.25),
            "claude-opus-4-8": ("anthropic", 5, 25, 0.5, 6.25),
            "claude-opus-4-7": ("anthropic", 5, 25, 0.5, 6.25),
            "claude-opus-4-6": ("anthropic", 5, 25, 0.5, 6.25),
            "claude-opus-4-5": ("anthropic", 5, 25, 0.5, 6.25),
            "claude-opus-4-1": ("anthropic", 15, 75, 1.5, 18.75),
            "claude-sonnet-5-5": ("anthropic", 2, 10, 0.2, 2.5),
            "claude-sonnet-5": ("anthropic", 2, 10, 0.2, 2.5),
            "claude-sonnet-4-6": ("anthropic", 3, 15, 0.3, 3.75),
            "claude-sonnet-4-5": ("anthropic", 3, 15, 0.3, 3.75),
            "claude-sonnet-4": ("anthropic", 3, 15, 0.3, 3.75),
            "claude-haiku-5-5": ("anthropic", 0.1, 0.5, 0.01, 0.125),
            "claude-haiku-4-5": ("anthropic", 1, 5, 0.1, 1.25),
        },
    ),
    **_rows(
        "cursor.com",
        {
            "composer-2.5": ("cursor", 0.5, 2.5, 0.2, 0.5),
            "composer-2.5-fast": ("cursor", 3, 15, 0.5, 3),
        },
    ),
}

# A copy of the public catalog for the models most organizations run.
SNAPSHOT: dict[str, ListPrice] = _rows(
    f"{CATALOG_LABEL} ({SNAPSHOT_AS_OF})",
    {
        "gpt-6.2-astra": ("openai", 8, 40, 0.4, 10),
        "gpt-6.2-sol": ("openai", 2, 10, 0.1, 2.5),
        "gpt-6.1-sol": ("openai", 2, 10, 0.1, 2.5),
        "gpt-6.1-terra": ("openai", 1, 5, 0.05, 1.25),
        "gpt-6-astra": ("openai", 10, 50, 1, 12.5),
        "gpt-6-sol": ("openai", 2, 10, 0.2, 2.5),
        "gpt-6-luna": ("openai", 0.1, 0.5, 0.01, 0.125),
        "gpt-5.6-sol": ("openai", 4, 20, 0.4, 5),
        "gpt-5.6-terra": ("openai", 2, 12, 0.2, 2.5),
        "gpt-5.6-luna": ("openai", 0.2, 1.2, 0.02, 0.25),
        "gpt-5.5": ("openai", 5, 30, 0.5, 0),
        "gpt-5.4": ("openai", 2.5, 15, 0.25, 0),
        "gpt-5.4-mini": ("openai", 0.75, 4.5, 0.075, 0),
        "gpt-5.4-nano": ("openai", 0.2, 1.25, 0.02, 0),
        "gpt-5": ("openai", 1.25, 10, 0.125, 0),
        "gpt-5-mini": ("openai", 0.25, 2, 0.025, 0),
        "gemini-3.1-pro-preview": ("google", 2, 12, 0.2, 0),
        "gemini-3.8-flash": ("google", 0.75, 3.75, 0.075, 0),
        "gemini-3.5-flash": ("google", 1.5, 9, 0.15, 0),
        "gemini-3.5-flash-lite": ("google", 0.3, 2.5, 0.03, 0),
        "gemini-2.5-pro": ("google", 1.25, 10, 0.125, 0),
        "gemini-2.5-flash": ("google", 0.3, 2.5, 0.03, 0),
        "grok-4.7": ("xai", 2, 6, 0.5, 0),
        "grok-4.6": ("xai", 2, 6, 0.5, 0),
        "grok-4.5": ("xai", 2, 6, 0.3, 0),
    },
)

# Claude Code's --model names. Each follows the current model of that tier.
ALIASES = {
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5",
    "fable": "claude-fable-5-1",
}

_DATE = re.compile(r"-(\d{8}|\d{4}-\d{2}-\d{2}|\d{4})$")
_BEDROCK_VERSION = re.compile(r"-v\d+(:\d+)?$")
_CURSOR_CLAUDE = re.compile(r"^claude-(\d+(?:[.-]\d+)?)-(opus|sonnet|haiku|fable|mythos)(.*)$")


def normalize_model(name: str) -> str:
    """One spelling per model, so a gateway's or Cursor's name finds the list price.

    ``anthropic/claude-sonnet-4-5-20250929``, ``claude-sonnet-4-5@20250929``,
    ``us.anthropic.claude-sonnet-4-5-v1:0``, and Cursor's ``claude-4.5-sonnet``
    all become ``claude-sonnet-4-5``. Claude Code's ``sonnet`` becomes the
    current Sonnet.
    """
    text = " ".join(str(name or "").split()).lower()
    if "/" in text:
        text = text.rsplit("/", 1)[1]
    text = text.split("@", 1)[0]
    if "anthropic." in text:
        text = text.split("anthropic.", 1)[1]
        text = _BEDROCK_VERSION.sub("", text)
    if text.endswith("-latest"):
        text = text[: -len("-latest")]
    text = _DATE.sub("", text)
    flipped = _CURSOR_CLAUDE.match(text)
    if flipped:
        version, tier, rest = flipped.groups()
        text = f"claude-{tier}-{version}{rest}"
    if text.startswith("claude-"):
        text = text.replace(".", "-")
    return ALIASES.get(text, text)


def find_price(table: dict[str, ListPrice], model: str) -> ListPrice | None:
    """The list price for a model name, or None.

    An unknown suffix such as ``-thinking`` or ``-high`` falls back to the
    model it names, down to two name parts and never further.
    """
    name = normalize_model(model)
    while name:
        found = table.get(name)
        if found is not None:
            return found
        parts = name.split("-")
        if len(parts) <= 2:
            return None
        name = "-".join(parts[:-1])
    return None


def merged(fetched: dict[str, ListPrice]) -> dict[str, ListPrice]:
    """Snapshot, then the fetched catalog, then the providers' own pages."""
    return {**SNAPSHOT, **fetched, **FIRST_PARTY}


def parse_catalog(payload: object) -> list[ListPrice]:
    """Chat models from the public catalog, for the providers LBW prices.

    Anything that is not a sane number is skipped, so a bad row never prices a run.
    """
    if not isinstance(payload, dict):
        raise ValueError("the price list was not a JSON object")
    found: dict[str, ListPrice] = {}
    for key in sorted(payload, key=len):
        row = payload[key]
        if not isinstance(key, str) or not isinstance(row, dict):
            continue
        provider = _CATALOG_PROVIDERS.get(str(row.get("litellm_provider") or ""))
        if provider is None or row.get("mode") != "chat":
            continue
        name = normalize_model(key)
        if not name or len(name) > 120 or name in found:
            continue
        prices = [
            _per_mtok(row.get(field))
            for field in (
                "input_cost_per_token",
                "output_cost_per_token",
                "cache_read_input_token_cost",
                "cache_creation_input_token_cost",
            )
        ]
        if prices[0] is None or prices[1] is None:
            continue
        found[name] = ListPrice(
            name,
            provider,
            prices[0],
            prices[1],
            prices[2] or 0.0,
            prices[3] or 0.0,
            CATALOG_LABEL,
        )
    return list(found.values())


def fetch_catalog(url: str = CATALOG_URL) -> list[ListPrice]:
    """Download and parse the public catalog. Raises ValueError when it cannot."""
    try:
        response = httpx.get(url, timeout=20.0, follow_redirects=False)
    except httpx.HTTPError as exc:
        raise ValueError("could not reach the public price list") from exc
    if response.status_code >= 400:
        raise ValueError(f"the public price list returned HTTP {response.status_code}")
    if len(response.content) > _MAX_BYTES:
        raise ValueError("the public price list is larger than LBW reads")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError("the public price list was not JSON") from exc
    rows = parse_catalog(payload)
    if not rows:
        raise ValueError("the public price list had no models LBW prices")
    return rows


def _per_mtok(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    dollars = float(value) * _PER_MILLION
    if not 0 <= dollars <= _MAX_PRICE:
        return None
    return round(dollars, 6)


def _micros(dollars: float) -> int:
    return int(round(float(dollars) * _PER_MILLION))
