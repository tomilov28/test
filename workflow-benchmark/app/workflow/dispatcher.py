"""Transactional outbox dispatcher.

Polls PENDING WorkflowCommand rows and hands them to the appropriate engine
adapter. The command is written in the SAME database transaction as the domain
change it represents; the dispatcher only ever talks to the engine via the
public REST API.

At-least-once semantics: a command is claimed with an atomic PENDING ->
PROCESSING transition (writing `processing_started_at` as a lease timestamp),
then dispatched. If the process crashes between claim and DONE the command
stays PROCESSING forever; rearm_stale_processing() returns stale PROCESSING
commands (judged by the LEASE timestamp, never by `created_at`) to PENDING so
they survive restarts.

Domain-first rule (audit A01/A02): the dispatcher NEVER decides a business
outcome. Cancellation and completion close the Request at the API layer in the
same transaction that enqueues the engine command. The dispatcher only performs
technical convergence on the engine. For a request that was already cancelled
it must converge the engine to the already-decided state without ever starting
new work or overwriting the outcome.
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


def _now_minus(seconds: float, db: Session):
    from datetime import datetime, timedelta, timezone

    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


def claim_pending_commands(db: Session, limit: int = 10, max_attempts: int = 5) -> list[WorkflowCommand]:
    """Atomically claim PENDING commands for dispatch.

    The PENDING -> PROCESSING transition is guarded by FOR UPDATE SKIP LOCKED,
    so two concurrent dispatchers can never claim the same row (A03b). The lease
    timestamp `processing_started_at` is written here, at claim time; stale
    re-arm is computed from it, never from `created_at` (A03a).
    """
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
    now = _now_minus(0, db)
    for cmd in rows:
        cmd.state = CommandState.PROCESSING.value
        cmd.attempts += 1
        cmd.processing_started_at = now
    db.commit()
    return rows


def rearm_stale_processing(db: Session, stale_after_seconds: float = STALE_PROCESSING_AFTER_SECONDS) -> int:
    """Return crashed PROCESSING commands to PENDING so they are retried.

    Staleness is measured from `processing_started_at` (the lease timestamp set
    when the command was claimed), NOT from `created_at`: a command created long
    ago but claimed moments ago must not be re-armed (A03a).
    """
    threshold = _now_minus(stale_after_seconds, db)
    res = db.execute(
        update(WorkflowCommand)
        .where(
            WorkflowCommand.state == CommandState.PROCESSING.value,
            WorkflowCommand.processing_started_at.is_not(None),
            WorkflowCommand.processing_started_at < threshold,
        )
        .values(state=CommandState.PENDING.value, processing_started_at=None)
    )
    db.commit()
    return res.rowcount or 0


def _mark_command_result(db: Session, command_id, *, state: str, error: str | None, reset_lease: bool) -> None:
    cmd = db.get(WorkflowCommand, command_id)
    if cmd is None:
        return
    cmd.state = state
    cmd.last_error = error
    cmd.processed_at = _now_minus(0, db)
    if reset_lease:
        cmd.processing_started_at = None
    db.commit()


def _cancel_engine_instances(adapter, instances, reason: str | None) -> None:
    """Terminate every running engine instance idempotently (convergence)."""
    for inst in instances:
        if inst.state != "ENDED":
            try:
                adapter.cancel_process(process_instance_id=inst.process_instance_id, reason=reason)
            except Exception:
                # still-running after a failed cancel is fine: the dispatcher
                # will retry the command and converge again.
                raise


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
        _mark_command_result(
            db, command.id, state=CommandState.FAILED.value, error="request not found", reset_lease=True
        )
        return

    adapter = adapter_factory(request.workflow_engine)
    request_cancelled = (
        request.lifecycle_state == LifecycleState.CLOSED.value
        and request.outcome == RequestOutcome.CANCELLED.value
    )
    try:
        if command.command_type == CommandType.START_PROCESS.value:
            if request_cancelled:
                # Domain-first cancellation (A01): the request was cancelled
                # before the process was (durably) started. Never start new
                # work. If an earlier ambiguous START already created an engine
                # instance, converge it by terminating that instance so no
                # orphan process survives recovery (A01c).
                existing = adapter.find_process_instance_by_business_key(
                    request.request_type, request.number
                )
                _cancel_engine_instances(adapter, existing, reason="cancelled-before-start")
                if existing:
                    request.workflow_instance_id = existing[0].process_instance_id
            else:
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
            if request_cancelled:
                # The domain decision was CANCELLED: never complete a task on a
                # process the domain has cancelled. The requested state is
                # already achieved at the domain layer -> technical success (A04).
                pass
            else:
                # Idempotent completion: if the engine task no longer exists the
                # requested state (task completed) is already achieved, so a retried
                # COMPLETE after a lost response is a success, not a failure (A04).
                # If the request is still ACTIVE but its process unexpectedly ended,
                # the reconciler records that as a workflow anomaly (A02c); the
                # command itself has nothing left to do on the engine.
                task = adapter.get_human_task(external_task_id=command.payload["external_task_id"])
                if task is not None:
                    adapter.complete_human_task(
                        external_task_id=command.payload["external_task_id"],
                        variables=command.payload.get("data") or {},
                    )

        elif command.command_type == CommandType.CANCEL_PROCESS.value:
            # Technical convergence ONLY. The business outcome (CANCELLED) was
            # already committed by the domain transaction that enqueued this
            # command (A01); the dispatcher must never overwrite it.
            instance_id = command.payload.get("workflow_instance_id") or request.workflow_instance_id
            if instance_id is not None:
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
        command.processing_started_at = None

        db.commit()
    except Exception as exc:  # engine down, timeouts, adapter errors...
        logger.warning("command %s (type=%s) failed: %s", command.id, command.command_type, exc)
        db.rollback()
        _mark_command_result(
            db,
            command.id,
            state=CommandState.PENDING.value if command.attempts < max_attempts else CommandState.FAILED.value,
            error=f"{type(exc).__name__}: {exc}",
            reset_lease=True,
        )
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
