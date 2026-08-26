"""Concurrency regression tests (C01, C02) against real PostgreSQL.

These run in the DEDICATED `benchmark_test` database (same as
test_outbox_lease) so they never race the live harness. They exercise REAL
database concurrency (separate sessions/transactions, row locks, real
claim/re-arm/dispatch code); only the engine boundary is a MockAdapter.

  C01  concurrent START around lease expiry: one dispatcher crashes mid-start
       (engine instance exists, command still PROCESSING, request row locked);
       a second dispatcher re-arms and re-claims the same command and dispatches
       it concurrently. Verifies the request-row lock + query-before-start
       serialize same-request STARTs so exactly ONE engine instance is created.

       Honest limit (strict exactly-once is not provable): query-before-start
       provides retry idempotency but not strict exactly-once under overlapping
       dispatch after lease expiry. The engines have no unique business-key
       constraint; strictness rests on the app-level request-row lock, which
       closes the same-request overlap exercised here (see
       test_c01_engine_lookup_not_unique for the engine-level gap).

  C02  concurrent CANCEL vs final completion: two parallel transactions race a
       terminal domain action on the same Request. The guarded conditional
       UPDATE makes it deterministic "first committed wins": exactly one
       terminal action commits, the loser gets a 409, the state is consistent
       with the winner, terminal state is immutable afterwards, and the engine
       convergence commands cannot change the business winner.
"""

import os
import threading
import time
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.api.routes import cancel_request, complete_work_item
from app.api.schemas import CancelRequestIn, CompleteWorkItemIn
from app.db import Base
from app.domain.completion import DECISION_OUTCOMES
from app.domain.enums import CommandState, CommandType, LifecycleState, RequestOutcome, WorkItemState
from app.domain.models import Request, TaskResult, WorkItem, WorkflowCommand
from app.workflow.dispatcher import (
    claim_pending_commands,
    dispatch_once,
    dispatch_one,
    rearm_stale_processing,
)
from app.workflow.mock import MockAdapter

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get("BENCH_DATABASE_URL", "postgresql+psycopg2://benchmark:benchmark@localhost:5432/benchmark")
TEST_DB = "benchmark_test"


@pytest.fixture(scope="module")
def lease_engine():
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    with admin.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": TEST_DB}
        ).scalar()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
    admin.dispose()

    url = ADMIN_URL.rsplit("/", 1)[0] + "/" + TEST_DB
    engine = create_engine(url, poolclass=NullPool)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def factory(lease_engine):
    f = sessionmaker(bind=lease_engine, autoflush=False, expire_on_commit=False)
    yield f
    Base.metadata.drop_all(lease_engine)
    Base.metadata.create_all(lease_engine)


def _seed_request(factory, *, with_start_command: bool) -> Request:
    session = factory()
    try:
        request = Request(
            number=f"REQ-C{int(time.time() * 1000)}-{uuid.uuid4().hex[:6].upper()}",
            request_type="credit_decision",
            request_type_version=1,
            workflow_engine="OPERATON",
        )
        session.add(request)
        session.flush()
        if with_start_command:
            session.add(
                WorkflowCommand(
                    request_id=request.id,
                    command_type=CommandType.START_PROCESS.value,
                    payload={
                        "process_key": request.request_type,
                        "business_key": request.number,
                    },
                    state=CommandState.PENDING.value,
                )
            )
        session.commit()
        return request
    finally:
        session.close()


class BlockingStartAdapter(MockAdapter):
    """Barrier-controlled adapter: start_process records that it entered and
    blocks until released, keeping the dispatcher's transaction open (request
    row locked) with the engine instance already created but not yet committed -
    exactly the ambiguous mid-start window after lease expiry."""

    def __init__(self) -> None:
        super().__init__()
        self.start_calls = 0
        self.find_calls = 0
        self.start_entered = threading.Event()
        self.start_proceed = threading.Event()

    def find_process_instance_by_business_key(self, process_key: str, business_key: str):
        self.find_calls += 1
        return super().find_process_instance_by_business_key(process_key, business_key)

    def start_process(self, process_key: str, business_key: str | None = None, variables=None, version=None):
        self.start_calls += 1
        self.start_entered.set()
        result = super().start_process(process_key, business_key=business_key, variables=variables, version=version)
        self.start_proceed.wait(timeout=15)
        return result


def test_c01_overlapping_dispatch_after_lease_expiry_no_duplicate_instance(factory):
    """C01: a stale re-arm + re-dispatch overlapping the original in-flight
    START must not create a second engine instance."""
    request = _seed_request(factory, with_start_command=True)
    mock = BlockingStartAdapter()

    def adapter_factory(_engine=None):
        return mock

    # dispatcher A claims the START command and dispatches it. It creates the
    # engine instance but blocks before committing (mid-start, lease running).
    s1 = factory()
    claimed = claim_pending_commands(s1, limit=10)
    assert len(claimed) == 1
    cmd = claimed[0]

    def run_a():
        dispatch_one(s1, cmd, adapter_factory)

    ta = threading.Thread(target=run_a)
    ta.start()
    assert mock.start_entered.wait(timeout=10), "dispatcher A never entered start_process"
    assert mock.start_calls == 1

    # dispatcher B: the lease has expired; it re-arms and re-claims the SAME
    # command, then dispatches it while A still holds the request row lock.
    s2 = factory()
    rearmed = rearm_stale_processing(s2, stale_after_seconds=0)
    assert rearmed == 1, "stale PROCESSING command must be re-armed"
    re_claimed = claim_pending_commands(s2, limit=10)
    assert len(re_claimed) == 1 and re_claimed[0].id == cmd.id

    b_done = threading.Event()

    def run_b():
        try:
            dispatch_one(s2, re_claimed[0], adapter_factory)
        finally:
            b_done.set()

    tb = threading.Thread(target=run_b)
    tb.start()

    # B must be serialized on the request row lock: it cannot complete and must
    # not start a second instance while A's transaction is still open.
    time.sleep(1.0)
    assert not b_done.is_set(), "dispatcher B must block on the request row lock while A holds it"
    assert mock.start_calls == 1, "B started an instance while A's START was in flight"

    # release A: it commits the instance id, then B proceeds and reuses it.
    mock.start_proceed.set()
    ta.join(timeout=15)
    tb.join(timeout=15)
    assert not ta.is_alive() and not tb.is_alive()

    # exactly one engine instance for the business key, one start call, and both
    # dispatches ran the query-before-start (idempotent reuse path).
    matches = mock.find_process_instance_by_business_key(request.request_type, request.number)
    assert len(matches) == 1, f"duplicate engine instances after lease-expiry overlap: {matches}"
    assert mock.start_calls == 1, "start_process must be invoked exactly once"
    assert mock.find_calls >= 2, "query-before-start must run on every dispatch"

    s3 = factory()
    try:
        req = s3.get(Request, request.id)
        assert req.workflow_instance_id == matches[0].process_instance_id
        done = s3.get(WorkflowCommand, cmd.id)
        assert done.state == CommandState.DONE.value
        assert done.attempts == 2  # claimed once by A and once by B
    finally:
        s3.close()
    s1.close()
    s2.close()


def test_c01_engine_lookup_not_unique(factory):
    """C01 capability note: the engine has no unique business-key constraint.

    query-before-start is RETRY idempotency, not strict exactly-once. Two
    sequential start attempts that each miss the lookup (the equivalent of two
    dispatchers that never serialized on the request row) create two engine
    instances. The request-row lock in dispatch_one is what closes the
    same-request overlap; without it the engines would double-start."""
    request = _seed_request(factory, with_start_command=False)
    mock = MockAdapter()
    assert mock.find_process_instance_by_business_key(request.request_type, request.number) == []
    first = mock.start_process(process_key=request.request_type, business_key=request.number)
    second = mock.start_process(process_key=request.request_type, business_key=request.number)
    assert first.process_instance_id != second.process_instance_id
    matches = mock.find_process_instance_by_business_key(request.request_type, request.number)
    assert len(matches) == 2, "engine accepted two instances for one business key"


def test_c02_concurrent_cancel_vs_final_completion(factory):
    """C02: concurrent CANCEL and final COMPLETE race the same Request; exactly
    one terminal action commits (first committed wins), the loser gets a 409,
    and the terminal outcome is immutable afterwards."""
    request = _seed_request(factory, with_start_command=False)
    session = factory()
    try:
        wi = WorkItem(
            request_id=request.id,
            task_definition_key="final_decision",
            external_task_id=f"task-final-{request.number}",
            state=WorkItemState.ACTIVE.value,
        )
        session.add(wi)
        session.commit()
        wi_id = wi.id
    finally:
        session.close()

    barrier = threading.Barrier(2)
    results: dict[str, tuple] = {}

    def run_cancel():
        db = factory()
        barrier.wait()
        try:
            cancel_request(request.id, CancelRequestIn(reason="c02-race"), db)
            results["cancel"] = ("ok", None)
        except HTTPException as exc:
            db.rollback()
            results["cancel"] = ("http", exc.status_code)
        finally:
            db.close()

    def run_complete():
        db = factory()
        barrier.wait()
        try:
            complete_work_item(wi_id, CompleteWorkItemIn(version=1, data={"decision": "APPROVE"}), db)
            results["complete"] = ("ok", None)
        except HTTPException as exc:
            db.rollback()
            results["complete"] = ("http", exc.status_code)
        finally:
            db.close()

    threads = [threading.Thread(target=run_cancel), threading.Thread(target=run_complete)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [k for k, (status, _) in results.items() if status == "ok"]
    losers = [code for _, (status, code) in results.items() if status == "http"]
    assert len(winners) == 1, f"exactly one terminal action must win, got {results}"
    assert len(losers) == 1 and losers == [409], f"loser must get HTTP 409, got {results}"
    winner = winners[0]

    db = factory()
    try:
        req = db.get(Request, request.id)
        assert req.lifecycle_state == LifecycleState.CLOSED.value
        outcome = req.outcome
        assert outcome in (RequestOutcome.CANCELLED.value, RequestOutcome.COMPLETED.value)
        assert req.closed_at is not None

        db.expire_all()
        final_wi = db.get(WorkItem, wi_id)
        result_count = db.query(TaskResult).filter_by(work_item_id=wi_id).count()
        cmd_types = {c.command_type for c in db.query(WorkflowCommand).filter_by(request_id=request.id).all()}

        if winner == "complete":
            assert outcome == DECISION_OUTCOMES["APPROVE"], "winner COMPLETE must set COMPLETED"
            assert final_wi.state == WorkItemState.COMPLETED.value
            assert result_count == 1, "winner COMPLETE must persist exactly one TaskResult"
            assert CommandType.COMPLETE_TASK.value in cmd_types
            assert CommandType.CANCEL_PROCESS.value not in cmd_types
        else:
            assert outcome == RequestOutcome.CANCELLED.value, "winner CANCEL must set CANCELLED"
            assert final_wi.state == WorkItemState.CANCELLED.value
            assert result_count == 0, "loser COMPLETE must leave no TaskResult behind"
            assert CommandType.CANCEL_PROCESS.value in cmd_types
            assert CommandType.COMPLETE_TASK.value not in cmd_types
    finally:
        db.close()

    # immutability: the losing operation is rejected afterwards, state unchanged
    db2 = factory()
    try:
        if winner == "complete":
            with pytest.raises(HTTPException) as ei:
                cancel_request(request.id, CancelRequestIn(reason="c02-retry"), db2)
            assert ei.value.status_code == 409
        else:
            with pytest.raises(HTTPException) as ei:
                complete_work_item(wi_id, CompleteWorkItemIn(version=1, data={"decision": "APPROVE"}), db2)
            assert ei.value.status_code == 409
        db2.rollback()
    finally:
        db2.close()

    db3 = factory()
    try:
        db3.expire_all()
        req2 = db3.get(Request, request.id)
        assert req2.outcome == outcome
        assert req2.lifecycle_state == LifecycleState.CLOSED.value
        assert db3.query(TaskResult).filter_by(work_item_id=wi_id).count() == result_count
    finally:
        db3.close()

    # engine technical convergence commands cannot change the business winner
    mock = MockAdapter()
    s = factory()
    try:
        dispatch_once(s, lambda _engine=None: mock)
    finally:
        s.close()
    db4 = factory()
    try:
        db4.expire_all()
        req3 = db4.get(Request, request.id)
        assert req3.outcome == outcome
        assert req3.lifecycle_state == LifecycleState.CLOSED.value
    finally:
        db4.close()
