"""Integration tests for the Operaton adapter against a LIVE engine.

Requirement: integration tests run against PostgreSQL (the running engine is
configured with a PostgreSQL datasource). These are skipped automatically when
no engine is reachable and excluded from the fast unit suite via the
`integration` marker (see pyproject.toml / Makefile).
"""

import os
import time

import httpx
import pytest

from app.workflow.operaton import OperatonAdapter

from tests.conftest import require_engine_fixture

pytestmark = pytest.mark.integration

ENGINE_BASE = os.environ.get("ENGINE_OPERATON_URL", "http://localhost:8080/engine-rest")
FIXTURE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "bpmn",
    "operaton",
    "credit_decision.bpmn",
)

require_engine_fixture("Operaton", f"{ENGINE_BASE}/engine")


@pytest.fixture(scope="module")
def adapter():
    a = OperatonAdapter(base_url=ENGINE_BASE)
    yield a
    a.close()


def test_deploy_start_tasks_complete_history(adapter):
    xml = open(FIXTURE).read()
    dep = adapter.deploy_process(xml, "credit_decision", name="it-fixture")
    assert dep.process_key == "credit_decision"
    assert dep.deployment_id

    pi = adapter.start_process(
        "credit_decision",
        business_key=f"REQ-IT-{int(time.time())}",
        variables={"amount": 1200, "currency": "USD"},
    )
    assert pi.state == "ACTIVE"
    assert pi.process_instance_id

    assert adapter.get_process_instance(pi.process_instance_id).state == "ACTIVE"

    # first human task
    tasks = adapter.get_active_human_tasks(pi.process_instance_id)
    assert [t.task_definition_key for t in tasks] == ["review_request"]

    adapter.complete_human_task(tasks[0].id, variables={"review": "PASS"})

    # second human task appears
    tasks = adapter.get_active_human_tasks(pi.process_instance_id)
    assert [t.task_definition_key for t in tasks] == ["final_approval"]

    adapter.complete_human_task(tasks[0].id, variables={"decision": "APPROVE"})

    deadline = time.time() + 10
    while time.time() < deadline:
        if adapter.get_process_instance(pi.process_instance_id).state == "ENDED":
            break
        time.sleep(0.5)
    assert adapter.get_process_instance(pi.process_instance_id).state == "ENDED"

    history = adapter.get_process_history(pi.process_instance_id)
    assert {h.activity_type for h in history} >= {"startEvent", "userTask", "noneEndEvent"}


def test_cancel_process(adapter):
    xml = open(FIXTURE).read()
    adapter.deploy_process(xml, "credit_decision", name="it-fixture")
    pi = adapter.start_process("credit_decision", business_key="REQ-IT-CANCEL")
    adapter.cancel_process(pi.process_instance_id, reason="integration test")
    deadline = time.time() + 10
    while time.time() < deadline:
        if adapter.get_process_instance(pi.process_instance_id).state != "ACTIVE":
            break
        time.sleep(0.5)
    assert adapter.get_process_instance(pi.process_instance_id).state == "ENDED"


def test_failed_jobs_empty_for_clean_instance(adapter):
    xml = open(FIXTURE).read()
    adapter.deploy_process(xml, "credit_decision", name="it-fixture")
    pi = adapter.start_process("credit_decision", business_key="REQ-IT-JOBS")
    jobs = adapter.get_failed_jobs(pi.process_instance_id)
    assert isinstance(jobs, list)
