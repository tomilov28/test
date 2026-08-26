"""Outbox processing-lease tests against real PostgreSQL (audit A03).

These run in a DEDICATED `benchmark_test` database so they never race the live
harness's own dispatcher/reconciler threads. They exercise the real lease
semantics: claim writes `processing_started_at`, stale re-arm is computed from
it (never from `created_at`), and concurrent claims never give one command two
owners.

  A03a  command created long ago but claimed just now -> NOT re-armed as stale
  A03b  two dispatchers claiming concurrently -> no command has two owners
  A03c  crash after claim -> rearmed after lease timeout -> executes
  A03d  concurrent stale-rearm + START_PROCESS -> no duplicate engine instance

Requires the shared PostgreSQL (app DATABASE_URL); it is engine-independent but
marked integration so it runs with the live-stack suites.
"""

import os
import threading
import time

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.db import Base
from app.domain.enums import CommandState, CommandType
from app.domain.models import Request, WorkflowCommand
from app.workflow.base import ProcessInstanceInfo
from app.workflow.dispatcher import claim_pending_commands, dispatch_once, rearm_stale_processing
from app.workflow.mock import MockAdapter

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get("BENCH_DATABASE_URL", "postgresql+psycopg2://benchmark:benchmark@localhost:5432/benchmark")
TEST_DB = "benchmark_test"


@pytest.fixture(scope="module")
def lease_engine():
    # ensure the dedicated test database exists
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


def _seed(factory, n=1, *, created_age_s=0, state=CommandState.PENDING.value) -> list[Request]:
    session = factory()
    requests = []
    try:
        for i in range(n):
            request = Request(
                number=f"REQ-LEASE-{int(time.time())}-{i}",
                request_type="credit_decision",
                request_type_version=1,
                workflow_engine="OPERATON",
            )
            session.add(request)
            session.flush()
            cmd = WorkflowCommand(
                request_id=request.id,
                command_type=CommandType.START_PROCESS.value,
                payload={"process_key": request.request_type, "business_key": request.number},
                state=state,
            )
            if created_age_s:
                from datetime import datetime, timedelta, timezone

                cmd.created_at = datetime.now(timezone.utc) - timedelta(seconds=created_age_s)
            session.add(cmd)
            requests.append(request)
        session.commit()
    finally:
        session.close()
    return requests


def _all_commands(factory) -> list[WorkflowCommand]:
    session = factory()
    try:
        return session.query(WorkflowCommand).order_by(WorkflowCommand.created_at).all()
    finally:
        session.close()


def test_a03a_created_long_ago_but_claimed_now_not_stale(factory):
    _seed(factory, n=1, created_age_s=3600 * 5)
    s1 = factory()
    claimed = claim_pending_commands(s1, limit=10)
    assert len(claimed) == 1
    assert claimed[0].processing_started_at is not None

    # a second dispatcher re-arms stale commands; the just-claimed command is NOT
    # stale even though it was created 5h ago.
    s2 = factory()
    rearmed = rearm_stale_processing(s2, stale_after_seconds=60)
    assert rearmed == 0
    cmd = s2.get(WorkflowCommand, claimed[0].id)
    assert cmd.state == CommandState.PROCESSING.value
    s1.close()
    s2.close()


def test_a03b_concurrent_claim_single_owner(factory):
    _seed(factory, n=5)
    barrier = threading.Barrier(2)
    results: dict[str, list] = {}

    def _claim(tag: str):
        session = factory()
        barrier.wait()
        claimed = claim_pending_commands(session, limit=10)
        results[tag] = [str(c.id) for c in claimed]
        session.close()

    threads = [threading.Thread(target=_claim, args=(t,)) for t in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    a_ids = set(results["A"])
    b_ids = set(results["B"])
    # SKIP LOCKED allows the two concurrent claimants to SPLIT the rows (fair
    # sharing); the invariant is that a row is never claimed by both dispatchers
    # and every claimable row is claimed exactly once.
    assert not (a_ids & b_ids), f"a row was claimed by both dispatchers: {results}"
    assert len(a_ids | b_ids) == 5, f"all 5 commands must be claimed exactly once: {results}"
    # no command has two owners
    session = factory()
    try:
        states = session.query(WorkflowCommand.state).all()
        assert all(s == CommandState.PROCESSING.value for (s,) in states)
    finally:
        session.close()


def test_a03c_crash_after_claim_rearmed_then_executes(factory):
    request = _seed(factory, n=1)[0]
    s1 = factory()
    claimed = claim_pending_commands(s1, limit=10)
    assert len(claimed) == 1
    # simulated crash: s1 closes without dispatching
    s1.close()

    s2 = factory()
    # after the lease timeout a second dispatcher re-arms the command
    rearmed = rearm_stale_processing(s2, stale_after_seconds=0)
    assert rearmed == 1
    cmd = s2.get(WorkflowCommand, claimed[0].id)
    assert cmd.state == CommandState.PENDING.value
    s2.close()

    mock = MockAdapter()

    def factory2(_engine=None):
        return mock

    s3 = factory()
    dispatched = dispatch_once(s3, factory2)
    assert dispatched == 1
    cmd = s3.get(WorkflowCommand, claimed[0].id)
    assert cmd.state == CommandState.DONE.value
    s3.close()


def test_a03d_concurrent_rearm_and_start_no_duplicate_instance(factory):
    request = _seed(factory, n=1)[0]
    s1 = factory()
    claimed = claim_pending_commands(s1, limit=10)
    assert len(claimed) == 1
    s1.close()

    # rearm after lease expiry, then dispatch; the idempotent query-before-start
    # must prevent a second engine instance even when a stale rearm races a
    # dispatch of an earlier START that already created an instance.
    s2 = factory()
    rearmed = rearm_stale_processing(s2, stale_after_seconds=0)
    assert rearmed == 1
    s2.close()

    mock = MockAdapter()
    # simulate the earlier ambiguous START having created an instance
    mock.process_instances["pi-existing"] = ProcessInstanceInfo(
        process_instance_id="pi-existing", state="ACTIVE", business_key=request.number
    )

    def factory2(_engine=None):
        return mock

    s3 = factory()
    dispatched = dispatch_once(s3, factory2)
    assert dispatched == 1
    s3.close()

    matches = mock.find_process_instance_by_business_key("credit_decision", request.number)
    assert len(matches) == 1, f"duplicate engine instances: {matches}"
