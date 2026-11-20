import { useEffect, useState } from "react";
import { createPortal } from "react-dom";

export type TeamStep = {
  key: string;
  job: string;
  after?: string[];
  paths?: string[];
};

export type AgentTeam = {
  id: string;
  name: string;
  job?: string;
  team?: string;
  instruction?: string;
  model_id?: string;
  model_name?: string;
  service?: string;
  enabled?: boolean;
  steps?: TeamStep[];
};

export type ModelChoice = {
  id: string;
  name: string;
  service?: string;
  endpoint_name?: string;
  provider?: string;
};

export type GraphWave = {
  job: string;
  wave: number;
  steps: string[];
  parallel: boolean;
};

export type DraftStep = {
  key: string;
  job: string;
  after: string;
  paths: string;
};

export type TeamDraft = {
  name: string;
  instruction: string;
  model_id: string;
  steps: DraftStep[];
};

const JOB_LABELS: Record<string, string> = {
  plan: "Plans",
  build: "Writes code",
  review: "Reviews",
};

const PHASES = [
  { job: "plan", name: "Plan" },
  { job: "build", name: "Build" },
  { job: "review", name: "Review" },
];

export function emptyDraft(): TeamDraft {
  return {
    name: "",
    instruction: "",
    model_id: "",
    steps: [{ key: "", job: "review", after: "", paths: "" }],
  };
}

export function draftFor(team: AgentTeam): TeamDraft {
  return {
    name: team.name,
    instruction: team.instruction || "",
    model_id: team.model_id || "",
    steps: (team.steps || []).map((step) => ({
      key: step.key,
      job: step.job,
      after: (step.after || []).join(", "),
      paths: (step.paths || []).join(", "),
    })),
  };
}

export function serviceOf(model: ModelChoice) {
  return model.service || model.endpoint_name || model.provider || "";
}

export function stepSummary(team: AgentTeam) {
  const steps = team.steps || [];
  if (!steps.length) return team.team || "";
  return steps
    .map((step) => {
      const waits = step.after?.length ? ` after ${step.after.join(", ")}` : "";
      return `${step.key} (${JOB_LABELS[step.job] || step.job}${waits})`;
    })
    .join("; ");
}

export function runsOn(team: AgentTeam) {
  if (!team.model_name) return "the proposal's model";
  return team.service ? `${team.model_name} on ${team.service}` : team.model_name;
}

/** Add or edit one team: its name, instruction, service and model, and the steps it owns. */
export function TeamDialog(props: {
  id: string;
  open: boolean;
  editing: boolean;
  draft: TeamDraft;
  models: ModelChoice[];
  knownSteps: string[];
  onChange: (draft: TeamDraft) => void;
  onClose: () => void;
  onSave: () => void;
}) {
  const { draft, models } = props;
  const services = Array.from(new Set(models.map(serviceOf).filter(Boolean))).sort();
  const chosen = models.find((model) => model.id === draft.model_id);
  const [service, setService] = useState(chosen ? serviceOf(chosen) : "");

  useEffect(() => {
    if (!props.open) return;
    const current = models.find((model) => model.id === draft.model_id);
    setService(current ? serviceOf(current) : "");
    // Reset the service only when the dialog opens or the team changes.
  }, [props.open, props.id]);

  if (!props.open) return null;
  const shown = service ? models.filter((model) => serviceOf(model) === service) : [];
  const own = draft.steps.map((step) => step.key).filter(Boolean);
  const waitable = Array.from(new Set([...props.knownSteps, ...own])).sort();
  const titleId = `${props.id}-title`;

  function setStep(index: number, next: DraftStep) {
    props.onChange({
      ...draft,
      steps: draft.steps.map((step, spot) => (spot === index ? next : step)),
    });
  }

  function removeStep(index: number) {
    props.onChange({ ...draft, steps: draft.steps.filter((_, spot) => spot !== index) });
  }

  function addStep() {
    props.onChange({
      ...draft,
      steps: [...draft.steps, { key: "", job: "review", after: "", paths: "" }],
    });
  }

  return createPortal(
    <div
      className="modal-backdrop open"
      id={props.id}
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      onClick={(event) => {
        if (event.target === event.currentTarget) props.onClose();
      }}
    >
      <div className="modal">
        <h2 id={titleId}>{props.editing ? `Edit ${draft.name || "team"}` : "Add a team"}</h2>
        <label htmlFor={`${props.id}-name`}>Name</label>
        <input
          id={`${props.id}-name`}
          value={draft.name}
          onChange={(event) => props.onChange({ ...draft, name: event.target.value })}
        />
        <label htmlFor={`${props.id}-instruction`}>Instruction</label>
        <textarea
          id={`${props.id}-instruction`}
          rows={4}
          value={draft.instruction}
          onChange={(event) => props.onChange({ ...draft, instruction: event.target.value })}
        />
        <label htmlFor={`${props.id}-service`}>Service</label>
        <select
          id={`${props.id}-service`}
          value={service}
          onChange={(event) => {
            const next = event.target.value;
            setService(next);
            const first = models.find((model) => serviceOf(model) === next);
            props.onChange({ ...draft, model_id: first ? first.id : "" });
          }}
        >
          <option value="">The proposal's model</option>
          {services.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
        {service ? (
          <>
            <label htmlFor={`${props.id}-model`}>Model</label>
            <select
              id={`${props.id}-model`}
              value={draft.model_id}
              onChange={(event) => props.onChange({ ...draft, model_id: event.target.value })}
            >
              {shown.map((model) => (
                <option key={model.id} value={model.id}>
                  {model.name}
                </option>
              ))}
            </select>
          </>
        ) : null}
        <div className="org-section-head">
          <h3 className="rule-heading">Steps this team runs</h3>
          <button type="button" className="secondary" data-add-team-step onClick={addStep}>
            Add a step
          </button>
        </div>
        <p className="hint">
          Steps that do not wait for each other run side by side. Build steps take turns on the
          checkout; a build step with paths writes only the plan steps for those files.
        </p>
        <datalist id={`${props.id}-step-names`}>
          {waitable.map((key) => (
            <option key={key} value={key} />
          ))}
        </datalist>
        <div className="team-steps">
          {draft.steps.map((step, index) => (
            <div className="team-step" key={index} data-team-step={index}>
              <div className="team-step-field">
                <label htmlFor={`${props.id}-step-${index}-key`}>Step</label>
                <input
                  id={`${props.id}-step-${index}-key`}
                  placeholder={draft.name || "step name"}
                  value={step.key}
                  onChange={(event) => setStep(index, { ...step, key: event.target.value })}
                />
              </div>
              <div className="team-step-field">
                <label htmlFor={`${props.id}-step-${index}-job`}>Does</label>
                <select
                  id={`${props.id}-step-${index}-job`}
                  value={step.job}
                  onChange={(event) => {
                    const job = event.target.value;
                    setStep(index, { ...step, job, paths: job === "build" ? step.paths : "" });
                  }}
                >
                  <option value="plan">Plan the change</option>
                  <option value="build">Write the change</option>
                  <option value="review">Review the change</option>
                </select>
              </div>
              <div className="team-step-field">
                <label htmlFor={`${props.id}-step-${index}-after`}>Waits for</label>
                <input
                  id={`${props.id}-step-${index}-after`}
                  list={`${props.id}-step-names`}
                  placeholder="nothing"
                  value={step.after}
                  onChange={(event) => setStep(index, { ...step, after: event.target.value })}
                />
              </div>
              {step.job === "build" ? (
                <div className="team-step-field">
                  <label htmlFor={`${props.id}-step-${index}-paths`}>Owns paths</label>
                  <input
                    id={`${props.id}-step-${index}-paths`}
                    placeholder="every file"
                    value={step.paths}
                    onChange={(event) => setStep(index, { ...step, paths: event.target.value })}
                  />
                </div>
              ) : null}
              <button
                type="button"
                className="secondary"
                aria-label={`Remove step ${step.key || index + 1}`}
                disabled={draft.steps.length < 2}
                onClick={() => removeStep(index)}
              >
                Remove
              </button>
            </div>
          ))}
        </div>
        <div className="modal-actions">
          <button type="button" className="secondary" onClick={props.onClose}>
            Close
          </button>
          <button type="button" id={`${props.id}-save`} onClick={props.onSave}>
            {props.editing ? "Save team" : "Add team"}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}

/** The order a project's steps run in: one line per phase, one pill group per wave. */
export function StepGraph(props: { graph: GraphWave[]; error?: string }) {
  if (props.error) {
    return (
      <p className="modal-error" id="agent-team-graph-error">
        {props.error}
      </p>
    );
  }
  if (!props.graph.length) return null;
  return (
    <div className="team-graph" id="agent-team-graph">
      {PHASES.map((phase) => {
        const waves = props.graph.filter((item) => item.job === phase.job);
        if (!waves.length) return null;
        return (
          <div className="team-graph-phase" key={phase.job}>
            <strong>{phase.name}</strong>
            {waves.map((wave) => (
              <span
                className="team-wave"
                key={wave.wave}
                title={
                  wave.parallel
                    ? "These steps run side by side. Steps on the same model take turns."
                    : phase.job === "build" && wave.steps.length > 1
                      ? "Build steps take turns on the checkout."
                      : undefined
                }
              >
                {wave.steps.map((key) => (
                  <span className="step-pill" key={key}>
                    {key}
                  </span>
                ))}
              </span>
            ))}
          </div>
        );
      })}
    </div>
  );
}

export function teamPayload(draft: TeamDraft) {
  return {
    name: draft.name,
    instruction: draft.instruction,
    model_id: draft.model_id,
    steps: draft.steps.map((step) => ({
      key: step.key.trim() || draft.name,
      job: step.job,
      after: splitList(step.after),
      paths: step.job === "build" ? splitList(step.paths) : [],
    })),
  };
}

export function stepKeys(teams: AgentTeam[], except?: string) {
  return teams
    .filter((team) => team.id !== except)
    .flatMap((team) => (team.steps || []).map((step) => step.key));
}

function splitList(value: string) {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}
