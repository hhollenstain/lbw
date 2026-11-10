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


class ProjectModel(BaseModel):
    id: str
    name: str
    provider: str
    model_name: str
    org_enabled: bool
    enabled: bool
    endpoint_name: str = ""


class ProjectModelList(BaseModel):
    can_manage: bool
    models: list[ProjectModel] = Field(default_factory=list)


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
