import { useEffect, useState } from "react";
import { createPortal } from "react-dom";

import { ApiError, api } from "../../api";
import type { Endpoint, OrgCatalog, OrgModel } from "./types";

const TRASH = (
  <svg viewBox="0 0 24 24" aria-hidden="true">
    <path
      d="M9 3h6l1 2h4v2H4V5h4l1-2zm1 6h2v8h-2V9zm4 0h2v8h-2V9zM7 9h2v8H7V9z"
      fill="currentColor"
    />
  </svg>
);

type DialogName = "endpoint" | "model" | "remove-endpoint" | "own-key" | "";

export function ModelsSection() {
  const [catalog, setCatalog] = useState<OrgCatalog | null>(null);
  const [notice, setNotice] = useState("");
  const [dialog, setDialog] = useState<DialogName>("");
  const [removeId, setRemoveId] = useState("");
  const [endpointName, setEndpointName] = useState("");
  const [endpointProvider, setEndpointProvider] = useState("");
  const [endpointUrl, setEndpointUrl] = useState("");
  const [endpointKey, setEndpointKey] = useState("");
  const [endpointAuth, setEndpointAuth] = useState("key");
  const [endpointAudience, setEndpointAudience] = useState("");
  const [ownKeyId, setOwnKeyId] = useState("");
  const [ownKey, setOwnKey] = useState("");
  const [modelEndpoint, setModelEndpoint] = useState("");
  const [modelName, setModelName] = useState("");
  const [modelShown, setModelShown] = useState("");

  useEffect(() => {
    function onCatalog(event: Event) {
      const detail = (event as CustomEvent<OrgCatalog>).detail || {};
      setCatalog(detail);
      setEndpointProvider((current) => current || detail.providers?.[0]?.id || "");
    }
    const cached = (window as OrgSettingsWindow).__lbwOrgSettings;
    if (cached) onCatalog(new CustomEvent("lbw-org-settings", { detail: cached }));
    document.addEventListener("lbw-org-settings", onCatalog);
    return () => document.removeEventListener("lbw-org-settings", onCatalog);
  }, []);

  const providers = catalog?.providers || [];
  const endpoints = catalog?.endpoints || [];
  const models = catalog?.models || [];
  const manage = Boolean(catalog?.can_manage);
  const chosenEndpoint = endpoints.find((item) => item.id === modelEndpoint);
  const modelNames = chosenEndpoint?.model_names || [];
  const removeTarget = endpoints.find((item) => item.id === removeId);
  const chosenProvider = providers.find((item) => item.id === endpointProvider);
  const authModes = endpointAuthModes(chosenProvider);

  function fail(err: unknown) {
    const message = err instanceof ApiError || err instanceof Error ? err.message : "request failed";
    setNotice(message);
  }

  function apply(body: OrgCatalog) {
    const refresh = (window as RefreshWindow).renderOrgSettings;
    if (refresh) refresh(body);
    else setCatalog(body);
    if (body.endpoint_warning) setNotice(body.endpoint_warning);
    else setNotice("");
  }

  function revert() {
    setCatalog((current) => (current ? { ...current } : current));
  }

  function defaultUrl(providerId: string, current: string): string {
    const defaults = providers.map((item) => item.base_url || "");
    const selected = providers.find((item) => item.id === providerId);
    if (!current || defaults.includes(current)) return selected?.base_url || "";
    return current;
  }

  function openEndpoint() {
    const first = providers[0];
    const providerId = endpointProvider || first?.id || "";
    setEndpointProvider(providerId);
    setEndpointUrl((current) => defaultUrl(providerId, current));
    setDialog("endpoint");
  }

  function openModel() {
    const first = endpoints[0];
    const endpointId = modelEndpoint || first?.id || "";
    setModelEndpoint(endpointId);
    const names = endpoints.find((item) => item.id === endpointId)?.model_names || [];
    setModelName(names[0] || "");
    setDialog("model");
  }

  async function addEndpoint() {
    const known = new Set(endpoints.map((item) => item.id));
    const body = await api<OrgCatalog>("/api/org/endpoints", {
      method: "POST",
      body: JSON.stringify({
        name: endpointName,
        provider: endpointProvider,
        base_url: endpointUrl,
        api_key: endpointAuth === "sso" ? "" : endpointKey,
        auth: endpointAuth,
        audience:
          endpointAuth === "key" || !providers.find((item) => item.id === endpointProvider)?.gateway
            ? ""
            : endpointAudience,
      }),
    });
    const saved = (body.endpoints || []).find((item) => item.auth === "sso" && !known.has(item.id));
    setEndpointName("");
    setEndpointKey("");
    setEndpointAuth("key");
    setEndpointAudience("");
    setDialog("");
    apply(body);
    if (saved) {
      window.location.assign(`/api/org/endpoints/${encodeURIComponent(saved.id)}/sso`);
    }
  }

  async function saveOwnKey() {
    const endpointId = ownKeyId;
    if (!endpointId) return;
    const body = await api<OrgCatalog>(
      `/api/org/endpoints/${encodeURIComponent(endpointId)}/credential`,
      {
        method: "POST",
        body: JSON.stringify({ api_key: ownKey }),
      },
    );
    setOwnKey("");
    setOwnKeyId("");
    setDialog("");
    apply(body);
  }

  async function syncEndpoint(endpointId: string) {
    const body = await api<OrgCatalog>(
      `/api/org/endpoints/${encodeURIComponent(endpointId)}/sync`,
      { method: "POST" },
    );
    apply(body);
  }

  async function removeEndpoint() {
    const endpointId = removeId;
    setRemoveId("");
    setDialog("");
    if (!endpointId) return;
    const body = await api<OrgCatalog>(
      `/api/org/endpoints/${encodeURIComponent(endpointId)}`,
      { method: "DELETE" },
    );
    apply(body);
  }

  async function addModel() {
    const body = await api<OrgCatalog>("/api/org/models", {
      method: "POST",
      body: JSON.stringify({
        name: modelShown,
        endpoint_id: modelEndpoint,
        model_name: modelName,
      }),
    });
    setModelShown("");
    setDialog("");
    apply(body);
  }

  async function setEnabled(kind: "models", id: string, enabled: boolean) {
    const body = await api<OrgCatalog>(`/api/org/${kind}/${encodeURIComponent(id)}/enabled`, {
      method: "PUT",
      body: JSON.stringify({ enabled }),
    });
    apply(body);
  }

  async function removeRow(kind: "models", id: string) {
    const body = await api<OrgCatalog>(`/api/org/${kind}/${encodeURIComponent(id)}`, {
      method: "DELETE",
    });
    apply(body);
  }

  return (
    <>
      {notice ? <div className="notice error">{notice}</div> : null}
      <div className="org-section-head">
        <h2 className="rule-heading">Endpoints</h2>
        <button
          type="button"
          className="secondary icon-button"
          id="open-org-endpoint"
          aria-label="Add an endpoint"
          hidden={!manage}
          onClick={openEndpoint}
        >
          +
        </button>
      </div>
      <p className="hint" id="org-models-hint">
        Bring your own endpoint. OpenAI, Claude, and Google take an API key. Claude runs through
        the Messages API. A self-hosted server such as Ollama, vLLM, or LM Studio uses its own
        address. LiteLLM and other gateways speak the OpenAI API. An organization key
        is shared. Each person can check their own key, or sign in when the gateway accepts this
        organization's sign-in token. A run uses that person's credential. Resync stores the names
        it returns. The key is sealed and is not shown again.
      </p>
      <div id="org-endpoints">
        <OrgTable
          headers={["Name", "Endpoint", "Models", ""]}
          empty="No endpoints yet."
          rows={endpoints.map((endpoint) => ({
            key: endpoint.id,
            cells: endpointCells(
              endpoint,
              manage,
              Boolean(providers.find((item) => item.id === endpoint.provider)?.gateway),
              (id) => {
                syncEndpoint(id).catch(fail);
              },
              (id) => {
                setRemoveId(id);
                setDialog("remove-endpoint");
              },
              (id) => {
                setOwnKeyId(id);
                setOwnKey("");
                setDialog("own-key");
              },
            ),
          }))}
        />
      </div>
      <div className="org-section-head">
        <h2 className="rule-heading">Models</h2>
        <button
          type="button"
          className="secondary icon-button"
          id="open-org-model"
          aria-label="Add a model"
          hidden={!manage}
          onClick={openModel}
        >
          +
        </button>
      </div>
      <p className="hint">
        The switch makes a model available on every project. A repository admin turns one off for
        that project. That choice stays if this switch is turned off and back on.
      </p>
      <div id="org-models">
        <OrgTable
          headers={["Endpoint", "Model", "On", ""]}
          empty="No models yet."
          rows={models.map((model) => ({
            key: model.id,
            cells: modelCells(model, manage, (id, enabled) => {
              setEnabled("models", id, enabled).catch((err: unknown) => {
                revert();
                fail(err);
              });
            }, (id) => {
              removeRow("models", id).catch(fail);
            }),
          }))}
        />
      </div>
      <Dialog
        id="endpoint-remove-dialog"
        titleId="endpoint-remove-title"
        title="Remove this endpoint?"
        open={dialog === "remove-endpoint"}
        onClose={() => setDialog("")}
      >
        <p className="hint" id="endpoint-remove-copy">
          {`Remove ${removeTarget?.name || "this endpoint"}? This takes it off the organization. A model that still uses it has to be removed first.`}
        </p>
        <div className="modal-actions">
          <button type="button" className="secondary" id="cancel-endpoint-remove" onClick={() => setDialog("")}>
            Cancel
          </button>
          <button
            type="button"
            id="confirm-endpoint-remove"
            onClick={() => {
              removeEndpoint().catch(fail);
            }}
          >
            Remove endpoint
          </button>
        </div>
      </Dialog>
      <Dialog
        id="org-endpoint-dialog"
        titleId="org-endpoint-title"
        title="Add an endpoint"
        open={dialog === "endpoint"}
        onClose={() => setDialog("")}
      >
        <form
          className="org-form"
          id="org-endpoint-form"
          onSubmit={(event) => {
            event.preventDefault();
            addEndpoint().catch(fail);
          }}
        >
          <label>
            Name
            <input
              id="org-endpoint-name"
              type="text"
              maxLength={80}
              placeholder="Desk"
              autoComplete="off"
              value={endpointName}
              onChange={(event) => setEndpointName(event.target.value)}
            />
          </label>
          <label>
            Provider
            <select
              id="org-endpoint-provider"
              aria-label="Model provider"
              value={endpointProvider}
              onChange={(event) => {
                const next = event.target.value;
                const provider = providers.find((item) => item.id === next);
                setEndpointUrl((current) => defaultUrl(next, current));
                if (!endpointAuthModes(provider).includes(endpointAuth)) setEndpointAuth("key");
                setEndpointProvider(next);
              }}
            >
              {providers.map((provider) => (
                <option key={provider.id} value={provider.id}>
                  {provider.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Base URL
            <input
              id="org-endpoint-url"
              type="url"
              maxLength={200}
              placeholder="Base URL"
              autoComplete="off"
              value={endpointUrl}
              onChange={(event) => setEndpointUrl(event.target.value)}
            />
          </label>
          {authModes.length > 1 ? (
            <label>
              Sign-in
              <select
                id="org-endpoint-auth"
                aria-label="Endpoint sign-in"
                value={endpointAuth}
                onChange={(event) => setEndpointAuth(event.target.value)}
              >
                {authModes.map((mode) => (
                  <option key={mode} value={mode}>
                    {mode === "personal" ? "Each person" : mode === "sso" ? "SSO" : "Organization key"}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
          {chosenProvider?.gateway && endpointAuth !== "key" ? (
            <label>
              Audience
              <input
                id="org-endpoint-audience"
                type="text"
                maxLength={200}
                placeholder="Gateway audience, if the provider requires one"
                autoComplete="off"
                value={endpointAudience}
                onChange={(event) => setEndpointAudience(event.target.value)}
              />
            </label>
          ) : null}
          {endpointAuth !== "sso" ? (
            <label>
              {endpointAuth === "personal" ? "Your API key" : "API key"}
              <input
                id="org-endpoint-key"
                type="password"
                placeholder={endpointAuth === "personal" ? "Checked for you. Optional." : "API key"}
                autoComplete="off"
                value={endpointKey}
                onChange={(event) => setEndpointKey(event.target.value)}
              />
            </label>
          ) : null}
          <div className="modal-actions">
            <button type="button" className="secondary" id="cancel-org-endpoint" onClick={() => setDialog("")}>
              Cancel
            </button>
            <button type="button" id="add-org-endpoint" onClick={() => addEndpoint().catch(fail)}>
              {endpointAuth === "sso"
                ? "Continue"
                : endpointAuth === "personal" && !endpointKey.trim()
                  ? "Add"
                  : "Check"}
            </button>
          </div>
        </form>
      </Dialog>
      <Dialog
        id="org-own-key-dialog"
        titleId="org-own-key-title"
        title="Your API key"
        open={dialog === "own-key"}
        onClose={() => setDialog("")}
      >
        <form
          className="org-form"
          id="org-own-key-form"
          onSubmit={(event) => {
            event.preventDefault();
            saveOwnKey().catch(fail);
          }}
        >
          <p className="hint">
            This key is checked against the endpoint and sealed for you. A run you start uses it.
          </p>
          <label>
            API key
            <input
              id="org-own-key"
              type="password"
              placeholder="API key"
              autoComplete="off"
              value={ownKey}
              onChange={(event) => setOwnKey(event.target.value)}
            />
          </label>
          <div className="modal-actions">
            <button type="button" className="secondary" id="cancel-org-own-key" onClick={() => setDialog("")}>
              Cancel
            </button>
            <button type="button" id="check-org-own-key" onClick={() => saveOwnKey().catch(fail)}>
              Check
            </button>
          </div>
        </form>
      </Dialog>
      <Dialog
        id="org-model-dialog"
        titleId="org-model-title"
        title="Add a model"
        open={dialog === "model"}
        onClose={() => setDialog("")}
      >
        <form
          className="org-form"
          id="org-model-form"
          onSubmit={(event) => {
            event.preventDefault();
            addModel().catch(fail);
          }}
        >
          <label>
            Endpoint
            <select
              id="org-model-endpoint"
              aria-label="Endpoint"
              value={modelEndpoint}
              onChange={(event) => {
                const next = event.target.value;
                setModelEndpoint(next);
                const names = endpoints.find((item) => item.id === next)?.model_names || [];
                setModelName(names[0] || "");
              }}
            >
              <option value="">Choose an endpoint</option>
              {endpoints.map((endpoint) => (
                <option key={endpoint.id} value={endpoint.id}>
                  {endpoint.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Model
            <select
              id="org-model-model"
              aria-label="Model name"
              value={modelName}
              onChange={(event) => setModelName(event.target.value)}
            >
              {modelNames.length === 0 ? <option value="">No models yet</option> : null}
              {modelNames.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Shown name
            <input
              id="org-model-name"
              type="text"
              maxLength={80}
              placeholder="Shown name"
              autoComplete="off"
              value={modelShown}
              onChange={(event) => setModelShown(event.target.value)}
            />
          </label>
          <div className="modal-actions">
            <button type="button" className="secondary" id="cancel-org-model" onClick={() => setDialog("")}>
              Cancel
            </button>
            <button type="button" id="add-org-model" onClick={() => addModel().catch(fail)}>
              Add
            </button>
          </div>
        </form>
      </Dialog>
    </>
  );
}

function endpointAuthModes(provider: { key: boolean; gateway?: boolean } | undefined): string[] {
  if (!provider) return [];
  if (provider.gateway) return ["key", "personal", "sso"];
  if (provider.key) return ["key", "personal"];
  return [];
}

function endpointCells(
  endpoint: Endpoint,
  manage: boolean,
  gateway: boolean,
  onSync: (id: string) => void,
  onRemove: (id: string) => void,
  onOwnKey: (id: string) => void,
) {
  const ownKey = endpoint.auth === "personal" ? (
    <button
      type="button"
      className="secondary"
      data-endpoint-key={endpoint.id}
      onClick={() => onOwnKey(endpoint.id)}
    >
      {endpoint.signed_in ? "Replace key" : "Your key"}
    </button>
  ) : null;
  const signIn =
    !endpoint.signed_in && (endpoint.auth === "sso" || (endpoint.auth === "personal" && gateway)) ? (
      <a
        className="secondary"
        href={`/api/org/endpoints/${encodeURIComponent(endpoint.id)}/sso`}
        data-endpoint-sso={endpoint.id}
      >
        Sign in
      </a>
    ) : null;
  const manageActions = manage ? (
    <>
      <button
        type="button"
        className="secondary"
        data-endpoint-sync={endpoint.id}
        onClick={() => onSync(endpoint.id)}
      >
        Resync
      </button>
      <button
        type="button"
        className="secondary icon-button"
        data-endpoint-remove={endpoint.id}
        aria-label={`Remove ${endpoint.name}`}
        onClick={() => onRemove(endpoint.id)}
      >
        {TRASH}
      </button>
    </>
  ) : null;
  const actions = ownKey || signIn || manageActions ? (
    <div className="org-row-actions">
      {ownKey}
      {signIn}
      {manageActions}
    </div>
  ) : null;
  return [
    <td key="name">{endpoint.name}</td>,
    <td key="url" className="url">{endpoint.base_url || "—"}</td>,
    <td key="count">{String((endpoint.model_names || []).length)}</td>,
    <td key="actions" className="org-table-actions">{actions}</td>,
  ];
}

function modelCells(
  model: OrgModel,
  manage: boolean,
  onEnabled: (id: string, enabled: boolean) => void,
  onRemove: (id: string) => void,
) {
  return [
    <td key="endpoint">{model.endpoint_name || model.provider}</td>,
    <td key="name">{model.model_name}</td>,
    <td key="on">
      <input
        type="checkbox"
        className="org-switch"
        checked={Boolean(model.enabled)}
        disabled={!manage}
        data-model-enable={model.id}
        aria-label={`Use ${model.model_name}`}
        onChange={(event) => onEnabled(model.id, event.target.checked)}
      />
    </td>,
    <td key="actions" className="org-table-actions">
      {manage ? (
        <div className="org-row-actions">
          <button
            type="button"
            className="secondary icon-button"
            data-model-remove={model.id}
            aria-label={`Remove ${model.model_name}`}
            onClick={() => onRemove(model.id)}
          >
            {TRASH}
          </button>
        </div>
      ) : null}
    </td>,
  ];
}

function OrgTable(props: {
  headers: string[];
  empty: string;
  rows: { key: string; cells: React.ReactNode[] }[];
}) {
  return (
    <table className="org-table">
      <thead>
        <tr>
          {props.headers.map((label, index) => (
            <th key={`${label}-${index}`} className={label ? undefined : "org-table-actions"}>
              {label}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {props.rows.length === 0 ? (
          <tr>
            <td colSpan={props.headers.length} className="muted">
              {props.empty}
            </td>
          </tr>
        ) : (
          props.rows.map((row) => <tr key={row.key}>{row.cells}</tr>)
        )}
      </tbody>
    </table>
  );
}

function Dialog(props: {
  id: string;
  titleId: string;
  title: string;
  open: boolean;
  onClose: () => void;
  children: React.ReactNode;
}) {
  if (!props.open) return null;
  return createPortal(
    <div
      className="modal-backdrop open"
      id={props.id}
      role="dialog"
      aria-modal="true"
      aria-labelledby={props.titleId}
      onClick={(event) => {
        if (event.target === event.currentTarget) props.onClose();
      }}
    >
      <div className="modal">
        <h2 id={props.titleId}>{props.title}</h2>
        {props.children}
      </div>
    </div>,
    document.body,
  );
}

type RefreshWindow = Window & {
  renderOrgSettings?: (body: OrgCatalog) => void;
};

type OrgSettingsWindow = Window & {
  __lbwOrgSettings?: OrgCatalog;
};
