from app.domain.enums import LifecycleState, WorkItemState
from app.domain.models import Request, WorkItem
from app.workflow.base import EngineTask
from app.workflow.mock import MockAdapter
from app.workflow.reconciler import reconcile_once, upsert_work_item


def _active_request(db, instance_id="pi-1") -> Request:
    request = Request(
        number="REQ-REC-1",
        request_type="credit_decision",
        request_type_version=1,
        lifecycle_state=LifecycleState.ACTIVE.value,
        workflow_engine="OPERATON",
        workflow_instance_id=instance_id,
    )
    db.add(request)
    db.commit()
    return request


def test_upsert_work_item_is_idempotent(db):
    request = _active_request(db)
    wi1 = upsert_work_item(
        db, request_id=request.id, task_definition_key="review", external_task_id="ext-1"
    )
    db.commit()
    wi2 = upsert_work_item(
        db, request_id=request.id, task_definition_key="review", external_task_id="ext-1"
    )
    db.commit()
    assert wi1.id == wi2.id
    assert db.query(WorkItem).count() == 1


def test_reconcile_creates_work_items(db):
    request = _active_request(db)
    mock = MockAdapter()
    mock.process_instances["pi-1"] = mock.start_process("credit_decision", business_key=request.number)
    mock.tasks["t1"] = EngineTask(
        id="t1", task_definition_key="review", process_instance_id="pi-1"
    )

    summary = reconcile_once(db, lambda engine: mock)

    assert summary["requests_seen"] == 1
    assert summary["work_items_upserted"] == 1
    wi = db.query(WorkItem).one()
    assert wi.request_id == request.id
    assert wi.external_task_id == "t1"
    assert wi.state == WorkItemState.ACTIVE.value


def test_reconcile_survives_engine_absence(db):
    request = _active_request(db)

    class UnreachableAdapter:
        def get_active_human_tasks(self, instance_id=None):
            raise ConnectionError("engine unreachable")

        def close(self):
            pass

    summary = reconcile_once(db, lambda engine: UnreachableAdapter())

    assert summary["errors"]
    assert summary["requests_seen"] == 1
    assert db.query(WorkItem).count() == 0
