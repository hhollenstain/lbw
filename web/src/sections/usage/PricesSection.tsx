import { useEffect, useState } from "react";

import { api } from "../../api";
import { notify } from "../../page";

type Price = {
  provider: string;
  model: string;
  input_per_mtok: number;
  output_per_mtok: number;
  cache_read_per_mtok: number;
  cache_write_per_mtok: number;
};

type Prices = { prices: Price[]; can_manage: boolean };

const EMPTY: Price = {
  provider: "",
  model: "",
  input_per_mtok: 0,
  output_per_mtok: 0,
  cache_read_per_mtok: 0,
  cache_write_per_mtok: 0,
};

const FIELDS: { key: keyof Price; label: string }[] = [
  { key: "input_per_mtok", label: "Input" },
  { key: "output_per_mtok", label: "Output" },
  { key: "cache_read_per_mtok", label: "Cache read" },
  { key: "cache_write_per_mtok", label: "Cache write" },
];

/** Dollars per million tokens, per model. A saved price counts toward runs after it. */
export function PricesSection() {
  const [body, setBody] = useState<Prices | null>(null);
  const [draft, setDraft] = useState<Price>(EMPTY);

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

  async function remove(price: Price) {
    const query = new URLSearchParams({ provider: price.provider, model: price.model });
    try {
      setBody(await api<Prices>(`/api/org/prices?${query}`, { method: "DELETE" }));
    } catch (err) {
      notify("error", err instanceof Error ? err.message : "not removed", "Prices");
    }
  }

  if (!body) return null;
  return (
    <section className="usage-prices" id="usage-prices">
      <h2 className="rule-heading">Prices</h2>
      <p className="hint">
        Dollars per million tokens, from your provider's price page. Estimated cost uses the price
        saved when a run finishes; changing a price does not rewrite past runs. A blank provider
        prices that model on any provider.
      </p>
      {body.prices.length ? (
        <table className="usage-breakdown">
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
            {body.prices.map((price) => (
              <tr key={`${price.provider}/${price.model}`}>
                <th scope="row">
                  {price.model}
                  {price.provider ? <span className="hint"> · {price.provider}</span> : null}
                </th>
                {FIELDS.map((field) => (
                  <td className="usage-number" key={field.key}>
                    ${Number(price[field.key]).toFixed(2)}
                  </td>
                ))}
                {body.can_manage ? (
                  <td>
                    <button
                      type="button"
                      className="secondary"
                      onClick={() => {
                        setDraft(price);
                      }}
                    >
                      Edit
                    </button>{" "}
                    <button type="button" className="secondary" onClick={() => void remove(price)}>
                      Remove
                    </button>
                  </td>
                ) : null}
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="hint">No prices yet, so cost shows as $0.</p>
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
              placeholder="anthropic"
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
                step="0.01"
                id={`price-${field.key}`}
                value={String(draft[field.key])}
                onChange={(event) =>
                  setDraft({ ...draft, [field.key]: Number(event.target.value) || 0 })
                }
              />
            </div>
          ))}
          <button type="submit" id="save-price" disabled={!draft.model.trim()}>
            Save price
          </button>
        </form>
      ) : null}
    </section>
  );
}
