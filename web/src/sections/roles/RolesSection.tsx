import { useEffect, useState } from "react";

import { api } from "../../api";
import { notify, workspacePath } from "../../page";
import {
  type AgentTeam,
  type GraphWave,
  type ModelChoice,
  StepGraph,
  type TeamDraft,
  TeamDialog,
  draftFor,
  emptyDraft,
  runsOn,
  stepKeys,
  stepSummary,
  teamPayload,
} from "../teams/TeamEditor";

type Role = {
  name: string;
  title?: string;
  job?: string;
  team?: string;
  summary?: string;
  description?: string;
  enabled?: boolean;
  required?: boolean;
  enforced?: boolean;
};

type RolesPayload = {
  roles?: Role[];
  agent_teams?: AgentTeam[];
  models?: ModelChoice[];
  can_manage?: boolean;
  can_edit_teams?: boolean;
  graph?: GraphWave[];
  graph_error?: string;
  uses_roles?: boolean;
};

const TEAMS = [
  {
    name: "Plan",
    note: "Plans the proposal, then it pauses for a person with write access.",
  },
  {
    name: "Build",
    note: "Writes the change.",
  },
  {
    name: "Review",
    note: "Reviews the change, then it pauses again for a person with write access.",
  },
];

export function RolesSection() {
  const [payload, setPayload] = useState<RolesPayload>({});
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<AgentTeam | null>(null);
  const [draft, setDraft] = useState<TeamDraft>(emptyDraft());

  useEffect(() => {
    function onRoles(event: Event) {
      const detail = (event as CustomEvent<RolesPayload>).detail || {};
      setPayload(detail);
      paintStatus(detail);
    }
    const cached = (window as RolesWindow).__lbwProjectRoles;
    if (cached) onRoles(new CustomEvent("lbw-project-roles", { detail: cached }));
    document.addEventListener("lbw-project-roles", onRoles);
    return () => document.removeEventListener("lbw-project-roles", onRoles);
  }, []);

  const listed = payload.roles || [];
  const teams = payload.agent_teams || [];
  const models = payload.models || [];
  const canManage = Boolean(payload.can_manage);
  const canEdit = Boolean(payload.can_edit_teams);
  const usesRoles = payload.uses_roles !== false;

  async function save(next: Role[]) {
    const enabled = next.filter((role) => role.enabled || role.enforced).map((role) => role.name);
    try {
      const body = await api<RolesPayload>("/api/workspaces/roles", {
        method: "PUT",
        body: JSON.stringify({ path: workspacePath(), enabled }),
      });
      publishRoles(body);
    } catch (err) {
      notify(
        "error",
        err instanceof Error ? err.message : "failed to update agent teams",
        "Could not update agent teams",
      );
    }
  }

  function openTeam(team: AgentTeam | null) {
    setEditing(team);
    setDraft(team ? draftFor(team) : emptyDraft());
    setOpen(true);
  }

  async function saveTeam() {
    const body = teamPayload(draft);
    try {
      if (editing) {
        await api(`/api/org/agent-teams/${encodeURIComponent(editing.id)}`, {
          method: "PUT",
          body: JSON.stringify(body),
        });
      } else {
        await api("/api/org/agent-teams", {
          method: "POST",
          body: JSON.stringify({ ...body, job: body.steps[0]?.job || "review" }),
        });
      }
      await reload();
      setOpen(false);
      setEditing(null);
    } catch (err) {
      notify(
        "error",
        err instanceof Error ? err.message : "could not save the team",
        "Could not save the team",
      );
    }
  }

  async function toggleTeam(team: AgentTeam, enabled: boolean) {
    try {
      const body = await api<RolesPayload>("/api/workspaces/agent-teams", {
        method: "PUT",
        body: JSON.stringify({ path: workspacePath(), team_id: team.id, enabled }),
      });
      publishRoles(body);
    } catch (err) {
      notify(
        "error",
        err instanceof Error ? err.message : "could not update the team",
        "Could not update the team",
      );
    }
  }

  async function importRoles() {
    try {
      const body = await api<RolesPayload>("/api/workspaces/agent-teams/import-roles", {
        method: "POST",
        body: JSON.stringify({ path: workspacePath() }),
      });
      publishRoles(body);
      notify("success", "This project now runs its roles as agent teams.", "Agent teams");
    } catch (err) {
      notify(
        "error",
        err instanceof Error ? err.message : "could not turn the roles into teams",
        "Could not turn the roles into teams",
      );
    }
  }

  return (
    <>
      <div className="org-section-head">
        <h2 className="rule-heading">Teams</h2>
        {canEdit ? (
          <button type="button" id="add-agent-team" onClick={() => openTeam(null)}>
            Add a team
          </button>
        ) : null}
      </div>
      {teams.map((team) => (
        <div className="skill-row" key={team.id}>
          <input
            type="checkbox"
            className="org-switch"
            checked={Boolean(team.enabled)}
            disabled={!canManage}
            data-agent-team={team.id}
            aria-label={`Enable ${team.name}`}
            onChange={(event) => void toggleTeam(team, event.target.checked)}
          />
          <div className="skill-row-copy">
            <strong title={team.instruction || team.name}>{team.name}</strong>
            <span className="hint">
              {stepSummary(team)} · runs on {runsOn(team)}
            </span>
          </div>
          {canEdit ? (
            <div className="org-row-actions">
              <button
                type="button"
                className="secondary"
                data-edit-agent-team={team.id}
                onClick={() => openTeam(team)}
              >
                Edit
              </button>
            </div>
          ) : null}
        </div>
      ))}
      {usesRoles ? (
        <div className="team-import">
          <p className="hint">
            This project runs the roles below. Turn them into agent teams to give each its own
            model and to choose which steps wait for which.
          </p>
          {canEdit && listed.some((role) => role.enabled || role.enforced) ? (
            <button
              type="button"
              className="secondary"
              id="import-roles-as-teams"
              onClick={() => void importRoles()}
            >
              Turn roles into teams
            </button>
          ) : null}
        </div>
      ) : (
        <>
          <h3 className="rule-heading">How the steps run</h3>
          <StepGraph graph={payload.graph || []} error={payload.graph_error} />
          <p className="hint">
            Where a team that is on plans or reviews, it replaces the roles below for that
            phase. A phase no team covers still runs its roles, and roles the organization
            enforces always run.
          </p>
        </>
      )}
      <TeamDialog
        id="agent-team-dialog"
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
        const tasks = listed.filter((role) => (role.team || teamFor(role.job)) === team.name);
        if (!tasks.length) return null;
        return (
          <section key={team.name}>
            <h2 className="rule-heading" title={team.note}>
              {team.name}
            </h2>
            {tasks.map((role) => {
              const id = `role-enable-${role.name}`;
              const locked = Boolean(role.required || role.enforced);
              const tip = role.enforced
                ? "Enforced by the organization."
                : role.required
                  ? "Builder and QA stay on."
                  : role.summary || role.description || role.name;
              return (
                <div className="skill-row" key={role.name}>
                  <input
                    type="checkbox"
                    className="org-switch"
                    id={id}
                    checked={Boolean(role.enabled || role.enforced)}
                    disabled={locked}
                    data-role={role.name}
                    aria-label={`Enable ${role.title || role.name}`}
                    title={locked ? tip : undefined}
                    onChange={(event) => {
                      const enabled = event.target.checked;
                      void save(
                        listed.map((item) =>
                          item.name === role.name ? { ...item, enabled } : item,
                        ),
                      );
                    }}
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

async function reload() {
  const body = await api<RolesPayload>(
    `/api/workspaces/roles?path=${encodeURIComponent(workspacePath())}`,
  );
  publishRoles(body);
}

function paintStatus(body: RolesPayload) {
  const status = document.getElementById("roles-status");
  if (!status) return;
  const on =
    body.uses_roles === false
      ? []
      : (body.roles || []).filter((role) => role.enabled).map((role) => role.title || role.name);
  for (const team of body.agent_teams || []) {
    if (team.enabled) on.push(team.name);
  }
  status.textContent = on.length
    ? `On for this project: ${on.join(", ")}`
    : "No tasks are on for this project.";
}

function publishRoles(body: RolesPayload) {
  const page = window as RolesWindow;
  page.__lbwProjectRoles = body;
  document.dispatchEvent(new CustomEvent("lbw-project-roles", { detail: body }));
}

type RolesWindow = Window & {
  __lbwProjectRoles?: RolesPayload;
};
