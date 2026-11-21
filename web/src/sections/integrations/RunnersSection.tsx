import { useCallback, useEffect, useState } from "react";

import { api } from "../../api";
import { notify } from "../../page";

type Runner = {
  id: string;
  name: string;
  owner: string;
  mine: boolean;
  platform: string;
  version: string;
  claude_version: string;
  claude_ready: boolean;
  online: boolean;
  last_seen: number;
  pending_command: string;
};

type Runners = { runners: Runner[]; can_manage: boolean };

/** This person's own machines. A job on a "your machine" model goes to one
 * that is online and runs there with their own Claude Code. */
export function RunnersSection() {
  const [data, setData] = useState<Runners | null>(null);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api<Runners>("/api/me/runners")
      .then(setData)
      .catch((err) => notify("error", message(err), "Your machines"));
  }, []);

  useEffect(() => {
    load();
    const timer = window.setInterval(load, 30_000);
    document.addEventListener("lbw-integrations-show", load);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("lbw-integrations-show", load);
    };
  }, [load]);

  async function act(path: string, method: string, done: string, body?: object) {
    setBusy(true);
    try {
      const next = await api<Runners & { approved?: { name: string } }>(path, {
        method,
        body: body ? JSON.stringify(body) : undefined,
      });
      setData(next);
      notify("success", next.approved ? `Linked ${next.approved.name}.` : done, "Your machines");
      return true;
    } catch (err) {
      notify("error", message(err), "Your machines");
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function approve() {
    if (await act("/api/me/runners/approve", "POST", "Linked.", { user_code: code })) {
      setCode("");
    }
  }

  return (
    <div className="runner-panel">
      <h3>Your machines</h3>
      <p className="hint">
        Install <code>lbw-runner</code> on your computer, sign in to Claude Code there, then run{" "}
        <code>lbw-runner login --server {window.location.origin}</code>. Enter the code it shows
        here. Jobs on a "your machine" model then run with your own Claude Code, and LBW never sees
        your Claude login.
      </p>
      <div className="runner-approve">
        <label htmlFor="runner-code">Code from lbw-runner</label>
        <input
          id="runner-code"
          type="text"
          autoComplete="off"
          placeholder="ABCD-EFGH"
          value={code}
          onChange={(event) => setCode(event.target.value.toUpperCase())}
        />
        <button
          type="button"
          disabled={busy || code.replace(/[^A-Z0-9]/g, "").length !== 8}
          onClick={() => void approve()}
        >
          Link this machine
        </button>
      </div>
      {!data ? <p className="hint">Loading machines…</p> : null}
      {data && !data.runners.length ? <p className="hint">No machine is linked yet.</p> : null}
      {data?.runners.map((runner) => (
        <div className="skill-row integration-row" key={runner.id} data-runner={runner.id}>
          <div className="skill-row-copy">
            <strong>
              {runner.name}
              {!runner.mine && runner.owner ? ` (${runner.owner})` : ""}
            </strong>
            <span className="hint">
              {runner.online ? "Online" : "Offline"}
              {runner.platform ? ` · ${runner.platform}` : ""}
              {runner.version ? ` · runner ${runner.version}` : ""}
              {" · "}
              {runner.claude_ready
                ? `Claude Code ${runner.claude_version}`
                : "Claude Code not found"}
              {runner.pending_command ? " · update waiting" : ""}
            </span>
          </div>
          <div className="org-row-actions">
            <button
              type="button"
              className="secondary"
              disabled={busy || !runner.online}
              data-runner-update={runner.id}
              onClick={() =>
                void act(
                  `/api/me/runners/${encodeURIComponent(runner.id)}/update-claude`,
                  "POST",
                  `${runner.name} will update Claude Code at its next check-in.`,
                )
              }
            >
              Update Claude Code
            </button>
            <button
              type="button"
              className="secondary"
              disabled={busy}
              data-runner-remove={runner.id}
              onClick={() =>
                void act(
                  `/api/me/runners/${encodeURIComponent(runner.id)}`,
                  "DELETE",
                  `Disconnected ${runner.name}.`,
                )
              }
            >
              Disconnect
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}

function message(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
