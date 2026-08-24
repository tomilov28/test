"""Transactional outbox dispatcher.

Polls PENDING WorkflowCommand rows and hands them to the appropriate engine
adapter. The command is written in the SAME database transaction as the domain
change it represents; the dispatcher only ever talks to the engine via the
public REST API.

At-least-once semantics: a command is claimed with an atomic PENDING ->
PROCESSING transition, then dispatched. If the process crashes between claim
and DONE the command stays PROCESSING forever; reconcile() below re-arms stale
PROCESSING commands so they survive restarts.
"""

import logging
import threading
import time

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.domain.enums import CommandState, CommandType, LifecycleState, RequestOutcome, WorkItemState
from app.domain.models import Request, WorkItem, WorkflowCommand

logger = logging.getLogger(__name__)

STALE_PROCESSING_AFTER_SECONDS = 60.0


def claim_pending_commands(db: Session, limit: int = 10, max_attempts: int = 5) -> list[WorkflowCommand]:
    """Atomically claim PENDING commands for dispatch."""
    rows = (
        db.execute(
            select(WorkflowCommand)
            .where(
                WorkflowCommand.state == CommandState.PENDING.value,
                WorkflowCommand.attempts < max_attempts,
            )
            .order_by(WorkflowCommand.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    for cmd in rows:
        cmd.state = CommandState.PROCESSING.value
        cmd.attempts += 1
    db.commit()
    return rows


def rearm_stale_processing(db: Session, stale_after_seconds: float = STALE_PROCESSING_AFTER_SECONDS) -> int:
    """Return crashed PROCESSING commands to PENDING so they are retried."""
    threshold = _now_minus(stale_after_seconds, db)
    res = db.execute(
        update(WorkflowCommand)
        .where(
            WorkflowCommand.state == CommandState.PROCESSING.value,
            WorkflowCommand.processed_at.is_(None),
            WorkflowCommand.created_at < threshold,
        )
        .values(state=CommandState.PENDING.value)
    )
    db.commit()
    return res.rowcount or 0


def _now_minus(seconds: float, db: Session):
    from datetime import datetime, timedelta, timezone

    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


def dispatch_one(db: Session, command: WorkflowCommand, adapter_factory, max_attempts: int = 5) -> None:
    """Dispatch a single claimed command to the engine. Raises on failure."""
    # Row-lock the request: serializes dispatcher vs reconciler writers so a
    # concurrent auto-close (or cancellation) cannot be silently overwritten.
    # populate_existing forces a refresh from the locked row even if the entity
    # is already in the session identity map (avoids stale-guard reads).
    request = db.execute(
        select(Request)
        .where(Request.id == command.request_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalars().first()
    if request is None:
        command.state = CommandState.FAILED.value
        command.last_error = "request not found"
        command.processed_at = _now_minus(0, db)
        db.commit()
        return

    adapter = adapter_factory(request.workflow_engine)
    try:
        if command.command_type == CommandType.START_PROCESS.value:
            # Idempotent start: the engine has no unique business-key
            # constraint, so under at-least-once dispatch a retried START after
            # a lost response must reuse the instance the first attempt created
            # instead of starting a second one. Query-before-start covers both
            # still-running and already-finished instances.
            existing = adapter.find_process_instance_by_business_key(
                request.request_type, request.number
            )
            if existing:
                result = existing[0]
            else:
                result = adapter.start_process(
                    process_key=request.request_type,
                    business_key=request.number,
                    variables=command.payload.get("variables") or {},
                    version=request.request_type_version,
                )
            request.workflow_instance_id = result.process_instance_id

        elif command.command_type == CommandType.COMPLETE_TASK.value:
            # Idempotent completion: if the engine task no longer exists the
            # requested state (task completed) is already achieved, so a retried
            # COMPLETE after a lost response is a success, not a failure.
            task = adapter.get_human_task(external_task_id=command.payload["external_task_id"])
            if task is not None:
                adapter.complete_human_task(
                    external_task_id=command.payload["external_task_id"],
                    variables=command.payload.get("data") or {},
                )

        elif command.command_type == CommandType.CANCEL_PROCESS.value:
            instance_id = command.payload.get("workflow_instance_id") or request.workflow_instance_id
            if instance_id is None:
                raise RuntimeError("cancel requested but no workflow instance id known")
            # Idempotent cancellation: check the actual engine state first so a
            # retried CANCEL after a lost response is a no-op when the instance
            # already ended (either cancelled by us or finished naturally).
            if adapter.get_process_instance(instance_id).state != "ENDED":
                adapter.cancel_process(process_instance_id=instance_id, reason=command.payload.get("reason"))

        else:
            raise ValueError(f"unknown command_type: {command.command_type}")

        command.state = CommandState.DONE.value
        command.last_error = None
        command.processed_at = _now_minus(0, db)

        if command.command_type == CommandType.CANCEL_PROCESS.value:
            request.lifecycle_state = LifecycleState.CLOSED.value
            request.outcome = RequestOutcome.CANCELLED.value
            request.closed_at = _now_minus(0, db)
            # Mirror the engine cancellation into local WorkItems.
            db.execute(
                update(WorkItem)
                .where(
                    WorkItem.request_id == request.id,
                    WorkItem.state == WorkItemState.ACTIVE.value,
                )
                .values(
                    state=WorkItemState.CANCELLED.value,
                )
            )

        db.commit()
    except Exception as exc:  # engine down, timeouts, adapter errors...
        logger.warning("command %s (type=%s) failed: %s", command.id, command.command_type, exc)
        db.rollback()
        command = db.get(WorkflowCommand, command.id)
        command.last_error = f"{type(exc).__name__}: {exc}"
        command.processed_at = _now_minus(0, db)
        if command.attempts >= max_attempts:
            command.state = CommandState.FAILED.value
        else:
            command.state = CommandState.PENDING.value
        db.commit()
        raise
    finally:
        try:
            adapter.close()
        except Exception:
            pass


def dispatch_once(db: Session, adapter_factory, limit: int = 10, max_attempts: int = 5) -> int:
    """Claim and dispatch pending commands. Returns number of commands processed."""
    rearm_stale_processing(db)
    commands = claim_pending_commands(db, limit=limit, max_attempts=max_attempts)
    dispatched = 0
    for cmd in commands:
        try:
            dispatch_one(db, cmd, adapter_factory, max_attempts=max_attempts)
            dispatched += 1
        except Exception:
            continue
    return dispatched


class OutboxDispatcher:
    def __init__(self, db_factory, adapter_factory, interval_seconds: float | None = None) -> None:
        self._db_factory = db_factory
        self._adapter_factory = adapter_factory
        self._interval = interval_seconds or get_settings().dispatch_interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="outbox-dispatcher", daemon=True)
        self._thread.start()
        logger.info("outbox dispatcher started (interval=%ss)", self._interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                db = self._db_factory()
                try:
                    dispatch_once(db, self._adapter_factory)
                finally:
                    db.close()
            except Exception as exc:
                logger.error("dispatcher loop error: %s", exc)
            self._stop.wait(self._interval)
