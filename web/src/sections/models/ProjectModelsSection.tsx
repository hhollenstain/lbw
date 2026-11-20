import { useEffect, useState } from "react";
import { createPortal } from "react-dom";

import { api } from "../../api";
import { notify, workspacePath } from "../../page";

type ProjectModel = {
  id: string;
  name: string;
  model_name: string;
  endpoint_name?: string;
  endpoint_id?: string;
  provider?: string;
  auth?: string;
  signed_in?: boolean;
  org_enabled?: boolean;
  enabled?: boolean;
};

type ProjectModels = {
  can_manage?: boolean;
  models?: ProjectModel[];
};

export function ProjectModelsSection() {
  const [body, setBody] = useState<ProjectModels | null>(null);
  const [keyFor, setKeyFor] = useState<ProjectModel | null>(null);
  const [ownKey, setOwnKey] = useState("");

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

  async function checkKey() {
    const model = keyFor;
    if (!model?.endpoint_id) return;
    try {
      await api(`/api/org/endpoints/${encodeURIComponent(model.endpoint_id)}/credential`, {
        method: "POST",
        body: JSON.stringify({ api_key: ownKey }),
      });
      const next = await api<ProjectModels>(
        "/api/workspaces/models?path=" + encodeURIComponent(workspacePath()),
      );
      setKeyFor(null);
      setOwnKey("");
      publishModels(next);
    } catch (err) {
      notify(
        "error",
        err instanceof Error ? err.message : "that key was refused",
        "Could not check the key",
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
              {model.auth === "personal" && model.endpoint_id ? (
                <button
                  type="button"
                  className="secondary"
                  data-project-model-key={model.id}
                  onClick={() => {
                    setKeyFor(model);
                    setOwnKey("");
                  }}
                >
                  {model.signed_in ? "Replace key" : "Your key"}
                </button>
              ) : null}
              {!model.signed_in &&
              model.endpoint_id &&
              (model.auth === "sso" ||
                (model.auth === "personal" &&
                  (model.provider === "litellm" || model.provider === "gateway"))) ? (
                <a
                  className="secondary"
                  href={`/api/org/endpoints/${encodeURIComponent(model.endpoint_id)}/sso`}
                  data-project-model-sso={model.id}
                >
                  Sign in
                </a>
              ) : null}
            </div>
          </div>
        );
      })}
      {keyFor
        ? createPortal(
            <div
              className="modal-backdrop open"
              id="project-model-key-dialog"
              role="dialog"
              aria-modal="true"
              aria-labelledby="project-model-key-title"
              onClick={(event) => {
                if (event.target === event.currentTarget) setKeyFor(null);
              }}
            >
              <div className="modal">
                <h2 id="project-model-key-title">Your API key</h2>
                <form
                  className="org-form"
                  onSubmit={(event) => {
                    event.preventDefault();
                    void checkKey();
                  }}
                >
                  <p className="hint">
                    This key is checked against the endpoint and sealed for you. A run you start
                    uses it.
                  </p>
                  <label>
                    API key
                    <input
                      id="project-model-key"
                      type="password"
                      placeholder="API key"
                      autoComplete="off"
                      value={ownKey}
                      onChange={(event) => setOwnKey(event.target.value)}
                    />
                  </label>
                  <div className="modal-actions">
                    <button
                      type="button"
                      className="secondary"
                      id="cancel-project-model-key"
                      onClick={() => setKeyFor(null)}
                    >
                      Cancel
                    </button>
                    <button type="button" id="check-project-model-key" onClick={() => void checkKey()}>
                      Check
                    </button>
                  </div>
                </form>
              </div>
            </div>,
            document.body,
          )
        : null}
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
