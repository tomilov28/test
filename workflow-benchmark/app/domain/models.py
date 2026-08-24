import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.domain.enums import (
    CommandState,
    CommandType,
    LifecycleState,
    RequestOutcome,
    WorkflowEngine,
    WorkItemState,
)


class Request(Base):
    __tablename__ = "requests"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    number: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    request_type: Mapped[str] = mapped_column(String(128), nullable=False)
    request_type_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    lifecycle_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default=LifecycleState.ACTIVE.value
    )
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)

    workflow_engine: Mapped[str] = mapped_column(
        String(32), nullable=False, default=WorkflowEngine.OPERATON.value
    )
    workflow_instance_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    work_items: Mapped[list["WorkItem"]] = relationship(
        back_populates="request", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_requests_workflow_instance_id", "workflow_instance_id"),)


class WorkItem(Base):
    __tablename__ = "work_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("requests.id", ondelete="CASCADE"), nullable=False
    )
    task_definition_key: Mapped[str] = mapped_column(String(128), nullable=False)
    external_task_id: Mapped[str] = mapped_column(String(128), nullable=False)

    state: Mapped[str] = mapped_column(String(32), nullable=False, default=WorkItemState.ACTIVE.value)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    request: Mapped[Request] = relationship(back_populates="work_items")
    results: Mapped[list["TaskResult"]] = relationship(back_populates="work_item")

    __table_args__ = (
        UniqueConstraint(
            "request_id", "task_definition_key", "external_task_id", name="uq_work_item_engine_task"
        ),
        Index("ix_work_items_request_id", "request_id"),
    )


class TaskResult(Base):
    __tablename__ = "task_results"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    work_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("work_items.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    work_item: Mapped[WorkItem] = relationship(back_populates="results")

    __table_args__ = (Index("ix_task_results_work_item_id", "work_item_id"),)


class WorkflowCommand(Base):
    __tablename__ = "workflow_commands"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("requests.id", ondelete="CASCADE"), nullable=False
    )

    command_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    state: Mapped[str] = mapped_column(String(32), nullable=False, default=CommandState.PENDING.value)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_workflow_commands_state", "state"),
        Index("ix_workflow_commands_request_id", "request_id"),
    )
