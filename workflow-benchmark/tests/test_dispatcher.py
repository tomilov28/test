from app.domain.enums import CommandState, CommandType, LifecycleState, RequestOutcome
from app.domain.models import Request, WorkflowCommand
from app.workflow.base import ProcessInstanceInfo
from app.workflow.dispatcher import claim_pending_commands, dispatch_once, rearm_stale_processing
from app.workflow.mock import MockAdapter


def _active_request(db, engine="OPERATON") -> Request:
    request = Request(
        number=f"REQ-{engine}-1",
        request_type="credit_decision",
        request_type_version=1,
        lifecycle_state=LifecycleState.ACTIVE.value,
        workflow_engine=engine,
    )
    db.add(request)
    db.flush()
    return request


def _adapter_factory(mock: MockAdapter):
    def factory(engine: str):
        return mock

    return factory


def test_start_process_command_marks_request(db):
    request = _active_request(db)
    db.add(
        WorkflowCommand(
            request_id=request.id,
            command_type=CommandType.START_PROCESS.value,
            payload={"process_key": request.request_type, "business_key": request.number},
        )
    )
    db.commit()

    mock = MockAdapter()
    dispatched = dispatch_once(db, _adapter_factory(mock))

    assert dispatched == 1
    db.refresh(request)
    assert request.workflow_instance_id is not None
    cmd = db.query(WorkflowCommand).one()
    assert cmd.state == CommandState.DONE.value


def test_failed_command_retries_then_fails(db):
    request = _active_request(db)
    db.add(
        WorkflowCommand(
            request_id=request.id,
            command_type=CommandType.START_PROCESS.value,
            payload={"process_key": request.request_type},
        )
    )
    db.commit()

    mock = MockAdapter()
    mock.fail_start = True
    max_attempts = 3
    for _ in range(max_attempts - 1):
        dispatch_once(db, _adapter_factory(mock), max_attempts=max_attempts)
        cmd = db.query(WorkflowCommand).one()
        assert cmd.state == CommandState.PENDING.value
        assert cmd.last_error is not None

    dispatch_once(db, _adapter_factory(mock), max_attempts=max_attempts)
    cmd = db.query(WorkflowCommand).one()
    assert cmd.state == CommandState.FAILED.value
    assert cmd.attempts == max_attempts


def test_cancel_command_closes_request(db):
    request = _active_request(db)
    request.workflow_instance_id = "pi-1"
    db.add(
        WorkflowCommand(
            request_id=request.id,
            command_type=CommandType.CANCEL_PROCESS.value,
            payload={"workflow_instance_id": "pi-1"},
        )
    )
    db.commit()

    mock = MockAdapter()
    dispatch_once(db, _adapter_factory(mock))

    db.refresh(request)
    assert request.lifecycle_state == LifecycleState.CLOSED.value
    assert request.outcome == RequestOutcome.CANCELLED.value
    assert request.closed_at is not None


def test_claim_is_atomic_and_excludes_processing(db):
    request = _active_request(db)
    for i in range(3):
        db.add(
            WorkflowCommand(
                request_id=request.id,
                command_type=CommandType.START_PROCESS.value,
                payload={"i": i},
            )
        )
    db.commit()

    claimed = claim_pending_commands(db, limit=10)
    assert len(claimed) == 3
    assert all(c.state == CommandState.PROCESSING.value for c in claimed)
    assert claim_pending_commands(db, limit=10) == []


def test_start_process_reuses_existing_instance(db):
    """A retried START after a lost response reuses the instance the first
    attempt created instead of starting a second one (business-key lookup)."""
    request = _active_request(db)
    existing = ProcessInstanceInfo(process_instance_id="pi-existing", state="ACTIVE", business_key=request.number)
    db.add(
        WorkflowCommand(
            request_id=request.id,
            command_type=CommandType.START_PROCESS.value,
            payload={"process_key": request.request_type, "business_key": request.number},
        )
    )
    db.commit()

    mock = MockAdapter()
    mock.process_instances["pi-existing"] = existing
    dispatch_once(db, _adapter_factory(mock))

    db.refresh(request)
    assert request.workflow_instance_id == "pi-existing"
    # start_process must not have been called: only one instance ever exists
    assert "pi-1" not in mock.process_instances


def test_complete_task_state_already_achieved(db):
    """A retried COMPLETE whose engine task is already gone is a success."""
    request = _active_request(db)
    db.add(
        WorkflowCommand(
            request_id=request.id,
            command_type=CommandType.COMPLETE_TASK.value,
            payload={"external_task_id": "gone-task", "data": {"ok": True}},
        )
    )
    db.commit()

    mock = MockAdapter()  # no task "gone-task" -> requested state already achieved
    dispatch_once(db, _adapter_factory(mock))

    cmd = db.query(WorkflowCommand).one()
    assert cmd.state == CommandState.DONE.value
    assert mock.completed == []


def test_cancel_already_ended_skips_engine_call(db):
    """A retried CANCEL for an already-ended instance converges without a 500."""
    request = _active_request(db)
    request.workflow_instance_id = "pi-ended"
    db.add(
        WorkflowCommand(
            request_id=request.id,
            command_type=CommandType.CANCEL_PROCESS.value,
            payload={"workflow_instance_id": "pi-ended"},
        )
    )
    db.commit()

    mock = MockAdapter()
    mock.process_instances["pi-ended"] = ProcessInstanceInfo(
        process_instance_id="pi-ended", state="ENDED"
    )
    dispatch_once(db, _adapter_factory(mock))

    db.refresh(request)
    assert request.lifecycle_state == LifecycleState.CLOSED.value
    assert request.outcome == RequestOutcome.CANCELLED.value
    assert mock.cancelled == []  # engine already ended the instance


def test_stale_processing_commands_are_rearmed(db):
    from datetime import datetime, timedelta, timezone

    request = _active_request(db)
    cmd = WorkflowCommand(
        request_id=request.id,
        command_type=CommandType.START_PROCESS.value,
        payload={},
        state=CommandState.PROCESSING.value,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    )
    db.add(cmd)
    db.commit()

    rearmed = rearm_stale_processing(db, stale_after_seconds=60)
    assert rearmed == 1
    db.refresh(cmd)
    assert cmd.state == CommandState.PENDING.value
