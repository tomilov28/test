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


def test_reconcile_closes_request_on_natural_completion(db):
    from app.domain.enums import RequestOutcome

    request = _active_request(db)
    mock = MockAdapter()
    instance = mock.start_process("credit_decision", business_key=request.number)
    instance.process_instance_id = "pi-1"
    instance.state = "ENDED"
    mock.process_instances["pi-1"] = instance

    summary = reconcile_once(db, lambda engine: mock)

    db.refresh(request)
    assert summary["completed_requests"] == 1
    assert request.lifecycle_state == LifecycleState.CLOSED.value
    assert request.outcome == RequestOutcome.COMPLETED.value
    assert request.closed_at is not None


def test_reconcile_does_not_overwrite_concurrent_cancel(db, tmp_path):
    """Regression: a request selected as ACTIVE must not be auto-closed as
    COMPLETED when a concurrent cancellation commits CLOSED/CANCELLED between
    the reconciler's SELECT and its auto-close UPDATE. The engine's ENDED state
    is indistinguishable from natural completion, so the auto-close guard must
    be re-evaluated against the latest committed row (atomic conditional
    UPDATE), never against a stale in-session snapshot."""
    import os

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import Base
    from app.domain.enums import RequestOutcome
    from app.workflow.mock import MockAdapter
    from app.workflow.reconciler import reconcile_once

    dbfile = str(tmp_path / "race.db")
    engine = create_engine(f"sqlite:///{dbfile}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    s1 = factory()
    request = Request(
        number="REQ-RACE-1",
        request_type="credit_decision",
        request_type_version=1,
        lifecycle_state=LifecycleState.ACTIVE.value,
        workflow_engine="OPERATON",
        workflow_instance_id="pi-1",
    )
    s1.add(request)
    s1.commit()

    s2 = factory()

    mock = MockAdapter()

    def _get_process_instance(instance_id=None):
        # Simulate the dispatcher's CANCEL_PROCESS committing CLOSED/CANCELLED
        # in the window between the reconciler's SELECT and its auto-close.
        row = s2.get(Request, request.id)
        row.lifecycle_state = LifecycleState.CLOSED.value
        row.outcome = RequestOutcome.CANCELLED.value
        s2.commit()
        instance = mock.start_process("credit_decision", business_key=request.number)
        instance.process_instance_id = instance_id
        instance.state = "ENDED"
        return instance

    mock.get_process_instance = _get_process_instance

    summary = reconcile_once(s1, lambda engine: mock)

    s1.expire_all()
    fresh = s1.get(Request, request.id)
    assert summary["completed_requests"] == 0
    assert fresh.lifecycle_state == LifecycleState.CLOSED.value
    assert fresh.outcome == RequestOutcome.CANCELLED.value

    s2.close()
    s1.close()
    engine.dispose()
    os.unlink(dbfile)
