import { useCallback, useEffect, useState } from "react";

import { ApiError, api } from "../../api";
import { notify, workspacePath } from "../../page";

type Policy = {
  required: boolean;
  org_required?: boolean;
  project_required?: boolean;
  can_manage: boolean;
};

type Props = { scope: "org" | "project" };

/** Whether proposals must start from a spec, for the organization or one project. */
export function SpecPolicy({ scope }: Props) {
  const [data, setData] = useState<Policy | null>(null);
  const [busy, setBusy] = useState(false);
  const org = scope === "org";

  const load = useCallback(() => {
    const path = org
      ? "/api/org/specs"
      : `/api/workspaces/specs?path=${encodeURIComponent(workspacePath())}`;
    api<Policy>(path)
      .then(setData)
      .catch((err) => {
        // Before sign-in the page loads every section. Say nothing until then.
        if (!(err instanceof ApiError && err.status === 401)) {
          notify("error", message(err), "Specs");
        }
      });
  }, [org]);

  useEffect(() => {
    load();
    const event = org ? "lbw-org-settings" : "lbw-project-roles";
    document.addEventListener(event, load);
    return () => document.removeEventListener(event, load);
  }, [load, org]);

  async function save(required: boolean) {
    setBusy(true);
    try {
      const next = org
        ? await api<Policy>("/api/org/specs", {
            method: "PUT",
            body: JSON.stringify({ required }),
          })
        : await api<Policy>("/api/workspaces/specs", {
            method: "PUT",
            body: JSON.stringify({ path: workspacePath(), required }),
          });
      setData(next);
      document.dispatchEvent(new CustomEvent("lbw-spec-policy"));
      notify("success", required ? "A spec is required" : "A spec is optional", "Specs");
    } catch (err) {
      notify("error", message(err), "Specs");
      load();
    } finally {
      setBusy(false);
    }
  }

  if (!data) return null;
  const lockedByOrg = !org && Boolean(data.org_required);
  const checked = org ? data.required : Boolean(data.project_required) || lockedByOrg;
  const id = org ? "org-spec-required" : "project-spec-required";
  return (
    <div className="spec-policy">
      <h2 className="rule-heading">Spec-driven proposals</h2>
      <p className="hint">
        A spec-driven proposal first writes a spec: the problem, numbered requirements, and
        acceptance criteria. It pauses until a person with write access edits and accepts it, then
        the plan, the build, and QA follow it. The spec is committed in <code>specs/</code> with the
        change. Otherwise each proposal chooses with "write a spec first".
      </p>
      <label className="spec-policy-choice" htmlFor={id}>
        <input
          id={id}
          type="checkbox"
          className="org-switch"
          checked={checked}
          disabled={busy || !data.can_manage || lockedByOrg}
          onChange={(event) => void save(event.target.checked)}
        />
        {org ? "Require a spec on every project" : "Require a spec for this project's proposals"}
      </label>
      {lockedByOrg ? (
        <p className="hint">Your organization requires a spec on every project.</p>
      ) : null}
      {!data.can_manage ? (
        <p className="hint">
          {org
            ? "An organization owner or admin changes this."
            : "A person who manages this project changes this."}
        </p>
      ) : null}
    </div>
  );
}

function message(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
