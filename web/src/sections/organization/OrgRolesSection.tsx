import { useEffect, useState } from "react";

import { api } from "../../api";
import { notify } from "../../page";
import {
  type AgentTeam,
  type ModelChoice,
  type TeamDraft,
  TeamDialog,
  draftFor,
  emptyDraft,
  runsOn,
  stepKeys,
  stepSummary,
  teamPayload,
} from "../teams/TeamEditor";

type OrgRole = {
  name: string;
  title?: string;
  job?: string;
  team?: string;
  summary?: string;
  description?: string;
  required?: boolean;
};

type OrgRoles = {
  can_manage?: boolean;
  roles?: OrgRole[];
  enforced?: string[];
  agent_teams?: AgentTeam[];
  models?: ModelChoice[];
};

const TEAMS = ["Plan", "Build", "Review"];

export function OrgRolesSection() {
  const [body, setBody] = useState<OrgRoles | null>(null);
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<AgentTeam | null>(null);
  const [draft, setDraft] = useState<TeamDraft>(emptyDraft());

  useEffect(() => {
    function onSettings(event: Event) {
      setBody((event as CustomEvent<OrgRoles>).detail || {});
    }
    const cached = (window as OrgRolesWindow).__lbwOrgSettings;
    if (cached) setBody(cached);
    document.addEventListener("lbw-org-settings", onSettings);
    return () => document.removeEventListener("lbw-org-settings", onSettings);
  }, []);

  const roles = body?.roles || [];
  const teams = body?.agent_teams || [];
  const models = body?.models || [];
  const enforced = new Set(body?.enforced || []);
  const manage = Boolean(body?.can_manage);

  function openTeam(team: AgentTeam | null) {
    setEditing(team);
    setDraft(team ? draftFor(team) : emptyDraft());
    setOpen(true);
  }

  async function saveTeam() {
    const payload = teamPayload(draft);
    try {
      const next = editing
        ? await api<OrgRoles>(`/api/org/agent-teams/${encodeURIComponent(editing.id)}`, {
            method: "PUT",
            body: JSON.stringify(payload),
          })
        : await api<OrgRoles>("/api/org/agent-teams", {
            method: "POST",
            body: JSON.stringify({ ...payload, job: payload.steps[0]?.job || "review" }),
          });
      (window as OrgRolesWindow).renderOrgSettings?.(next);
      setOpen(false);
      setEditing(null);
    } catch (err) {
      notify(
        "error",
        err instanceof Error ? err.message : "could not save the team",
        "Organization",
      );
    }
  }

  async function removeTeam(team: AgentTeam) {
    try {
      const next = await api<OrgRoles>(`/api/org/agent-teams/${encodeURIComponent(team.id)}`, {
        method: "DELETE",
      });
      (window as OrgRolesWindow).renderOrgSettings?.(next);
    } catch (err) {
      notify(
        "error",
        err instanceof Error ? err.message : "could not remove the team",
        "Organization",
      );
    }
  }

  async function save(name: string, on: boolean) {
    try {
      const next = await api<OrgRoles>("/api/org/roles", {
        method: "PUT",
        body: JSON.stringify({ role_name: name, enforced: on }),
      });
      (window as OrgRolesWindow).renderOrgSettings?.(next);
    } catch (err) {
      notify(
        "error",
        err instanceof Error ? err.message : "could not update the task",
        "Organization",
      );
    }
  }

  return (
    <>
      <div className="org-section-head">
        <h2 className="rule-heading">Created teams</h2>
        {manage ? (
          <button type="button" id="add-org-agent-team" onClick={() => openTeam(null)}>
            Add a team
          </button>
        ) : null}
      </div>
      {teams.map((team) => (
        <div className="skill-row" key={team.id}>
          <div className="skill-row-copy">
            <strong title={team.instruction || team.name}>{team.name}</strong>
            <span className="hint">
              {stepSummary(team)} · runs on {runsOn(team)}
            </span>
          </div>
          {manage ? (
            <div className="org-row-actions">
              <button
                type="button"
                className="secondary"
                data-edit-agent-team={team.id}
                onClick={() => openTeam(team)}
              >
                Edit
              </button>
              <button
                type="button"
                className="secondary"
                data-remove-agent-team={team.id}
                onClick={() => void removeTeam(team)}
              >
                Remove
              </button>
            </div>
          ) : null}
        </div>
      ))}
      <TeamDialog
        id="org-agent-team-dialog"
        open={open}
        editing={editing !== null}
        draft={draft}
        models={models}
        knownSteps={stepKeys(teams, editing?.id)}
        onChange={setDraft}
        onClose={() => {
          setOpen(false);
          setEditing(null);
        }}
        onSave={() => void saveTeam()}
      />
      {TEAMS.map((team) => {
        const tasks = roles.filter((role) => (role.team || teamFor(role.job)) === team);
        if (!tasks.length) return null;
        return (
          <section key={team}>
            <h2 className="rule-heading">{team}</h2>
            {tasks.map((role) => {
              const tip = role.required
                ? "Builder and QA stay on for a project. This switch keeps the task on everywhere."
                : role.summary || role.description || role.name;
              return (
                <div className="skill-row" key={role.name}>
                  <input
                    type="checkbox"
                    className="org-switch"
                    checked={enforced.has(role.name)}
                    disabled={!manage}
                    data-org-role={role.name}
                    aria-label={`Enforce ${role.title || role.name}`}
                    onChange={(event) => void save(role.name, event.target.checked)}
                  />
                  <div className="skill-row-copy">
                    <strong title={tip}>{role.title || role.name}</strong>
                  </div>
                </div>
              );
            })}
          </section>
        );
      })}
    </>
  );
}

function teamFor(job: string | undefined) {
  if (job === "plan") return "Plan";
  if (job === "build") return "Build";
  return "Review";
}

type OrgRolesWindow = Window & {
  __lbwOrgSettings?: OrgRoles;
  renderOrgSettings?: (body: OrgRoles) => void;
};
