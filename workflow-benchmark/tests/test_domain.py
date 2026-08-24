import pytest
from sqlalchemy.exc import IntegrityError

from app.domain.enums import CommandState, CommandType, LifecycleState, WorkItemState
from app.domain.models import Request, TaskResult, WorkflowCommand, WorkItem


def _request(db) -> Request:
    request = Request(
        number="REQ-TEST-1",
        request_type="credit_decision",
        request_type_version=1,
        lifecycle_state=LifecycleState.ACTIVE.value,
        workflow_engine="OPERATON",
    )
    db.add(request)
    db.flush()
    return request


def test_request_create_and_lifecycle(db):
    request = _request(db)
    assert request.lifecycle_state == LifecycleState.ACTIVE.value
    assert request.outcome is None
    assert request.workflow_instance_id is None


def test_work_item_unique_engine_task(db):
    request = _request(db)
    db.add(
        WorkItem(
            request_id=request.id,
            task_definition_key="review",
            external_task_id="ext-1",
            state=WorkItemState.ACTIVE.value,
        )
    )
    db.flush()
    db.add(
        WorkItem(
            request_id=request.id,
            task_definition_key="review",
            external_task_id="ext-1",
            state=WorkItemState.ACTIVE.value,
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()


def test_task_result_versioning(db):
    request = _request(db)
    wi = WorkItem(
        request_id=request.id,
        task_definition_key="review",
        external_task_id="ext-1",
        state=WorkItemState.ACTIVE.value,
    )
    db.add(wi)
    db.flush()
    db.add(TaskResult(work_item_id=wi.id, version=1, data={"approved": False}))
    db.add(TaskResult(work_item_id=wi.id, version=2, data={"approved": True}))
    db.commit()
    assert len(wi.results) == 2


def test_workflow_command_states(db):
    request = _request(db)
    cmd = WorkflowCommand(
        request_id=request.id,
        command_type=CommandType.START_PROCESS.value,
        payload={"process_key": "credit_decision"},
        state=CommandState.PENDING.value,
    )
    db.add(cmd)
    db.commit()
    assert cmd.state == CommandState.PENDING.value
    assert cmd.attempts == 0
