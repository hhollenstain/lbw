import { useEffect, useState } from "react";

import { api } from "../../api";
import { notify, workspacePath } from "../../page";

type ProjectModel = {
  id: string;
  name: string;
  model_name: string;
  endpoint_name?: string;
  org_enabled?: boolean;
  enabled?: boolean;
};

type ProjectModels = {
  can_manage?: boolean;
  models?: ProjectModel[];
};

export function ProjectModelsSection() {
  const [body, setBody] = useState<ProjectModels | null>(null);

  useEffect(() => {
    function onModels(event: Event) {
      setBody((event as CustomEvent<ProjectModels>).detail || {});
    }
    const cached = (window as ProjectModelsWindow).__lbwProjectModels;
    if (cached) onModels(new CustomEvent("lbw-project-models", { detail: cached }));
    document.addEventListener("lbw-project-models", onModels);
    return () => document.removeEventListener("lbw-project-models", onModels);
  }, []);

  async function save(model: ProjectModel, enabled: boolean) {
    try {
      const next = await api<ProjectModels>("/api/workspaces/models", {
        method: "PUT",
        body: JSON.stringify({ path: workspacePath(), model_id: model.id, enabled }),
      });
      publishModels(next);
    } catch (err) {
      notify(
        "error",
        err instanceof Error ? err.message : "failed to update the model",
        "Could not update the model",
      );
    }
  }

  const models = body?.models || [];
  const manage = Boolean(body?.can_manage);
  if (body && models.length === 0) {
    return <p className="hint">No models yet. Turn one on in Organization.</p>;
  }
  return (
    <>
      {models.map((model) => {
        const note = model.org_enabled
          ? model.endpoint_name || ""
          : "Off for the organization. This choice stays when it is turned back on.";
        return (
          <div className="skill-row" key={model.id}>
            <input
              type="checkbox"
              className="org-switch"
              checked={Boolean(model.enabled)}
              disabled={!manage}
              data-project-model={model.id}
              aria-label={`Use ${model.model_name} on this project`}
              onChange={(event) => {
                void save(model, event.target.checked);
              }}
            />
            <div className="skill-row-copy">
              <strong>{model.name || model.model_name}</strong>
              {note ? <p>{note}</p> : null}
            </div>
          </div>
        );
      })}
    </>
  );
}

function publishModels(body: ProjectModels) {
  const page = window as ProjectModelsWindow;
  page.__lbwProjectModels = body;
  document.dispatchEvent(new CustomEvent("lbw-project-models", { detail: body }));
}

type ProjectModelsWindow = Window & {
  __lbwProjectModels?: ProjectModels;
};
