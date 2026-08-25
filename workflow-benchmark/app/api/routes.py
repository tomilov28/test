import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.api.schemas import (
    CancelRequestIn,
    CommandOut,
    CompleteWorkItemIn,
    FaultArmIn,
    FaultClearIn,
    HealthOut,
    ReconcileResult,
    RequestCreate,
    RequestDetail,
    RequestOut,
    WorkItemOut,
)
from app.db import get_db
from app.domain.completion import DECISION_OUTCOMES, validate_completion_contract
from app.domain.enums import (
    CommandState,
    CommandType,
    LifecycleState,
    RequestOutcome,
    WorkItemState,
)
from app.domain.models import Request, TaskResult, WorkItem, WorkflowCommand
from app.workflow.fault_injector import SUPPORTED_OPERATIONS, controller
from app.workflow.reconciler import reconcile_once
from app.workflow.registry import build_adapter

router = APIRouter()


def _enqueue_command(db: Session, request_id: uuid.UUID, command_type: str, payload: dict) -> WorkflowCommand:
    cmd = WorkflowCommand(request_id=request_id, command_type=command_type, payload=payload)
    db.add(cmd)
    return cmd


def _get_request_or_404(db: Session, request_id: uuid.UUID) -> Request:
    request = db.get(Request, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="request not found")
    return request


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


@router.post("/requests", response_model=RequestOut, status_code=201)
def create_request(body: RequestCreate, db: Session = Depends(get_db)):
    number = f"REQ-{uuid.uuid4().hex[:12].upper()}"
    request = Request(
        number=number,
        request_type=body.request_type,
        request_type_version=body.request_type_version,
        lifecycle_state=LifecycleState.ACTIVE.value,
        workflow_engine=body.workflow_engine,
    )
    db.add(request)
    db.flush()
    _enqueue_command(
        db,
        request.id,
        CommandType.START_PROCESS.value,
        {"process_key": body.request_type, "business_key": number, "variables": body.variables},
    )
    db.commit()
    db.refresh(request)
    return request


@router.get("/requests", response_model=list[RequestOut])
def list_requests(db: Session = Depends(get_db)):
    return db.execute(select(Request).order_by(Request.created_at.desc()).limit(100)).scalars().all()


@router.get("/requests/{request_id}", response_model=RequestDetail)
def get_request(request_id: uuid.UUID, db: Session = Depends(get_db)):
    request = _get_request_or_404(db, request_id)
    request.work_items  # force lazy load
    return request


@router.get("/requests/{request_id}/work-items", response_model=list[WorkItemOut])
def list_work_items(request_id: uuid.UUID, db: Session = Depends(get_db)):
    _get_request_or_404(db, request_id)
    return db.execute(select(WorkItem).where(WorkItem.request_id == request_id)).scalars().all()


@router.get("/requests/{request_id}/commands", response_model=list[CommandOut])
def list_commands(request_id: uuid.UUID, db: Session = Depends(get_db)):
    _get_request_or_404(db, request_id)
    return db.execute(select(WorkflowCommand).where(WorkflowCommand.request_id == request_id)).scalars().all()


@router.post("/work-items/{work_item_id}/complete", response_model=WorkItemOut)
def complete_work_item(work_item_id: uuid.UUID, body: CompleteWorkItemIn, db: Session = Depends(get_db)):
    work_item = db.get(WorkItem, work_item_id)
    if work_item is None:
        raise HTTPException(status_code=404, detail="work item not found")
    if work_item.state != WorkItemState.ACTIVE.value:
        raise HTTPException(status_code=409, detail=f"work item not ACTIVE (state={work_item.state})")

    from datetime import datetime, timezone

    # Domain-first completion (audit A02): validate the CompletionContract in the
    # same transaction that applies the final domain action. A contract violation
    # leaves the Request and WorkItem untouched.
    try:
        decision = validate_completion_contract(work_item.task_definition_key, body.data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    request = db.get(Request, work_item.request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="request not found")

    result = TaskResult(
        work_item_id=work_item.id,
        version=body.version,
        data=body.data,
        created_at=datetime.now(timezone.utc),
    )
    db.add(result)
    work_item.state = WorkItemState.COMPLETED.value
    work_item.completed_at = result.created_at

    closes_request = decision is not None
    if closes_request:
        # The final domain action assigns the business outcome atomically.
        request.lifecycle_state = LifecycleState.CLOSED.value
        request.outcome = DECISION_OUTCOMES[decision]
        request.closed_at = result.created_at

    _enqueue_command(
        db,
        work_item.request_id,
        CommandType.COMPLETE_TASK.value,
        {
            "work_item_id": str(work_item.id),
            "external_task_id": work_item.external_task_id,
            "task_definition_key": work_item.task_definition_key,
            "data": body.data,
            "version": body.version,
        },
    )
    db.commit()
    db.refresh(work_item)
    return work_item


@router.post("/requests/{request_id}/cancel", response_model=RequestOut)
def cancel_request(request_id: uuid.UUID, body: CancelRequestIn, db: Session = Depends(get_db)):
    # Domain-first cancellation (audit A01): the Request acquires its business
    # outcome IMMEDIATELY, in the same transaction that enqueues the engine
    # command. The dispatcher then performs technical convergence (terminating
    # the engine process) as a subsequent step; it never decides the outcome.
    request = _get_request_or_404(db, request_id)
    if request.lifecycle_state != LifecycleState.ACTIVE.value:
        raise HTTPException(status_code=409, detail="request is not ACTIVE")

    request.lifecycle_state = LifecycleState.CLOSED.value
    request.outcome = RequestOutcome.CANCELLED.value
    request.closed_at = _utcnow()
    db.execute(
        update(WorkItem)
        .where(
            WorkItem.request_id == request.id,
            WorkItem.state == WorkItemState.ACTIVE.value,
        )
        .values(state=WorkItemState.CANCELLED.value)
    )
    _enqueue_command(
        db,
        request.id,
        CommandType.CANCEL_PROCESS.value,
        {"workflow_instance_id": request.workflow_instance_id, "reason": body.reason},
    )
    db.commit()
    db.refresh(request)
    return request


@router.post("/admin/reconcile", response_model=ReconcileResult)
def admin_reconcile(db: Session = Depends(get_db)):
    return reconcile_once(db, build_adapter)


@router.post("/admin/commands/{command_id}/requeue", response_model=CommandOut)
def admin_requeue_command(command_id: uuid.UUID, db: Session = Depends(get_db)):
    """Requeue a FAILED technical command (audit A01b).

    A command whose technical retries are exhausted stays FAILED; the domain
    outcome is unchanged (it was decided at enqueue time). This endpoint is the
    minimal recovery path: reset to PENDING with a fresh attempt budget so the
    engine convergence operation can be retried after a long outage, without
    re-running the domain action and without touching the business outcome.
    """
    command = db.get(WorkflowCommand, command_id)
    if command is None:
        raise HTTPException(status_code=404, detail="command not found")
    if command.state != CommandState.FAILED.value:
        raise HTTPException(status_code=409, detail=f"command not FAILED (state={command.state})")
    command.state = CommandState.PENDING.value
    command.attempts = 0
    command.last_error = None
    command.processed_at = None
    command.processing_started_at = None
    db.commit()
    db.refresh(command)
    return command


# ---- fault injection control surface (benchmark/test only) -----------------


@router.post("/admin/faults/arm")
def admin_faults_arm(body: FaultArmIn):
    """Arm a fault for an engine+operation. Inert unless called; nothing is
    armed in the default production configuration."""
    controller.arm(body.engine, body.operation, body.mode, remaining=body.remaining)
    return {"armed": body.engine, "operations": controller.snapshot().get(body.engine, {})}


@router.post("/admin/faults/clear")
def admin_faults_clear(body: FaultClearIn):
    cleared = controller.clear(body.engine, body.operation)
    return {"cleared": cleared, "operations": controller.snapshot()}


@router.get("/admin/faults")
def admin_faults_status():
    return {"operations": controller.snapshot(), "supported": list(SUPPORTED_OPERATIONS)}


@router.get("/health", response_model=HealthOut)
def health(db: Session = Depends(get_db)):
    db_ok = "ok"
    try:
        db.execute(select(1))
    except Exception as exc:
        db_ok = f"error: {exc}"

    engines: dict[str, str] = {}
    for name, url in (("operaton", None), ("flowable", None)):
        from app.config import get_settings

        settings = get_settings()
        target = settings.engine_operaton_url if name == "operaton" else settings.engine_flowable_url
        engines[name] = f"configured:{target}"

    return HealthOut(status="ok" if db_ok == "ok" else "degraded", database=db_ok, engines=engines)
