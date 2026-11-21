import { useCallback, useEffect, useState } from "react";

import { api } from "../../api";
import { notify } from "../../page";

type Integration = {
  endpoint_id: string;
  name: string;
  provider: string;
  auth: string;
  linked: boolean;
  linked_with: string;
  can_add_key: boolean;
  can_sign_in: boolean;
  can_link_claude: boolean;
};

type Mine = { integrations: Integration[]; claude_link_ready: boolean };

const LINKED_WITH: Record<string, string> = {
  key: "your API key",
  sso: "your sign-in",
  claude_link: "your Claude organization",
};

/** The integrations the organization turned on, and this person's link to each.
 * A job they start, or continue at a gate, runs on these credentials. */
export function IntegrationsSection() {
  const [mine, setMine] = useState<Mine | null>(null);
  const [keyFor, setKeyFor] = useState("");
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api<Mine>("/api/me/integrations")
      .then(setMine)
      .catch((err) => notify("error", message(err), "Integrations"));
  }, []);

  useEffect(() => {
    load();
    const params = new URLSearchParams(window.location.search);
    if (params.get("link_error")) {
      notify("error", "Claude did not link. Try again, or ask an admin.", "Integrations");
    }
    function onShow() {
      load();
    }
    document.addEventListener("lbw-integrations-show", onShow);
    return () => document.removeEventListener("lbw-integrations-show", onShow);
  }, [load]);

  async function saveKey(item: Integration) {
    setBusy(true);
    try {
      await api(`/api/org/endpoints/${encodeURIComponent(item.endpoint_id)}/credential`, {
        method: "POST",
        body: JSON.stringify({ api_key: key }),
      });
      setKey("");
      setKeyFor("");
      load();
      notify("success", `${item.name} is linked with your API key.`, "Integrations");
    } catch (err) {
      notify("error", message(err), "Could not check that key");
    } finally {
      setBusy(false);
    }
  }

  async function linkClaude(item: Integration) {
    setBusy(true);
    try {
      const body = await api<{ url: string }>(
        `/api/me/integrations/${encodeURIComponent(item.endpoint_id)}/claude-link`,
        { method: "POST" },
      );
      window.location.assign(body.url);
    } catch (err) {
      notify("error", message(err), "Could not start the Claude link");
      setBusy(false);
    }
  }

  async function unlinkIt(item: Integration) {
    setBusy(true);
    try {
      const body = await api<Mine>(
        `/api/me/integrations/${encodeURIComponent(item.endpoint_id)}`,
        { method: "DELETE" },
      );
      setMine(body);
    } catch (err) {
      notify("error", message(err), "Could not unlink");
    } finally {
      setBusy(false);
    }
  }

  if (!mine) return <p className="hint">Loading integrations…</p>;
  if (!mine.integrations.length) {
    return (
      <p className="hint">
        No integration asks for your own account yet. An owner or admin turns one on by setting a
        model endpoint to personal keys or sign-in.
      </p>
    );
  }
  return (
    <div className="integration-list">
      {mine.integrations.map((item) => (
        <div className="skill-row integration-row" key={item.endpoint_id}>
          <div className="skill-row-copy">
            <strong>{item.name}</strong>
            <span className="hint">
              {item.linked
                ? `Linked with ${LINKED_WITH[item.linked_with] || "your account"}. Jobs you start or continue use it.`
                : "Not linked. You cannot start or continue a job on this model until you link it."}
            </span>
            {keyFor === item.endpoint_id ? (
              <div className="integration-key">
                <label htmlFor={`integration-key-${item.endpoint_id}`}>API key</label>
                <input
                  type="password"
                  id={`integration-key-${item.endpoint_id}`}
                  autoComplete="off"
                  value={key}
                  onChange={(event) => setKey(event.target.value)}
                />
                <div className="modal-actions">
                  <button
                    type="button"
                    className="secondary"
                    onClick={() => {
                      setKeyFor("");
                      setKey("");
                    }}
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    disabled={busy || !key.trim()}
                    onClick={() => void saveKey(item)}
                  >
                    Check and save
                  </button>
                </div>
              </div>
            ) : null}
          </div>
          <div className="org-row-actions">
            {item.can_add_key && keyFor !== item.endpoint_id ? (
              <button
                type="button"
                className="secondary"
                data-integration-key={item.endpoint_id}
                disabled={busy}
                onClick={() => setKeyFor(item.endpoint_id)}
              >
                {item.linked_with === "key" ? "Replace key" : "Add API key"}
              </button>
            ) : null}
            {item.can_sign_in ? (
              <a
                className="secondary button-link"
                href={`/api/org/endpoints/${encodeURIComponent(item.endpoint_id)}/sso`}
              >
                Sign in
              </a>
            ) : null}
            {item.can_link_claude ? (
              <button
                type="button"
                className="secondary"
                data-integration-claude={item.endpoint_id}
                disabled={busy}
                onClick={() => void linkClaude(item)}
              >
                Link your Claude organization
              </button>
            ) : null}
            {item.linked ? (
              <button
                type="button"
                className="secondary"
                data-integration-unlink={item.endpoint_id}
                disabled={busy}
                onClick={() => void unlinkIt(item)}
              >
                Unlink
              </button>
            ) : null}
          </div>
        </div>
      ))}
      {mine.integrations.some((item) => item.provider === "anthropic") &&
      !mine.claude_link_ready ? (
        <p className="hint">
          To pay for Claude from your own Claude organization instead of a key, an owner or admin
          sets up the Claude link under Organization, Models.
        </p>
      ) : null}
    </div>
  );
}

function message(err: unknown) {
  return err instanceof Error ? err.message : "request failed";
}
