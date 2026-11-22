import { useCallback, useEffect, useState } from "react";

import { ApiError, api } from "../../api";
import { notify } from "../../page";

type Provider = { id: string; label: string; base_url: string };
type LocalModel = {
  policy: string;
  allowed: boolean;
  enabled: boolean;
  provider: string;
  base_url: string;
  models: string[];
  synced: number;
  providers: Provider[];
};

/** This person's own model server. LBW reaches it only through their runner. */
export function LocalModelSection() {
  const [data, setData] = useState<LocalModel | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [provider, setProvider] = useState("ollama");
  const [address, setAddress] = useState("");
  const [busy, setBusy] = useState(false);

  const show = useCallback((body: LocalModel) => {
    setData(body);
    setEnabled(body.enabled);
    setProvider(body.provider);
    setAddress(body.base_url);
  }, []);

  const load = useCallback(() => {
    api<LocalModel>("/api/me/local-model")
      .then(show)
      .catch((err) => {
        // Before sign-in the page loads every section. Say nothing until then.
        if (!(err instanceof ApiError && err.status === 401)) {
          notify("error", message(err), "Local models");
        }
      });
  }, [show]);

  useEffect(() => {
    load();
    document.addEventListener("lbw-integrations-show", load);
    return () => document.removeEventListener("lbw-integrations-show", load);
  }, [load]);

  async function act(
    path: string,
    method: string,
    done: (next: LocalModel) => string,
    body?: object,
  ) {
    setBusy(true);
    try {
      const next = await api<LocalModel>(path, {
        method,
        body: body ? JSON.stringify(body) : undefined,
      });
      show(next);
      notify("success", done(next), "Local models");
    } catch (err) {
      notify("error", message(err), "Local models");
    } finally {
      setBusy(false);
    }
  }

  function chooseProvider(id: string) {
    setProvider(id);
    const preset = data?.providers.find((item) => item.id === id)?.base_url;
    if (preset) setAddress(preset);
  }

  if (!data) return null;
  if (!data.allowed) {
    return (
      <div className="runner-panel" id="local-model-panel">
        <h3>Local models</h3>
        <p className="hint">
          Your organization has not turned on local models for you. An owner or admin turns them
          on under Organization, Models.
        </p>
      </div>
    );
  }
  const dirty =
    enabled !== data.enabled || provider !== data.provider || address !== data.base_url;
  return (
    <div className="runner-panel" id="local-model-panel">
      <h3>Local models</h3>
      <p className="hint">
        Run jobs on a model server on your own machine or network. LBW runs the job and sends each model call
        to your runner, which passes it to this address. Your runner relays to this machine and
        your private network, and to a public host only when its <code>llm_hosts</code> setting
        lists it. Nobody else's job reaches your server.
      </p>
      <form
        className="org-form"
        id="local-model-form"
        onSubmit={(event) => {
          event.preventDefault();
          void act("/api/me/local-model", "PUT", () => "Saved your model server.", {
            enabled,
            provider,
            base_url: address,
          });
        }}
      >
        <label className="local-model-switch">
          <span>Use my model server</span>
          <input
            type="checkbox"
            className="org-switch"
            id="local-model-enabled"
            checked={enabled}
            onChange={(event) => setEnabled(event.target.checked)}
          />
        </label>
        <label>
          Server
          <select
            id="local-model-provider"
            value={provider}
            onChange={(event) => chooseProvider(event.target.value)}
          >
            {data.providers.map((item) => (
              <option key={item.id} value={item.id}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Address
          <input
            id="local-model-address"
            type="url"
            autoComplete="off"
            placeholder="http://127.0.0.1:11434"
            value={address}
            onChange={(event) => setAddress(event.target.value)}
          />
        </label>
        <div className="modal-actions local-model-actions">
          <button type="submit" disabled={busy || !dirty}>
            Save
          </button>
          <button
            type="button"
            className="secondary"
            id="local-model-sync"
            disabled={busy || dirty || !data.enabled}
            onClick={() =>
              void act(
                "/api/me/local-model/sync",
                "POST",
                (next) => `Found ${next.models.length} model${next.models.length === 1 ? "" : "s"}.`,
              )
            }
          >
            Sync models
          </button>
        </div>
      </form>
      {data.models.length ? (
        <>
          <p className="hint" id="local-model-synced">
            {`Synced ${new Date(data.synced * 1000).toLocaleString()}. These show on the job form as "your machine".`}
          </p>
          <ul className="local-model-names" id="local-model-names">
            {data.models.map((name) => (
              <li key={name}>
                <code>{name}</code>
              </li>
            ))}
          </ul>
        </>
      ) : (
        <p className="hint" id="local-model-synced">
          {data.enabled
            ? "No models synced yet. Start lbw-runner, then sync."
            : "Turn it on and save, then sync its models."}
        </p>
      )}
    </div>
  );
}

function message(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
