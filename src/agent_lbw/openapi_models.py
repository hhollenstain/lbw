from __future__ import annotations

from pydantic import BaseModel, Field

# Generated from openapi/lbw.yaml. Run `make openapi` after a spec change.


class ScheduleCadence(BaseModel):
    kind: str
    minute: int = 0
    hour: int = 9
    weekdays: list[int] = Field(default_factory=list)


class ScheduleWrite(BaseModel):
    name: str
    goal: str
    cadence: ScheduleCadence
    workspace: str = ""
    kind: str = "project"
    enabled: bool = True
    auto_commit: bool = True
    create_pr: bool = True
    base_branch: str = ""
    model_id: str = ""


class AuditEntry(BaseModel):
    id: str
    at: str
    action: str
    summary: str
    actor_id: str = ""
    actor_name: str = ""
    method: str = ""
    path: str = ""
    target_type: str = ""
    target_id: str = ""
    detail: str = ""


class AuditList(BaseModel):
    entries: list[AuditEntry] = Field(default_factory=list)


class RepoRuleFile(BaseModel):
    path: str
    enabled: bool
    body: str
    html: str


class RepoRuleList(BaseModel):
    branch: str = ""
    files: list[RepoRuleFile] = Field(default_factory=list)


class RepoRuleWrite(BaseModel):
    path: str
    enabled: bool
    workspace: str = ""


class EndpointCredential(BaseModel):
    api_key: str


class EndpointWrite(BaseModel):
    name: str
    provider: str
    base_url: str = ""
    api_key: str = ""
    auth: str = "key"
    audience: str = ""


class ProjectModel(BaseModel):
    id: str
    name: str
    provider: str
    model_name: str
    org_enabled: bool
    enabled: bool
    endpoint_name: str = ""
    endpoint_id: str = ""
    auth: str = "key"
    signed_in: bool = True


class ProjectModelList(BaseModel):
    can_manage: bool
    models: list[ProjectModel] = Field(default_factory=list)


class AgentTeamStep(BaseModel):
    key: str
    job: str
    after: list[str] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)


class AgentTeamWrite(BaseModel):
    name: str
    job: str = ""
    instruction: str = ""
    model_id: str = ""
    steps: list[AgentTeamStep] = Field(default_factory=list)


class AgentTeamUpdate(BaseModel):
    name: str
    instruction: str
    model_id: str
    steps: list[AgentTeamStep] = Field(default_factory=list)


class PriceWrite(BaseModel):
    model: str
    input_per_mtok: float
    output_per_mtok: float
    provider: str = ""
    cache_read_per_mtok: float = 0
    cache_write_per_mtok: float = 0


class AgentTeamImport(BaseModel):
    path: str = ""


class AgentTeamProjectWrite(BaseModel):
    team_id: str
    enabled: bool
    path: str = ""


class ProjectModelWrite(BaseModel):
    model_id: str
    enabled: bool
    path: str = ""


class RepoEnabled(BaseModel):
    project_id: str
    enabled: bool


class TaskActor(BaseModel):
    id: str
    name: str


class TaskGate(BaseModel):
    action: str
    at: str
    actor: TaskActor
    note: str = ""


class RunnerLinkStart(BaseModel):
    name: str
    platform: str = ""


class RunnerLinkToken(BaseModel):
    device_code: str


class RunnerApprove(BaseModel):
    user_code: str


class RunnerHeartbeat(BaseModel):
    version: str = ""
    claude_version: str = ""
    claude_ready: bool = False
    platform: str = ""
    llm_relay: bool = False


class RunnerEvents(BaseModel):
    events: list[object] = Field(default_factory=list)


class SpecPolicy(BaseModel):
    required: bool


class ProjectSpecPolicy(BaseModel):
    path: str
    required: bool


class DecidePayload(BaseModel):
    decision: str = ""
    note: str = ""
    spec_text: str = ""


class RerunPayload(BaseModel):
    sha: str = ""
    goal: str = ""
    model_id: str = ""


class RunnerModelAnswer(BaseModel):
    status: int
    body: str = ""
    error: str = ""


class LocalModelPolicy(BaseModel):
    policy: str


class LocalModelPerson(BaseModel):
    allowed: bool


class LocalModelServer(BaseModel):
    enabled: bool
    provider: str
    base_url: str = ""


class RunnerFinish(BaseModel):
    status: str
    summary: str = ""
    error: str = ""
    patch: str = ""
