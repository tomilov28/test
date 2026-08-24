import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RequestCreate(BaseModel):
    request_type: str = Field(min_length=1, max_length=128)
    request_type_version: int = Field(default=1, ge=1)
    workflow_engine: str = Field(default="OPERATON", pattern="^(OPERATON|FLOWABLE)$")
    variables: dict[str, Any] = Field(default_factory=dict)


class TaskResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    work_item_id: uuid.UUID
    version: int
    data: dict[str, Any]
    created_at: datetime


class WorkItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    request_id: uuid.UUID
    task_definition_key: str
    external_task_id: str
    state: str
    created_at: datetime
    completed_at: datetime | None
    results: list[TaskResultOut] = Field(default_factory=list)
    completed_at: datetime | None
    results: list[TaskResultOut] = Field(default_factory=list)


class TaskResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    work_item_id: uuid.UUID
    version: int
    data: dict[str, Any]
    created_at: datetime


class RequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    number: str
    request_type: str
    request_type_version: int
    lifecycle_state: str
    outcome: str | None
    workflow_engine: str
    workflow_instance_id: str | None
    created_at: datetime
    closed_at: datetime | None


class RequestDetail(RequestOut):
    work_items: list[WorkItemOut] = Field(default_factory=list)


class CommandOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    request_id: uuid.UUID
    command_type: str
    state: str
    attempts: int
    last_error: str | None
    created_at: datetime
    processed_at: datetime | None


class CompleteWorkItemIn(BaseModel):
    data: dict[str, Any] = Field(default_factory=dict)
    version: int = Field(default=1, ge=1)


class CancelRequestIn(BaseModel):
    reason: str | None = None


class ReconcileResult(BaseModel):
    requests_seen: int
    tasks_seen: int
    work_items_upserted: int
    completed_requests: int
    errors: list[dict[str, str]]


class HealthOut(BaseModel):
    status: str
    database: str
    engines: dict[str, str]
