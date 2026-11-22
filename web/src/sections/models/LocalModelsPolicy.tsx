import { useCallback, useEffect, useState } from "react";

import { ApiError, api } from "../../api";
import { notify } from "../../page";

type Person = { id: string; name: string; allowed: boolean; enabled: boolean; models: number };
type Policy = { policy: string; can_manage: boolean; people: Person[] };

const POLICY_LABELS: Record<string, string> = {
  off: "Off",
  everyone: "On for everyone",
  chosen: "On for chosen people",
};

/** Who may run jobs on a model server on their own machine, such as Ollama. */
export function LocalModelsPolicy() {
  const [data, setData] = useState<Policy | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api<Policy>("/api/org/local-models")
      .then(setData)
      .catch((err) => {
        // Before sign-in the page loads every section. Say nothing until then.
        if (!(err instanceof ApiError && err.status === 401)) {
          notify("error", message(err), "Local models");
        }
      });
  }, []);

  useEffect(() => {
    load();
    document.addEventListener("lbw-org-settings", load);
    return () => document.removeEventListener("lbw-org-settings", load);
  }, [load]);

  async function save(path: string, body: object, done: string) {
    setBusy(true);
    try {
      setData(await api<Policy>(path, { method: "PUT", body: JSON.stringify(body) }));
      notify("success", done, "Local models");
    } catch (err) {
      notify("error", message(err), "Local models");
      load();
    } finally {
      setBusy(false);
    }
  }

  if (!data) return null;
  return (
    <div className="local-models-policy">
      <div className="org-section-head">
        <h2 className="rule-heading">Local models</h2>
      </div>
      <p className="hint">
        A person it is on for sets the address of a model server on their own machine, such as
        Ollama, LM Studio, or vLLM, under Profile, Integrations, and syncs its models. LBW runs the
        job, and each model call goes through that person's own runner to their server. Nobody
        else's job reaches it.
      </p>
      {data.can_manage ? (
        <label className="local-models-choice">
          Who can use them
          <select
            id="local-models-policy"
            value={data.policy}
            disabled={busy}
            onChange={(event) =>
              void save(
                "/api/org/local-models",
                { policy: event.target.value },
                `Local models: ${POLICY_LABELS[event.target.value].toLowerCase()}.`,
              )
            }
          >
            {Object.entries(POLICY_LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
      ) : (
        <p className="hint">{POLICY_LABELS[data.policy] || "Off"}.</p>
      )}
      {data.can_manage && data.policy === "chosen" ? (
        <table className="org-table" id="local-models-people">
          <thead>
            <tr>
              <th>Person</th>
              <th>Server</th>
              <th>On</th>
            </tr>
          </thead>
          <tbody>
            {data.people.map((person) => (
              <tr key={person.id}>
                <td>{person.name}</td>
                <td>
                  {person.enabled
                    ? `${person.models} model${person.models === 1 ? "" : "s"}`
                    : "Not set up"}
                </td>
                <td>
                  <input
                    type="checkbox"
                    className="org-switch"
                    checked={person.allowed}
                    disabled={busy}
                    data-local-person={person.id}
                    aria-label={`Local models for ${person.name}`}
                    onChange={(event) =>
                      void save(
                        `/api/org/local-models/people/${encodeURIComponent(person.id)}`,
                        { allowed: event.target.checked },
                        `Local models ${event.target.checked ? "on" : "off"} for ${person.name}.`,
                      )
                    }
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </div>
  );
}

function message(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
