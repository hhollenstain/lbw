import { useEffect, useState } from "react";

import { api } from "../../api";
import { notify } from "../../page";

type LinkApp = {
  configured: boolean;
  client_id: string;
  assertion_file: string;
  has_profile_key: boolean;
  callback_url: string;
  can_manage: boolean;
};

/** Organization settings for "Link your Claude organization" (Claude Platform OAuth, beta). */
export function ClaudeLinkSettings() {
  const [app, setApp] = useState<LinkApp | null>(null);
  const [clientId, setClientId] = useState("");
  const [assertionFile, setAssertionFile] = useState("");
  const [profileKey, setProfileKey] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<LinkApp>("/api/org/claude-link")
      .then((body) => {
        setApp(body);
        setClientId(body.client_id);
        setAssertionFile(body.assertion_file);
      })
      .catch(() => setApp(null));
  }, []);

  async function save() {
    setBusy(true);
    try {
      const body = await api<LinkApp>("/api/org/claude-link", {
        method: "PUT",
        body: JSON.stringify({
          client_id: clientId,
          assertion_file: assertionFile,
          profile_api_key: profileKey,
        }),
      });
      setApp(body);
      setProfileKey("");
      notify("success", "People can now link their Claude organization.", "Claude link");
    } catch (err) {
      notify("error", err instanceof Error ? err.message : "not saved", "Claude link");
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    setBusy(true);
    try {
      setApp(await api<LinkApp>("/api/org/claude-link", { method: "DELETE" }));
    } catch (err) {
      notify("error", err instanceof Error ? err.message : "not removed", "Claude link");
    } finally {
      setBusy(false);
    }
  }

  if (!app) return null;
  return (
    <section className="claude-link-settings" id="claude-link-settings">
      <h2 className="rule-heading">Claude organization link</h2>
      <p className="hint">
        Anthropic does not let other apps use a Claude Pro or Max subscription. With Claude
        Platform OAuth (beta), a person links their Claude organization on their profile, and
        that organization pays for the jobs they run. Register LBW as an OAuth app in the Claude
        Console with this redirect URI: <code>{app.callback_url}</code>. Anthropic issues no
        client secret; LBW signs each token request with a JWT from the identity provider the
        app trusts. A person who links needs the Admin or Billing role in their Claude
        organization.
      </p>
      {app.can_manage ? (
        <form
          className="org-form"
          onSubmit={(event) => {
            event.preventDefault();
            void save();
          }}
        >
          <label htmlFor="claude-link-client">OAuth app client ID</label>
          <input
            type="text"
            id="claude-link-client"
            value={clientId}
            onChange={(event) => setClientId(event.target.value)}
          />
          <label htmlFor="claude-link-assertion">Identity provider JWT file</label>
          <input
            type="text"
            id="claude-link-assertion"
            placeholder="/var/run/secrets/tokens/claude"
            value={assertionFile}
            onChange={(event) => setAssertionFile(event.target.value)}
          />
          <label htmlFor="claude-link-key">API key for user profiles</label>
          <input
            type="password"
            id="claude-link-key"
            autoComplete="off"
            placeholder={app.has_profile_key ? "Saved. Leave empty to keep it." : ""}
            value={profileKey}
            onChange={(event) => setProfileKey(event.target.value)}
          />
          <p className="hint">
            A key from LBW's own Claude organization. It only creates the user profile that
            stands for each person, and it is sealed.
          </p>
          <div className="modal-actions">
            {app.configured ? (
              <button
                type="button"
                className="secondary"
                disabled={busy}
                onClick={() => void remove()}
              >
                Turn off
              </button>
            ) : null}
            <button type="submit" id="save-claude-link" disabled={busy}>
              Save
            </button>
          </div>
        </form>
      ) : (
        <p className="hint">{app.configured ? "Set up." : "Not set up."}</p>
      )}
    </section>
  );
}
