import { useEffect, useState } from "react";

import { api } from "../../api";
import { notify } from "../../page";

type PerMtok = {
  input_per_mtok: number;
  output_per_mtok: number;
  cache_read_per_mtok: number;
  cache_write_per_mtok: number;
};

type Price = PerMtok & { provider: string; model: string };

type ModelPrice = {
  provider: string;
  model: string;
  in_use: boolean;
  source: "override" | "list" | "none";
  price: PerMtok | null;
  list_price: PerMtok | null;
  list_source: string;
};

type Prices = {
  prices: Price[];
  models: ModelPrice[];
  list: {
    fetched: number;
    fetched_models: number;
    source: string;
    source_url: string;
    snapshot_as_of: string;
  };
  can_manage: boolean;
};

const ZERO: PerMtok = {
  input_per_mtok: 0,
  output_per_mtok: 0,
  cache_read_per_mtok: 0,
  cache_write_per_mtok: 0,
};

const EMPTY: Price = { provider: "", model: "", ...ZERO };

const FIELDS: { key: keyof PerMtok; label: string }[] = [
  { key: "input_per_mtok", label: "Input" },
  { key: "output_per_mtok", label: "Output" },
  { key: "cache_read_per_mtok", label: "Cache read" },
  { key: "cache_write_per_mtok", label: "Cache write" },
];

function money(value: number): string {
  // Two places, or up to four for a fraction of a dollar: $0.20, $0.075.
  return value >= 1 || value === 0
    ? `$${value.toFixed(2)}`
    : `$${value.toFixed(4).replace(/0{1,2}$/, "")}`;
}

function SourcePill({ row }: { row: ModelPrice }) {
  if (row.source === "override") {
    return <span className="step-pill step-pill-wait">Your price</span>;
  }
  if (row.source === "list") {
    return (
      <span className="step-pill step-pill-ok" title={row.list_source}>
        List price
      </span>
    );
  }
  return <span className="step-pill">No price</span>;
}

/** The price each model's runs use: the organization's own, or else the public list price. */
export function PricesSection() {
  const [body, setBody] = useState<Prices | null>(null);
  const [draft, setDraft] = useState<Price>(EMPTY);
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    api<Prices>("/api/org/prices")
      .then(setBody)
      .catch(() => setBody(null));
  }, []);

  async function save() {
    try {
      setBody(await api<Prices>("/api/org/prices", { method: "PUT", body: JSON.stringify(draft) }));
      setDraft(EMPTY);
    } catch (err) {
      notify("error", err instanceof Error ? err.message : "not saved", "Prices");
    }
  }

  async function remove(row: { provider: string; model: string }) {
    const query = new URLSearchParams({ provider: row.provider, model: row.model });
    try {
      setBody(await api<Prices>(`/api/org/prices?${query}`, { method: "DELETE" }));
    } catch (err) {
      notify("error", err instanceof Error ? err.message : "not removed", "Prices");
    }
  }

  async function refresh() {
    setRefreshing(true);
    try {
      setBody(await api<Prices>("/api/org/prices/refresh", { method: "POST" }));
      notify("success", "List prices are up to date.", "Prices");
    } catch (err) {
      notify("error", err instanceof Error ? err.message : "not refreshed", "Prices");
    } finally {
      setRefreshing(false);
    }
  }

  function override(row: ModelPrice) {
    setDraft({ provider: row.provider, model: row.model, ...(row.price || row.list_price || ZERO) });
    document.getElementById("price-model")?.focus();
  }

  if (!body) return null;
  const fetched = body.list.fetched
    ? `refreshed ${new Date(body.list.fetched * 1000).toLocaleDateString()}`
    : `as of ${body.list.snapshot_as_of}`;
  return (
    <section className="usage-prices" id="usage-prices">
      <h2 className="rule-heading">Prices</h2>
      <p className="hint">
        Dollars per million tokens. Hosted models use their public list price ({fetched}): the
        provider's own page for Claude and Cursor's models, and the{" "}
        <a href={body.list.source_url} target="_blank" rel="noreferrer">
          {body.list.source}
        </a>{" "}
        for the rest. Set your own price when your contract differs. Cost is fixed when a run
        finishes; changing a price does not rewrite past runs.
      </p>
      {body.can_manage ? (
        <p>
          <button
            type="button"
            className="secondary"
            id="refresh-prices"
            disabled={refreshing}
            onClick={() => void refresh()}
          >
            {refreshing ? "Refreshing…" : "Refresh list prices"}
          </button>
        </p>
      ) : null}
      {body.models.length ? (
        <div className="usage-price-scroll">
          <table className="usage-breakdown" id="price-table">
            <thead>
              <tr>
                <th scope="col">Model</th>
                {FIELDS.map((field) => (
                  <th scope="col" className="usage-number" key={field.key}>
                    {field.label}
                  </th>
                ))}
                {body.can_manage ? (
                  <th scope="col">
                    <span className="sr-only">Actions</span>
                  </th>
                ) : null}
              </tr>
            </thead>
            <tbody>
              {body.models.map((row) => (
                <tr key={`${row.provider}/${row.model}`}>
                  <th scope="row" title={row.model}>
                    {row.model}
                    {row.provider ? <span className="hint"> · {row.provider}</span> : null}
                    <span className="usage-price-source">
                      <SourcePill row={row} />
                    </span>
                  </th>
                  {FIELDS.map((field) => (
                    <td className="usage-number" key={field.key}>
                      {row.price ? money(row.price[field.key]) : "—"}
                    </td>
                  ))}
                  {body.can_manage ? (
                    <td className="usage-price-actions">
                      <button type="button" className="secondary" onClick={() => override(row)}>
                        {row.source === "override" ? "Edit" : row.price ? "Override" : "Set price"}
                      </button>
                      {row.source === "override" ? (
                        <button
                          type="button"
                          className="secondary"
                          onClick={() => void remove(row)}
                        >
                          {row.list_price ? "Use list price" : "Remove"}
                        </button>
                      ) : null}
                    </td>
                  ) : null}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="hint">Add a model in Models and its list price shows here.</p>
      )}
      {body.can_manage ? (
        <form
          className="usage-price-form"
          onSubmit={(event) => {
            event.preventDefault();
            void save();
          }}
        >
          <div className="team-step-field">
            <label htmlFor="price-provider">Provider</label>
            <input
              type="text"
              id="price-provider"
              placeholder="any"
              value={draft.provider}
              onChange={(event) => setDraft({ ...draft, provider: event.target.value })}
            />
          </div>
          <div className="team-step-field">
            <label htmlFor="price-model">Model</label>
            <input
              type="text"
              id="price-model"
              value={draft.model}
              onChange={(event) => setDraft({ ...draft, model: event.target.value })}
            />
          </div>
          {FIELDS.map((field) => (
            <div className="team-step-field" key={field.key}>
              <label htmlFor={`price-${field.key}`}>{field.label} $/M</label>
              <input
                type="number"
                min={0}
                step="any"
                id={`price-${field.key}`}
                value={String(draft[field.key])}
                onChange={(event) =>
                  setDraft({ ...draft, [field.key]: Number(event.target.value) || 0 })
                }
              />
            </div>
          ))}
          <button type="submit" id="save-price" disabled={!draft.model.trim()}>
            Save your price
          </button>
        </form>
      ) : null}
    </section>
  );
}
