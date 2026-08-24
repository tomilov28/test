from app.domain.enums import CommandState, CommandType, LifecycleState, RequestOutcome
from app.domain.models import Request, WorkflowCommand
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
