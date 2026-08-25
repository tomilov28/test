"""Operaton LONG_VISIT_POC reference-implementation tests (live stack).

Covers benchmark scenarios T01..T06, T08, T10, T11 of the Operaton phase.
T07 (full restart) and T09 (durable timer + engine restart) require process
orchestration and live in scripts/operaton_scenarios.py.

Requirements:
  * Operaton reachable at ENGINE_OPERATON_URL (default localhost:8080)
  * FastAPI harness reachable at API_BASE (default localhost:8000)
  * PostgreSQL (shared) already migrated

These tests run only under the `integration` marker (make test-operaton).
"""

import os
import time
import uuid

import httpx
import pytest

from app.workers.external_worker import ExternalTaskWorker
from app.workflow.operaton import OperatonAdapter

from tests.conftest import require_engine_fixture

pytestmark = pytest.mark.integration

ENGINE_BASE = os.environ.get("ENGINE_OPERATON_URL", "http://localhost:8080/engine-rest")
API_BASE = os.environ.get("BENCHMARK_API_URL", "http://localhost:8000")
FIXTURES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bpmn", "operaton")
V1_BPMN = os.path.join(FIXTURES_DIR, "long_visit_poc.bpmn")
V2_BPMN = os.path.join(FIXTURES_DIR, "long_visit_poc_v2.bpmn")
PROCESS_KEY = "LONG_VISIT_POC"
ENGINE_NAME = "OPERATON"

require_engine_fixture("Operaton", f"{ENGINE_BASE}/engine")


def _wait_for(predicate, timeout: float = 20.0, interval: float = 0.5, label: str = "condition"):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except Exception as exc:  # engine transient errors during wait
            last = exc
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {label} (last={last!r})")


def _read(xml_path: str) -> str:
    with open(xml_path) as fh:
        return fh.read()


@pytest.fixture(scope="module")
def api():
    client = httpx.Client(base_url=API_BASE, timeout=20.0)
    yield client
    client.close()


@pytest.fixture(scope="module")
def adapter():
    a = OperatonAdapter(base_url=ENGINE_BASE)
    yield a
    a.close()


@pytest.fixture(scope="module")
def env(adapter, api):
    """Clean LONG_VISIT_POC state and deploy v1 (-> version 1)."""
    _drop_process_key(adapter, PROCESS_KEY)
    # deploy v1 fresh -> version 1
    info = adapter.deploy_process(_read(V1_BPMN), PROCESS_KEY, name="fixture-long_visit_poc_v1")
    definitions = {d["version"]: d for d in adapter.get_process_definitions(PROCESS_KEY)}
    assert 1 in definitions, "v1 fixture should deploy as version 1"
    env_obj = {
        "api": api,
        "adapter": adapter,
        "deployment_id": info.deployment_id,
    }
    yield env_obj
    # module teardown: leave running instances alone; drop LONG_VISIT_POC deployments
    _drop_process_key(adapter, PROCESS_KEY)


def _drop_process_key(adapter, process_key: str) -> None:
    """Cancel running instances and delete all deployments for a process key."""
    for inst in adapter._client.get("/process-instance", params={"processDefinitionKey": process_key}).json():
        adapter.cancel_process(inst["id"])
    for definition in adapter.get_process_definitions(process_key):
        dep_id = definition["deploymentId"]
        adapter._client.delete(f"/deployment/{dep_id}", params={"cascade": "true"})


def create_request(api: httpx.Client, version: int = 1) -> dict:
    resp = api.post(
        "/requests",
        json={
            "request_type": PROCESS_KEY,
            "request_type_version": version,
            "workflow_engine": ENGINE_NAME,
            "variables": {"initiator": "pytest", "version": version},
        },
    )
    resp.raise_for_status()
    return resp.json()


def wait_instance(api: httpx.Client, request_id: uuid.UUID) -> str:
    def _get():
        req = api.get(f"/requests/{request_id}").json()
        return req["workflow_instance_id"]

    return _wait_for(_get, label="START_PROCESS dispatched (workflow_instance_id)")


def run_external_tasks(adapter, instance_id: str, max_attempts: int = 3) -> ExternalTaskWorker:
    """Fetch+complete all pending prisoner-data external tasks for an instance."""
    worker = ExternalTaskWorker(engine_url=ENGINE_BASE)
    try:
        for _ in range(max_attempts):
            worker.poll_once(instance_filter=instance_id)
            if worker.results["completed"] or worker.results["failed"]:
                return worker
            time.sleep(1.0)  # task may still be locked by a concurrent poller
        return worker
    finally:
        worker.close()


def wait_active_tasks(adapter, instance_id: str, expected: set[str], timeout: float = 20.0) -> set[str]:
    def _get():
        active = adapter.get_active_activity_ids(instance_id)
        return active if active == expected else False

    try:
        return _wait_for(_get, timeout=timeout, label=f"active activities == {expected}")
    except AssertionError:
        print(f"\n[debug] expected={expected} actual={adapter.get_active_activity_ids(instance_id)}")
        raise


def reconcile(api: httpx.Client) -> dict:
    resp = api.post("/admin/reconcile")
    resp.raise_for_status()
    return resp.json()


def work_items(api: httpx.Client, request_id: uuid.UUID) -> list[dict]:
    resp = api.get(f"/requests/{request_id}/work-items")
    resp.raise_for_status()
    return resp.json()


# ---- T01 Deploy -------------------------------------------------------------

def test_t01_deploy_v1_is_version_1(env):
    adapter = env["adapter"]
    definitions = sorted(adapter.get_process_definitions(PROCESS_KEY), key=lambda d: d["version"])
    assert definitions, "no definitions for LONG_VISIT_POC"
    assert definitions[0]["version"] == 1
    # v1 XML must NOT contain discipline_check
    xml_resp = adapter._client.get(f"/process-definition/{definitions[0]['id']}/xml")
    xml_resp.raise_for_status()
    assert "discipline_check" not in xml_resp.json()["bpmn20Xml"]


# ---- T02 Start --------------------------------------------------------------

def test_t02_start_outbox_creates_instance(env):
    api, adapter = env["api"], env["adapter"]
    request = create_request(api, version=1)
    instance_id = wait_instance(api, request["id"])
    info = adapter.get_process_instance(instance_id)
    assert info.state == "ACTIVE"
    assert info.business_key == request["number"], "businessKey should carry the Request number"


# ---- T03 External worker retry ---------------------------------------------

def test_t03_external_worker_real_retry(env):
    api, adapter = env["api"], env["adapter"]
    request = create_request(api, version=1)
    instance_id = wait_instance(api, request["id"])

    worker = ExternalTaskWorker(engine_url=ENGINE_BASE, fail_first=True)
    try:
        worker.poll_once(instance_filter=instance_id)  # first invocation -> technical failure
        assert worker.results["failed"] == 1

        # engine-side: task reported failure with retries remaining (no incident)
        failed = adapter._client.get("/external-task", params={"processInstanceId": instance_id}).json()
        assert failed and failed[0]["errorMessage"] == "simulated technical failure"
        assert failed[0]["retries"] >= 1, "retries must remain so the task is re-fetchable"

        # Operaton's retry mechanism re-activates the task after retryTimeout
        def _refetch():
            return worker.fetch_and_lock()

        task = _wait_for(
            lambda: next((t for t in _refetch() if t["processInstanceId"] == instance_id), None),
            timeout=20.0,
            interval=0.5,
            label="external task re-activated by retry job",
        )
        worker.complete(task["id"], {"prisoner_exists": True, "unit": 3})  # next invocation -> success
    finally:
        worker.close()

    # process proceeded past the external task
    try:
        wait_active_tasks(adapter, instance_id, {"finance_check", "relative_check"})
    except AssertionError:
        ext = adapter._client.get("/external-task", params={"processInstanceId": instance_id}).json()
        print(f"\n[debug] external tasks now: {[(t['id'], t.get('retries'), t.get('errorMessage')) for t in ext]}")
        act_hist = adapter._client.get(
            "/history/activity-instance", params={"processInstanceId": instance_id}
        ).json()
        print(
            "[debug] history activities: "
            f"{[(a.get('activityId'), a.get('activityType'), a.get('endTime') is not None) for a in act_hist]}"
        )
        raise


# ---- T04 Parallel human tasks + reconciler ---------------------------------

@pytest.fixture(scope="module")
def parallel_request(env):
    """A request parked at the parallel stage (external task done)."""
    api, adapter = env["api"], env["adapter"]
    request = create_request(api, version=1)
    instance_id = wait_instance(api, request["id"])
    run_external_tasks(adapter, instance_id)
    wait_active_tasks(adapter, instance_id, {"finance_check", "relative_check"})
    reconcile(api)
    enriched = api.get(f"/requests/{request['id']}").json()
    return {"request": enriched, "instance_id": instance_id}


def test_t04_parallel_tasks_two_work_items(env, parallel_request):
    api, adapter = env["api"], env["adapter"]
    request = parallel_request["request"]
    tasks = adapter.get_active_human_tasks(request["workflow_instance_id"])
    keys = sorted(t.task_definition_key for t in tasks)
    assert keys == ["finance_check", "relative_check"], f"expected exactly 2 parallel tasks, got {keys}"
    items = work_items(api, request["id"])
    assert len(items) == 2
    assert sorted(wi["task_definition_key"] for wi in items) == ["finance_check", "relative_check"]


def test_t05_idempotent_reconciliation(env, parallel_request):
    api = env["api"]
    request = parallel_request["request"]
    before = len(work_items(api, request["id"]))
    assert before == 2
    for _ in range(5):
        summary = reconcile(api)
        assert summary["errors"] == []
    after = len(work_items(api, request["id"]))
    assert after == before, f"reconciliation must be idempotent: {before} -> {after}"


def test_t06_complete_one_branch(env, parallel_request):
    api, adapter = env["api"], env["adapter"]
    request = parallel_request["request"]
    instance_id = request["workflow_instance_id"]

    items = work_items(api, request["id"])
    finance = next(wi for wi in items if wi["task_definition_key"] == "finance_check")
    resp = api.post(f"/work-items/{finance['id']}/complete", json={"data": {"result": "OK"}, "version": 1})
    resp.raise_for_status()

    # local TaskResult persisted + WorkItem completed
    updated = work_items(api, request["id"])
    done = next(wi for wi in updated if wi["id"] == finance["id"])
    assert done["state"] == "COMPLETED"
    assert done["results"], "TaskResult must be saved"
    assert done["results"][0]["data"] == {"result": "OK"}

    # engine User Task completed; relative_check still active; join not passed
    active = adapter.get_active_activity_ids(instance_id)
    assert "relative_check" in active
    assert "final_decision" not in active
    assert adapter.get_process_instance(instance_id).state == "ACTIVE"


# ---- T08 Parallel join + timer ---------------------------------------------

def test_t08_join_then_timer(env, parallel_request):
    api, adapter = env["api"], env["adapter"]
    request = parallel_request["request"]
    instance_id = request["workflow_instance_id"]

    items = work_items(api, request["id"])
    relative = next(wi for wi in items if wi["task_definition_key"] == "relative_check")
    resp = api.post(f"/work-items/{relative['id']}/complete", json={"data": {"result": "OK"}, "version": 1})
    resp.raise_for_status()

    # join consumed both branches -> timer wait state, no human tasks
    wait_active_tasks(adapter, instance_id, {"Timer_1"})
    jobs = adapter._client.get("/job", params={"processInstanceId": instance_id}).json()
    assert jobs, "a timer job must be scheduled"
    definitions = adapter._client.get(
        "/job-definition", params={"processDefinitionKey": PROCESS_KEY}
    ).json()
    timer_def = next((d for d in definitions if d.get("activityId") == "Timer_1"), None)
    assert timer_def, "a timer job definition for Timer_1 must exist"
    assert "timer" in timer_def.get("jobType", ""), f"unexpected jobType: {timer_def.get('jobType')}"


# ---- T10 Versioning ---------------------------------------------------------

def test_t10_versioning_old_v1_new_v2(env):
    api, adapter = env["api"], env["adapter"]

    # instance A on v1, parked at parallel stage
    request_a = create_request(api, version=1)
    instance_a = wait_instance(api, request_a["id"])
    run_external_tasks(adapter, instance_a)
    wait_active_tasks(adapter, instance_a, {"finance_check", "relative_check"})
    assert adapter.get_instance_definition_version(instance_a) == 1

    # deploy v2 while v1 instance is active
    adapter.deploy_process(_read(V2_BPMN), PROCESS_KEY, name="fixture-long_visit_poc_v2")
    versions = {d["version"] for d in adapter.get_process_definitions(PROCESS_KEY)}
    assert 2 in versions

    # instance B on the new latest version
    request_b = create_request(api, version=2)
    instance_b = wait_instance(api, request_b["id"])
    assert adapter.get_instance_definition_version(instance_b) == 2
    run_external_tasks(adapter, instance_b)
    wait_active_tasks(adapter, instance_b, {"finance_check", "relative_check", "discipline_check"})

    # old instance unchanged: no discipline_check
    active_a = adapter.get_active_activity_ids(instance_a)
    assert "discipline_check" not in active_a
    assert {"finance_check", "relative_check"} <= active_a


# ---- T11 Cancellation -------------------------------------------------------

def test_t11_cancellation_marks_everything(env):
    api, adapter = env["api"], env["adapter"]
    request = create_request(api, version=1)
    instance_id = wait_instance(api, request["id"])
    run_external_tasks(adapter, instance_id)
    wait_active_tasks(adapter, instance_id, {"finance_check", "relative_check"})
    reconcile(api)
    items = work_items(api, request["id"])
    assert len(items) == 2

    resp = api.post(f"/requests/{request['id']}/cancel", json={"reason": "T11 cancellation test"})
    resp.raise_for_status()

    # outbox executes engine cancellation -> request CLOSED/CANCELLED
    _wait_for(
        lambda: api.get(f"/requests/{request['id']}").json().get("lifecycle_state") == "CLOSED",
        label="request CLOSED after cancel",
    )
    req = api.get(f"/requests/{request['id']}").json()
    assert req["outcome"] == "CANCELLED"
    assert req["closed_at"] is not None

    # domain-first (A01): the request is CLOSED immediately, but the engine
    # termination is a background command -- wait for it to converge.
    _wait_for(
        lambda: adapter.get_process_instance(instance_id).state == "ENDED",
        label="engine instance ENDED after cancel",
    )
    _wait_for(
        lambda: adapter.get_active_human_tasks(instance_id) == [],
        label="engine human tasks cleared after cancel",
    )

    # local WorkItems CANCELLED
    items = work_items(api, request["id"])
    assert items and all(wi["state"] == "CANCELLED" for wi in items)

    # history preserved
    history = adapter.get_process_history(instance_id)
    assert history, "history must survive cancellation"
    hist_resp = adapter._client.get(f"/history/process-instance/{instance_id}")
    assert hist_resp.status_code == 200
