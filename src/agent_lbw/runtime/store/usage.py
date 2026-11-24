"""Token usage, cost, and change insights, kept as small daily rollups.

A run records once when it stops, not once per model call. The cycle's
progress events already hold every model call; ``record_run`` folds them into
one row per day, project, person, service, model, step, and purpose, and adds
to that row. Reports read only these rollups, inside a bounded date range, and
a short cache answers repeat views, so a busy dashboard does not load the
database.

Names follow the OpenTelemetry GenAI conventions where one exists: input
tokens include cached input (``gen_ai.usage.input_tokens``), and cache reads
and writes are counted apart as well.

To add a breakdown: add a column to the rollup table in ``schema.py`` (and to
its primary key), fill it in ``record_run``, and add one ``Dimension`` to
``DIMENSIONS`` and to that dataset's ``dimensions``. To add a number, add one
``Measure`` to the dataset. The API and the dashboard read the registry, so
nothing else changes.
"""

from __future__ import annotations

import csv
import io
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import PurePosixPath
from urllib.parse import urlparse

from agent_lbw.runtime import list_prices as catalog
from agent_lbw.runtime.db import Database

MAX_DAYS = 366
DEFAULT_DAYS = 30
MAX_GROUPS = 50
CACHE_SECONDS = 60
_CACHE_LIMIT = 256
_PER_MILLION = 1_000_000


@dataclass(frozen=True)
class Dimension:
    key: str
    label: str
    column: str
    otel: str = ""
    names: str = ""


@dataclass(frozen=True)
class Measure:
    key: str
    label: str
    sql: str
    unit: str
    otel: str = ""


@dataclass(frozen=True)
class Dataset:
    key: str
    label: str
    table: str
    dimensions: tuple[str, ...]
    measures: tuple[Measure, ...]
    note: str = ""

    def measure(self, key: str) -> Measure:
        for item in self.measures:
            if item.key == key:
                return item
        raise ValueError(f"{self.label} has no measure {key}")


DIMENSIONS: dict[str, Dimension] = {
    item.key: item
    for item in (
        Dimension("day", "Day", "day"),
        Dimension("project", "Project", "project_id", names="project"),
        Dimension("person", "Person", "identity_id", names="person"),
        Dimension("service", "Service", "service"),
        Dimension("provider", "Provider", "provider", otel="gen_ai.provider.name"),
        Dimension("model", "Model", "model", otel="gen_ai.request.model"),
        Dimension("step", "Agent step", "step", otel="gen_ai.agent.name"),
        Dimension("purpose", "Operation", "purpose", otel="gen_ai.operation.name"),
        Dimension("extension", "File type", "extension"),
        Dimension("skill", "Skill", "skill"),
        Dimension("outcome", "Outcome", "outcome"),
    )
}

DATASETS: dict[str, Dataset] = {
    item.key: item
    for item in (
        Dataset(
            key="tokens",
            label="Tokens and cost",
            table="usage_daily",
            dimensions=(
                "day",
                "project",
                "person",
                "service",
                "provider",
                "model",
                "step",
                "purpose",
            ),
            measures=(
                Measure(
                    "tokens",
                    "Tokens",
                    "SUM(input_tokens + output_tokens)",
                    "tokens",
                ),
                Measure(
                    "input_tokens",
                    "Input tokens",
                    "SUM(input_tokens)",
                    "tokens",
                    otel="gen_ai.usage.input_tokens",
                ),
                Measure(
                    "output_tokens",
                    "Output tokens",
                    "SUM(output_tokens)",
                    "tokens",
                    otel="gen_ai.usage.output_tokens",
                ),
                Measure(
                    "cache_read_tokens",
                    "Cache reads",
                    "SUM(cache_read_tokens)",
                    "tokens",
                    otel="gen_ai.usage.cache_read.input_tokens",
                ),
                Measure(
                    "cache_write_tokens",
                    "Cache writes",
                    "SUM(cache_write_tokens)",
                    "tokens",
                    otel="gen_ai.usage.cache_creation.input_tokens",
                ),
                Measure("cost", "Estimated cost", "SUM(cost_micros)", "usd_micros"),
                Measure("calls", "Model calls", "SUM(calls)", "count"),
                Measure("errors", "Failed calls", "SUM(errors)", "count"),
                Measure(
                    "duration_ms",
                    "Model time",
                    "SUM(duration_ms)",
                    "ms",
                    otel="gen_ai.client.operation.duration",
                ),
            ),
            note=(
                "Cost uses the organization's price for a model, or its public list"
                " price, at the time of the run."
            ),
        ),
        Dataset(
            key="files",
            label="Files changed",
            table="usage_files_daily",
            dimensions=("day", "project", "person", "extension"),
            measures=(
                Measure("files", "Files", "SUM(files)", "count"),
                Measure("lines_added", "Lines added", "SUM(lines_added)", "count"),
                Measure("lines_removed", "Lines removed", "SUM(lines_removed)", "count"),
                Measure(
                    "lines_changed",
                    "Lines changed",
                    "SUM(lines_added + lines_removed)",
                    "count",
                ),
            ),
            note="Counted from each run's final diff.",
        ),
        Dataset(
            key="skills",
            label="Skills",
            table="usage_skills_daily",
            dimensions=("day", "project", "person", "skill"),
            measures=(
                Measure("runs", "Runs using the skill", "SUM(runs)", "count"),
                Measure("tokens", "Tokens in those runs", "SUM(tokens)", "tokens"),
            ),
            note="A run with several skills counts toward each of them.",
        ),
        Dataset(
            key="runs",
            label="Runs",
            table="usage_runs_daily",
            dimensions=("day", "project", "person", "outcome"),
            measures=(
                Measure("runs", "Runs", "SUM(runs)", "count"),
                Measure("duration_ms", "Run time", "SUM(duration_ms)", "ms"),
            ),
            note="One run is one stretch of work, from a start or a gate to the next stop.",
        ),
    )
}

_PURPOSE_STEP = {"agent": "build", "plan": "plan", "propose": "plan", "review": "goal check"}


def registry() -> dict:
    """The datasets, breakdowns, and numbers a report can ask for."""
    return {
        "datasets": [
            {
                "key": item.key,
                "label": item.label,
                "note": item.note,
                "dimensions": [
                    {
                        "key": DIMENSIONS[name].key,
                        "label": DIMENSIONS[name].label,
                        "otel": DIMENSIONS[name].otel,
                    }
                    for name in item.dimensions
                ],
                "measures": [
                    {
                        "key": measure.key,
                        "label": measure.label,
                        "unit": measure.unit,
                        "otel": measure.otel,
                    }
                    for measure in item.measures
                ],
            }
            for item in DATASETS.values()
        ],
        "max_days": MAX_DAYS,
        "default_days": DEFAULT_DAYS,
        "cache_seconds": CACHE_SECONDS,
    }


# --- recording -------------------------------------------------------------


def record_run(
    db: Database,
    *,
    tenant_id: str,
    project_id: str,
    identity_id: str,
    events: Iterable[dict],
    files: dict[str, dict[str, int]] | None = None,
    outcome: str = "",
    duration_ms: int = 0,
    default_service: str = "",
    default_model: str = "",
    when: datetime | None = None,
) -> dict:
    """Fold one run's model calls, skills, and file changes into the daily rollups.

    ``files`` is per file type, as ``file_stats`` returns it, and holds only what
    this run changed. Returns the run's totals, which the audit trail notes.
    """
    day = (when or datetime.now(UTC)).date().isoformat()
    pricing = load_pricing(db, tenant_id)
    rows: dict[tuple[str, str, str, str, str], dict[str, int]] = {}
    skills: set[str] = set()
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("kind") == "skills":
            skills.update(str(name) for name in event.get("names") or [] if str(name).strip())
            continue
        if event.get("kind") != "llm":
            continue
        purpose = str(event.get("purpose") or "")
        model = str(event.get("model_name") or default_model or "")
        service = str(event.get("service") or default_service or "")
        provider = str(event.get("provider") or _provider_from(service))
        step = str(event.get("role") or _PURPOSE_STEP.get(purpose, purpose))
        key = (service, provider, model, step, purpose)
        row = rows.setdefault(key, _empty_tokens())
        row["calls"] += 1
        if event.get("ok") is False:
            row["errors"] += 1
        prompt = _count(event.get("prompt_tokens"))
        completion = _count(event.get("completion_tokens"))
        total = _count(event.get("total_tokens"))
        if not prompt and not completion and total:
            prompt = total
        row["input_tokens"] += prompt
        row["output_tokens"] += completion
        row["cache_read_tokens"] += _count(event.get("cache_read_tokens"))
        row["cache_write_tokens"] += _count(event.get("cache_write_tokens"))
        row["duration_ms"] += _count(event.get("elapsed_ms"))
    totals = _empty_tokens()
    for (service, provider, model, step, purpose), row in rows.items():
        row["cost_micros"] = _cost(pricing, provider, model, row)
        for name, value in row.items():
            totals[name] += value
        db.execute(
            """
            INSERT INTO usage_daily (
                tenant_id, day, project_id, identity_id, service, provider, model, step,
                purpose, calls, errors, input_tokens, output_tokens, cache_read_tokens,
                cache_write_tokens, duration_ms, cost_micros
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (tenant_id, day, project_id, identity_id, service, model, step, purpose)
            DO UPDATE SET
                provider = excluded.provider,
                calls = usage_daily.calls + excluded.calls,
                errors = usage_daily.errors + excluded.errors,
                input_tokens = usage_daily.input_tokens + excluded.input_tokens,
                output_tokens = usage_daily.output_tokens + excluded.output_tokens,
                cache_read_tokens = usage_daily.cache_read_tokens + excluded.cache_read_tokens,
                cache_write_tokens = usage_daily.cache_write_tokens + excluded.cache_write_tokens,
                duration_ms = usage_daily.duration_ms + excluded.duration_ms,
                cost_micros = usage_daily.cost_micros + excluded.cost_micros
            """,
            (
                tenant_id,
                day,
                project_id,
                identity_id,
                service,
                provider,
                model,
                step,
                purpose,
                row["calls"],
                row["errors"],
                row["input_tokens"],
                row["output_tokens"],
                row["cache_read_tokens"],
                row["cache_write_tokens"],
                row["duration_ms"],
                row["cost_micros"],
            ),
        )
    tokens = totals["input_tokens"] + totals["output_tokens"]
    for skill in sorted(skills):
        db.execute(
            """
            INSERT INTO usage_skills_daily (
                tenant_id, day, project_id, identity_id, skill, runs, tokens
            ) VALUES (?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT (tenant_id, day, project_id, identity_id, skill) DO UPDATE SET
                runs = usage_skills_daily.runs + 1,
                tokens = usage_skills_daily.tokens + excluded.tokens
            """,
            (tenant_id, day, project_id, identity_id, skill, tokens),
        )
    for extension, stats in (files or {}).items():
        if not any(stats.values()):
            continue
        db.execute(
            """
            INSERT INTO usage_files_daily (
                tenant_id, day, project_id, identity_id, extension, files,
                lines_added, lines_removed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (tenant_id, day, project_id, identity_id, extension) DO UPDATE SET
                files = usage_files_daily.files + excluded.files,
                lines_added = usage_files_daily.lines_added + excluded.lines_added,
                lines_removed = usage_files_daily.lines_removed + excluded.lines_removed
            """,
            (
                tenant_id,
                day,
                project_id,
                identity_id,
                extension,
                stats["files"],
                stats["lines_added"],
                stats["lines_removed"],
            ),
        )
    if outcome:
        db.execute(
            """
            INSERT INTO usage_runs_daily (
                tenant_id, day, project_id, identity_id, outcome, runs, duration_ms
            ) VALUES (?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT (tenant_id, day, project_id, identity_id, outcome) DO UPDATE SET
                runs = usage_runs_daily.runs + 1,
                duration_ms = usage_runs_daily.duration_ms + excluded.duration_ms
            """,
            (tenant_id, day, project_id, identity_id, outcome, max(int(duration_ms), 0)),
        )
    totals["tokens"] = tokens
    totals["skills"] = len(skills)
    return totals


def file_stats(diff: str) -> dict[str, dict[str, int]]:
    """Files and lines changed per file type, read from a unified diff."""
    found: dict[str, dict[str, int]] = {}
    path = ""
    for line in (diff or "").splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" b/", 1)
            path = parts[1].strip() if len(parts) == 2 else ""
            if path:
                found.setdefault(file_type(path), _empty_files())["files"] += 1
            continue
        if not path or line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            found[file_type(path)]["lines_added"] += 1
        elif line.startswith("-"):
            found[file_type(path)]["lines_removed"] += 1
    return found


def file_delta(
    now: dict[str, dict[str, int]], before: dict[str, dict[str, int]]
) -> dict[str, dict[str, int]]:
    """What changed since the last run of the same job, so a rework is not counted twice."""
    delta: dict[str, dict[str, int]] = {}
    for extension, stats in now.items():
        earlier = before.get(extension, {})
        delta[extension] = {
            name: max(int(value) - int(earlier.get(name, 0)), 0) for name, value in stats.items()
        }
    return delta


def file_type(path: str) -> str:
    """A file's type for reports: its extension, or its name when it has none."""
    name = PurePosixPath(path).name
    suffix = PurePosixPath(name).suffix.lower()
    if suffix:
        return suffix
    return name or "(none)"


# --- prices ----------------------------------------------------------------


def list_prices(db: Database, tenant_id: str) -> list[dict]:
    rows = db.fetchall(
        """
        SELECT provider, model, input_micros, output_micros, cache_read_micros,
               cache_write_micros, updated
        FROM model_prices WHERE tenant_id = ?
        ORDER BY provider, model
        """,
        (tenant_id,),
    )
    return [
        {
            "provider": row["provider"],
            "model": row["model"],
            "input_per_mtok": _dollars(row["input_micros"]),
            "output_per_mtok": _dollars(row["output_micros"]),
            "cache_read_per_mtok": _dollars(row["cache_read_micros"]),
            "cache_write_per_mtok": _dollars(row["cache_write_micros"]),
            "updated": int(row["updated"] or 0),
        }
        for row in rows
    ]


def save_price(
    db: Database,
    tenant_id: str,
    *,
    provider: str,
    model: str,
    input_per_mtok: float,
    output_per_mtok: float,
    cache_read_per_mtok: float = 0.0,
    cache_write_per_mtok: float = 0.0,
) -> None:
    """Dollars per million tokens. A price counts toward runs after it is saved."""
    chosen_provider = " ".join((provider or "").split()).lower()
    chosen_model = " ".join((model or "").split())
    if not chosen_model:
        raise ValueError("name the model this price is for")
    if len(chosen_model) > 200 or len(chosen_provider) > 80:
        raise ValueError("keep the provider and model names short")
    values = [input_per_mtok, output_per_mtok, cache_read_per_mtok, cache_write_per_mtok]
    if any(value is None or value < 0 or value > 10_000 for value in values):
        raise ValueError("a price is between 0 and 10000 dollars per million tokens")
    micros = [int(round(float(value) * _PER_MILLION)) for value in values]
    db.execute(
        """
        INSERT INTO model_prices (
            tenant_id, provider, model, input_micros, output_micros,
            cache_read_micros, cache_write_micros, updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (tenant_id, provider, model) DO UPDATE SET
            input_micros = excluded.input_micros,
            output_micros = excluded.output_micros,
            cache_read_micros = excluded.cache_read_micros,
            cache_write_micros = excluded.cache_write_micros,
            updated = excluded.updated
        """,
        (tenant_id, chosen_provider, chosen_model, *micros, int(time.time())),
    )


def delete_price(db: Database, tenant_id: str, *, provider: str, model: str) -> None:
    db.execute(
        "DELETE FROM model_prices WHERE tenant_id = ? AND provider = ? AND model = ?",
        (tenant_id, " ".join((provider or "").split()).lower(), " ".join(model.split())),
    )


_PRICE_FIELDS = ("input_micros", "output_micros", "cache_read_micros", "cache_write_micros")


@dataclass(frozen=True)
class Pricing:
    """The organization's own prices first, then the public list prices."""

    overrides: dict[tuple[str, str], dict]
    listed: dict[str, catalog.ListPrice]

    def price(self, provider: str, model: str) -> tuple[dict | None, str]:
        """Micro-dollars per million tokens, and where the price came from."""
        wanted = (provider or "").lower()
        name = catalog.normalize_model(model)
        for key in ((wanted, model), ("", model), (wanted, name), ("", name)):
            found = self.overrides.get(key)
            if found is not None:
                return {field: int(found[field] or 0) for field in _PRICE_FIELDS}, "override"
        listed = catalog.find_price(self.listed, model)
        if listed is not None:
            return listed.micros(), listed.source
        return None, ""


def load_pricing(db: Database, tenant_id: str) -> Pricing:
    overrides: dict[tuple[str, str], dict] = {}
    for row in db.fetchall(
        f"SELECT provider, model, {', '.join(_PRICE_FIELDS)} FROM model_prices WHERE tenant_id = ?",
        (tenant_id,),
    ):
        provider = str(row["provider"] or "")
        overrides[(provider, str(row["model"]))] = row
        overrides.setdefault((provider, catalog.normalize_model(str(row["model"]))), row)
    fetched = {
        str(row["model"]): catalog.ListPrice(
            str(row["model"]),
            str(row["provider"] or ""),
            *(_dollars(row[field]) for field in _PRICE_FIELDS),
            source=catalog.CATALOG_LABEL,
        )
        for row in db.fetchall(
            f"SELECT model, provider, {', '.join(_PRICE_FIELDS)} "
            "FROM model_list_prices WHERE tenant_id = ?",
            (tenant_id,),
        )
    }
    return Pricing(overrides, catalog.merged(fetched))


def refresh_list_prices(
    db: Database, tenant_id: str, rows: Sequence[catalog.ListPrice], *, when: int | None = None
) -> int:
    """Replace the organization's copy of the public catalog. Runs already recorded keep
    their cost."""
    stamp = int(when if when is not None else time.time())
    db.execute("DELETE FROM model_list_prices WHERE tenant_id = ?", (tenant_id,))
    for row in rows:
        micros = row.micros()
        db.execute(
            """
            INSERT INTO model_list_prices (
                tenant_id, model, provider, input_micros, output_micros,
                cache_read_micros, cache_write_micros, fetched
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (tenant_id, model) DO NOTHING
            """,
            (
                tenant_id,
                row.model,
                row.provider,
                *(micros[field] for field in _PRICE_FIELDS),
                stamp,
            ),
        )
    return len(rows)


def price_table(db: Database, tenant_id: str) -> dict:
    """Every model the organization runs, and every price it set, with the price a run
    uses and where it came from."""
    pricing = load_pricing(db, tenant_id)
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for row in db.fetchall(
        """
        SELECT DISTINCT provider, model_name FROM models
        WHERE tenant_id = ? AND model_name IS NOT NULL AND model_name <> ''
        ORDER BY model_name, provider
        """,
        (tenant_id,),
    ):
        key = (str(row["provider"] or "").lower(), str(row["model_name"]))
        if key not in seen:
            seen.add(key)
            rows.append(_price_row(pricing, *key, in_use=True))
    for row in db.fetchall(
        "SELECT provider, model FROM model_prices WHERE tenant_id = ? ORDER BY model, provider",
        (tenant_id,),
    ):
        key = (str(row["provider"] or ""), str(row["model"]))
        if key not in seen:
            seen.add(key)
            rows.append(_price_row(pricing, *key, in_use=False))
    fetched = db.fetchone(
        "SELECT MAX(fetched) AS fetched, COUNT(*) AS models "
        "FROM model_list_prices WHERE tenant_id = ?",
        (tenant_id,),
    )
    return {
        "models": rows,
        "list": {
            "fetched": int((fetched or {}).get("fetched") or 0),
            "fetched_models": int((fetched or {}).get("models") or 0),
            "source": catalog.CATALOG_LABEL,
            "source_url": catalog.CATALOG_URL,
            "snapshot_as_of": catalog.SNAPSHOT_AS_OF,
        },
    }


def _price_row(pricing: Pricing, provider: str, model: str, *, in_use: bool) -> dict:
    price, source = pricing.price(provider, model)
    listed = catalog.find_price(pricing.listed, model)
    return {
        "provider": provider,
        "model": model,
        "in_use": in_use,
        "source": "override" if source == "override" else ("list" if price else "none"),
        "price": _per_mtok(price) if price else None,
        "list_price": _per_mtok(listed.micros()) if listed else None,
        "list_source": listed.source if listed else "",
    }


def _per_mtok(micros: dict) -> dict:
    return {
        "input_per_mtok": _dollars(micros["input_micros"]),
        "output_per_mtok": _dollars(micros["output_micros"]),
        "cache_read_per_mtok": _dollars(micros["cache_read_micros"]),
        "cache_write_per_mtok": _dollars(micros["cache_write_micros"]),
    }


# --- estimate --------------------------------------------------------------

ESTIMATE_DAYS = 90
# Used only until an organization has a finished build to learn from: an agent's
# build turns resend a growing context, so a build spends many times its plan.
DEFAULT_BUILD_RATIO = 15.0
_PLANNING = ("plan", "propose")
_NOT_BUILD = (*_PLANNING, "spec")


def estimate_build(
    db: Database,
    *,
    tenant_id: str,
    project_id: str,
    events: Iterable[dict],
    model: str,
    provider: str = "",
    today: date | None = None,
) -> dict:
    """What building a planned change should spend, before anyone presses Build.

    The change's own planning tokens are scaled by how many tokens builds,
    reviews, and checks used per planning token in this project over the last
    90 days, or in the organization when the project has none. With no history
    at all, a rough default ratio is used and the answer says so. It reads
    only the daily rollups.
    """
    planned = _empty_tokens()
    for event in events:
        if not isinstance(event, dict) or event.get("kind") != "llm":
            continue
        if event.get("ok") is False or str(event.get("purpose") or "") not in _PLANNING:
            continue
        planned["input_tokens"] += _count(event.get("prompt_tokens"))
        planned["output_tokens"] += _count(event.get("completion_tokens"))
        planned["cache_read_tokens"] += _count(event.get("cache_read_tokens"))
        planned["cache_write_tokens"] += _count(event.get("cache_write_tokens"))
        model = str(event.get("model_name") or "") or model
        provider = str(event.get("provider") or "") or provider
    plan_tokens = planned["input_tokens"] + planned["output_tokens"]
    pricing = load_pricing(db, tenant_id)
    price, source = pricing.price(provider, model)
    answer: dict = {
        "model": model,
        "provider": provider,
        "priced": price is not None,
        "price_source": source,
        "planning_tokens": plan_tokens,
        "planning_cost_micros": _cost(pricing, provider, model, planned),
    }
    if not plan_tokens:
        return answer | {
            "available": False,
            "note": "No planning calls were recorded for this change, so there is nothing "
            "to scale from.",
        }
    since = ((today or datetime.now(UTC).date()) - timedelta(days=ESTIMATE_DAYS)).isoformat()
    basis = "default"
    history: dict[str, dict[str, int]] = {}
    for scope, filters in (("project", project_id), ("organization", "")):
        if scope == "project" and not project_id:
            continue
        history = _phase_totals(db, tenant_id, since, filters)
        plan = history["plan"]["input_tokens"] + history["plan"]["output_tokens"]
        after = history["after"]["input_tokens"] + history["after"]["output_tokens"]
        if plan and after:
            basis = scope
            break
    if basis == "default":
        ratio = DEFAULT_BUILD_RATIO
        mix = planned
    else:
        mix = history["after"]
        ratio = (mix["input_tokens"] + mix["output_tokens"]) / (
            history["plan"]["input_tokens"] + history["plan"]["output_tokens"]
        )
    total = int(round(plan_tokens * ratio))
    mixed = mix["input_tokens"] + mix["output_tokens"]
    built = _empty_tokens()
    built["output_tokens"] = int(round(total * mix["output_tokens"] / mixed))
    built["input_tokens"] = total - built["output_tokens"]
    if mix["input_tokens"]:
        for name in ("cache_read_tokens", "cache_write_tokens"):
            built[name] = int(round(built["input_tokens"] * mix[name] / mix["input_tokens"]))
    notes = {
        "project": f"From this project's last {ESTIMATE_DAYS} days: building, reviews, and "
        f"checks used about {ratio:.1f} tokens per planning token.",
        "organization": f"This project has no finished build yet, so this uses the "
        f"organization's last {ESTIMATE_DAYS} days: about {ratio:.1f} tokens per planning token.",
        "default": f"No finished build to learn from yet, so this is rough: about "
        f"{ratio:.0f} tokens per planning token.",
    }
    note = notes[basis]
    if price is None:
        note += f" {model or 'This model'} has no price, so cost is not shown."
    return answer | {
        "available": True,
        "basis": basis,
        "days": ESTIMATE_DAYS,
        "ratio": round(ratio, 2),
        "tokens": total,
        "input_tokens": built["input_tokens"],
        "output_tokens": built["output_tokens"],
        "cache_read_tokens": built["cache_read_tokens"],
        "cache_write_tokens": built["cache_write_tokens"],
        "cost_micros": _cost(pricing, provider, model, built) if price else None,
        "note": note,
    }


def _phase_totals(
    db: Database, tenant_id: str, since: str, project_id: str
) -> dict[str, dict[str, int]]:
    planning = ", ".join("?" for _ in _PLANNING)
    skipped = ", ".join("?" for _ in _NOT_BUILD)
    where = "tenant_id = ? AND day >= ?"
    params: list[str] = [tenant_id, since]
    if project_id:
        where += " AND project_id = ?"
        params.append(project_id)
    totals = {"plan": _empty_tokens(), "after": _empty_tokens()}
    for phase, test in (
        ("plan", f"purpose IN ({planning})"),
        ("after", f"purpose NOT IN ({skipped})"),
    ):
        row = db.fetchone(
            f"""
            SELECT SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
                   SUM(cache_read_tokens) AS cache_read_tokens,
                   SUM(cache_write_tokens) AS cache_write_tokens
            FROM usage_daily WHERE {where} AND {test}
            """,
            (*params, *(_PLANNING if phase == "plan" else _NOT_BUILD)),
        )
        for name in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            totals[phase][name] = int((row or {}).get(name) or 0)
    return totals


# --- reports ---------------------------------------------------------------


@dataclass(frozen=True)
class Report:
    dataset: str
    group_by: tuple[str, ...]
    measures: tuple[str, ...]
    since: str
    until: str
    filters: tuple[tuple[str, str], ...] = ()
    limit: int = 25


def build_report(
    *,
    dataset: str = "tokens",
    group_by: Sequence[str] = (),
    measures: Sequence[str] = (),
    since: str = "",
    until: str = "",
    filters: dict[str, str] | None = None,
    limit: int = 25,
    today: date | None = None,
) -> Report:
    """Check a report request against the registry. Raises ValueError with a reason."""
    chosen = DATASETS.get(dataset)
    if chosen is None:
        raise ValueError(f"unknown dataset: {dataset}")
    groups = tuple(dict.fromkeys(name for name in group_by if name))
    if len(groups) > 2:
        raise ValueError("group by at most two breakdowns")
    for name in groups:
        if name not in chosen.dimensions:
            raise ValueError(f"{chosen.label} cannot be broken down by {name}")
    picked = tuple(dict.fromkeys(name for name in measures if name)) or (chosen.measures[0].key,)
    for name in picked:
        chosen.measure(name)
    end = _day(until) if until else (today or datetime.now(UTC).date())
    start = _day(since) if since else end - timedelta(days=DEFAULT_DAYS - 1)
    if start > end:
        raise ValueError("the start date is after the end date")
    if (end - start).days + 1 > MAX_DAYS:
        raise ValueError(f"a report covers at most {MAX_DAYS} days")
    pairs: list[tuple[str, str]] = []
    for name, value in sorted((filters or {}).items()):
        if not value:
            continue
        if name not in chosen.dimensions or name == "day":
            raise ValueError(f"{chosen.label} cannot be filtered by {name}")
        pairs.append((name, str(value)[:200]))
    return Report(
        dataset=chosen.key,
        group_by=groups,
        measures=picked,
        since=start.isoformat(),
        until=end.isoformat(),
        filters=tuple(pairs),
        limit=max(1, min(int(limit), MAX_GROUPS)),
    )


class _Cache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[tuple, tuple[float, dict]] = {}

    def get(self, key: tuple) -> dict | None:
        now = time.monotonic()
        with self._lock:
            found = self._items.get(key)
            if found is None or found[0] < now:
                self._items.pop(key, None)
                return None
            return found[1]

    def put(self, key: tuple, value: dict) -> None:
        with self._lock:
            if len(self._items) >= _CACHE_LIMIT:
                oldest = min(self._items, key=lambda item: self._items[item][0])
                self._items.pop(oldest, None)
            self._items[key] = (time.monotonic() + CACHE_SECONDS, value)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


_CACHE = _Cache()


def clear_cache() -> None:
    _CACHE.clear()


def run_report(db: Database, tenant_id: str, report: Report, *, cache_key: str = "") -> dict:
    """Totals, the top groups, and a daily series, from one rollup table.

    The same report for the same organization is answered from memory for
    ``CACHE_SECONDS``, so many people opening the dashboard cost one query.
    """
    key = (cache_key, tenant_id, report)
    cached = _CACHE.get(key)
    if cached is not None:
        return {**cached, "cached": True}
    dataset = DATASETS[report.dataset]
    where, params = _where(tenant_id, report)
    measure_sql = ", ".join(f"{dataset.measure(name).sql} AS m_{name}" for name in report.measures)
    total_row = db.fetchone(
        f"SELECT {measure_sql} FROM {dataset.table} WHERE {where}",
        tuple(params),
    )
    totals = {name: int((total_row or {}).get(f"m_{name}") or 0) for name in report.measures}
    rows: list[dict] = []
    if report.group_by:
        columns = [DIMENSIONS[name].column for name in report.group_by]
        selected = ", ".join(
            f"{column} AS d_{name}" for column, name in zip(columns, report.group_by, strict=True)
        )
        order = f"m_{report.measures[0]}"
        found = db.fetchall(
            f"SELECT {selected}, {measure_sql} FROM {dataset.table} WHERE {where} "
            f"GROUP BY {', '.join(columns)} ORDER BY {order} DESC LIMIT ?",
            (*params, report.limit),
        )
        rows = [
            {
                **{name: str(row[f"d_{name}"] or "") for name in report.group_by},
                **{name: int(row[f"m_{name}"] or 0) for name in report.measures},
            }
            for row in found
        ]
    series_rows = db.fetchall(
        f"SELECT day AS d_day, {measure_sql} FROM {dataset.table} WHERE {where} "
        "GROUP BY day ORDER BY day",
        tuple(params),
    )
    series = [
        {"day": str(row["d_day"]), **{name: int(row[f"m_{name}"] or 0) for name in report.measures}}
        for row in series_rows
    ]
    labels = _labels(db, tenant_id, report.group_by, rows)
    result = {
        "dataset": report.dataset,
        "group_by": list(report.group_by),
        "measures": list(report.measures),
        "since": report.since,
        "until": report.until,
        "filters": dict(report.filters),
        "totals": totals,
        "rows": rows,
        "series": series,
        "labels": labels,
        "cached": False,
    }
    _CACHE.put(key, result)
    return result


def report_csv(result: dict) -> str:
    """The report's groups as CSV, with names next to ids."""
    out = io.StringIO()
    writer = csv.writer(out)
    groups = list(result.get("group_by") or [])
    measures = list(result.get("measures") or [])
    labels = result.get("labels") or {}
    header: list[str] = []
    for name in groups:
        header.append(name)
        if name in labels:
            header.append(f"{name}_name")
    writer.writerow([*header, *measures])
    for row in result.get("rows") or []:
        line: list[str] = []
        for name in groups:
            value = str(row.get(name) or "")
            line.append(value)
            if name in labels:
                line.append(str(labels[name].get(value, "")))
        writer.writerow([*line, *(row.get(name, 0) for name in measures)])
    return out.getvalue()


def can_see_org_usage(db: Database, account: dict) -> bool:
    """Owners, admins, and people with the billing duty on the organization."""
    from agent_lbw.runtime.store.assignments import can_manage_org

    if can_manage_org(account):
        return True
    row = db.fetchone(
        """
        SELECT 1 AS found FROM duty_grants
        WHERE identity_id = ? AND duty = 'billing'
          AND scope_type = 'tenant' AND scope_id = ?
        """,
        (account["current_user_id"], account["org"]["id"]),
    )
    return row is not None


def _where(tenant_id: str, report: Report) -> tuple[str, list]:
    clauses = ["tenant_id = ?", "day >= ?", "day <= ?"]
    params: list = [tenant_id, report.since, report.until]
    for name, value in report.filters:
        clauses.append(f"{DIMENSIONS[name].column} = ?")
        params.append(value)
    return " AND ".join(clauses), params


def _labels(db: Database, tenant_id: str, groups: Sequence[str], rows: list[dict]) -> dict:
    labels: dict[str, dict[str, str]] = {}
    for name in groups:
        kind = DIMENSIONS[name].names
        ids = sorted({str(row.get(name) or "") for row in rows} - {""})
        if not kind or not ids:
            continue
        marks = ", ".join("?" for _ in ids)
        if kind == "project":
            found = db.fetchall(
                f"SELECT id, name FROM projects WHERE tenant_id = ? AND id IN ({marks})",
                (tenant_id, *ids),
            )
            labels[name] = {str(row["id"]): str(row["name"] or "") for row in found}
        elif kind == "person":
            found = db.fetchall(
                f"SELECT id, display_name FROM identities WHERE id IN ({marks})",
                tuple(ids),
            )
            labels[name] = {str(row["id"]): str(row["display_name"] or "") for row in found}
    return labels


def _cost(pricing: Pricing, provider: str, model: str, row: dict[str, int]) -> int:
    price, _ = pricing.price(provider, model)
    if price is None:
        return 0
    cache_read = row["cache_read_tokens"]
    cache_write = row["cache_write_tokens"]
    fresh = max(row["input_tokens"] - cache_read - cache_write, 0)
    micros = (
        fresh * int(price["input_micros"] or 0)
        + cache_read * int(price["cache_read_micros"] or 0)
        + cache_write * int(price["cache_write_micros"] or 0)
        + row["output_tokens"] * int(price["output_micros"] or 0)
    )
    return micros // _PER_MILLION


def _provider_from(service: str) -> str:
    host = ""
    try:
        host = urlparse(service if "//" in service else f"//{service}").netloc.lower()
    except ValueError:
        host = ""
    if host.endswith("anthropic.com"):
        return "anthropic"
    if host.endswith("openai.com"):
        return "openai"
    if host.endswith("googleapis.com"):
        return "google"
    return ""


def _empty_tokens() -> dict[str, int]:
    return {
        "calls": 0,
        "errors": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "duration_ms": 0,
        "cost_micros": 0,
    }


def _empty_files() -> dict[str, int]:
    return {"files": 0, "lines_added": 0, "lines_removed": 0}


def _count(value: object) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _day(value: str) -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ValueError(f"not a date: {value}") from exc


def _dollars(micros: object) -> float:
    return round(int(micros or 0) / _PER_MILLION, 6)
