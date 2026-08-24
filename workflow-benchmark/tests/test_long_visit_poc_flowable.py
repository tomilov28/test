"""Flowable LONG_VISIT_POC reference-implementation tests (live stack).

Flowable-side mirror of the Operaton benchmark suite: covers scenarios
T01..T06, T08, T10, T11 against Flowable 8.0.0. T07 (full restart) and T09
(durable timer + engine restart) require process orchestration and live in
scripts/flowable_scenarios.py (F07/F09).

The logical contract is identical to Operaton: external prisoner-data load,
parallel human checks, parallel join, PT15S durable timer, final decision.
The only differences are vendor mechanics (Flowable external-job protocol,
execution-tree activity ids, REST shapes) handled inside FlowableAdapter.

Requirements:
  * Flowable reachable at ENGINE_FLOWABLE_URL (default localhost:8081)
  * FastAPI harness reachable at API_BASE (default localhost:8000)
  * PostgreSQL (shared) already migrated

These tests run only under the `integration` marker (make test-flowable).
"""

import os
import time
import uuid

import httpx
import pytest

from app.workers.flowable_worker import FlowableExternalTaskWorker
from app.workflow.flowable import FlowableAdapter

pytestmark = pytest.mark.integration

ENGINE_BASE = os.environ.get("ENGINE_FLOWABLE_URL", "http://localhost:8081/flowable-rest/service")
EXTERNAL_BASE = os.environ.get("ENGINE_FLOWABLE_URL", "http://localhost:8081/flowable-rest/external-job-api")
API_BASE = os.environ.get("BENCHMARK_API_URL", "http://localhost:8000")
FIXTURES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bpmn", "flowable")
V1_BPMN = os.path.join(FIXTURES_DIR, "long_visit_v1.bpmn")
V2_BPMN = os.path.join(FIXTURES_DIR, "long_visit_v2.bpmn")
PROCESS_KEY = "LONG_VISIT_POC"
ENGINE_NAME = "FLOWABLE"


def _engine_available() -> bool:
    # Retry briefly: right after a container recreate the Spring context can
    # still be warming up while Tomcat already answers the probe.
    deadline = time.time() + 20.0
    while time.time() < deadline:
        try:
            r = httpx.get(f"{ENGINE_BASE}/management/engine", auth=("rest-admin", "test"), timeout=3.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


pytestmark = [pytestmark, pytest.mark.skipif(not _engine_available(), reason="Flowable engine not reachable")]


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
    a = FlowableAdapter(base_url=ENGINE_BASE)
    yield a
    a.close()


@pytest.fixture(scope="module")
def env(adapter, api):
    """Clean LONG_VISIT_POC state and deploy v1 (-> version 1)."""
    _drop_process_key(adapter, PROCESS_KEY)
    info = adapter.deploy_process(_read(V1_BPMN), PROCESS_KEY, name="fixture-long_visit_v1")
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
    for instance_id in adapter.get_running_instances(process_key):
        adapter.cancel_process(instance_id)
    for definition in adapter.get_process_definitions(process_key):
        adapter.delete_deployment(definition["deploymentId"])


def create_request(api: httpx.Client, version: int = 1) -> dict:
    resp = api.post(
        "/requests",
        json={
            "request_type": PROCESS_KEY,
            "request_type_version": version,
            "workflow_engine": ENGINE_NAME,
            "variables": {"initiator": "pytest-flowable", "version": version},
        },
    )
    resp.raise_for_status()
    return resp.json()


def wait_instance(api: httpx.Client, request_id: uuid.UUID) -> str:
    def _get():
        req = api.get(f"/requests/{request_id}").json()
        return req["workflow_instance_id"]

    return _wait_for(_get, label="START_PROCESS dispatched (workflow_instance_id)")


def run_external_tasks(adapter, instance_id: str, max_attempts: int = 3) -> FlowableExternalTaskWorker:
    """Fetch+complete all pending prisoner-data external jobs for an instance."""
    worker = FlowableExternalTaskWorker(engine_url=ENGINE_BASE)
    try:
        for _ in range(max_attempts):
            worker.poll_once(instance_filter=instance_id)
            if worker.results["completed"]:
                return worker
            time.sleep(1.0)  # job may still be locked by a concurrent poller
        return worker
    finally:
        worker.close()


def wait_active_tasks(adapter, instance_id: str, expected: set[str], timeout: float = 30.0) -> set[str]:
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
    # v1 XML must NOT contain discipline_check (Flowable: resourcedata endpoint)
    xml_resp = adapter._client.get(
        f"{adapter._service}/repository/process-definitions/{definitions[0]['id']}/resourcedata"
    )
    xml_resp.raise_for_status()
    assert "discipline_check" not in xml_resp.text


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

    worker = FlowableExternalTaskWorker(engine_url=ENGINE_BASE, fail_first=True)
    try:
        worker.poll_once(instance_filter=instance_id)  # first invocation -> technical failure
        assert worker.results["failed"] == 1

        # engine-side: the failed external job carries the error + retries remaining
        # (Flowable external-job listing; NOT the dead-letter table while retries > 0)
        def _jobs():
            resp = adapter._client.get(
                f"{adapter._external}/jobs", params={"processInstanceId": instance_id, "size": 100}
            )
            resp.raise_for_status()
            return resp.json().get("data", [])

        jobs = _jobs()
        assert jobs, "no external job rows after a failed worker attempt"
        assert jobs[0]["exceptionMessage"] == "simulated technical failure"
        assert int(jobs[0]["retries"]) >= 1, "retries must remain so the job is re-acquirable"

        # Flowable's async executor re-activates the job after the retry timeout
        # (lock reset at the PT6S async-executor cadence -> ~30-35s wall clock)
        def _refetch():
            return worker.fetch_and_lock()

        job = _wait_for(
            lambda: next((t for t in _refetch() if t["processInstanceId"] == instance_id), None),
            timeout=70.0,
            interval=1.0,
            label="external job re-activated by retry",
        )
        worker.complete(job["id"], {"prisoner_exists": True, "unit": 3})  # next invocation -> success
    finally:
        worker.close()

    # process proceeded past the external job into the parallel stage
    wait_active_tasks(adapter, instance_id, {"finance_check", "relative_check"})


# ---- T04 Parallel human tasks + reconciler ---------------------------------

@pytest.fixture(scope="module")
def parallel_request(env):
    """A request parked at the parallel stage (external job done)."""
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
    jobs = adapter._client.get(
        f"{adapter._service}/management/timer-jobs",
        params={"processInstanceId": instance_id, "size": 100},
    ).json().get("data", [])
    assert jobs, "a timer job must be scheduled"
    assert jobs[0]["processInstanceId"] == instance_id


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
    adapter.deploy_process(_read(V2_BPMN), PROCESS_KEY, name="fixture-long_visit_v2")
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
        timeout=30.0,
        label="request CLOSED after cancel",
    )
    req = api.get(f"/requests/{request['id']}").json()
    assert req["outcome"] == "CANCELLED"
    assert req["closed_at"] is not None

    # active engine tasks disappeared
    assert adapter.get_process_instance(instance_id).state == "ENDED"
    assert adapter.get_active_human_tasks(instance_id) == []

    # local WorkItems CANCELLED
    items = work_items(api, request["id"])
    assert items and all(wi["state"] == "CANCELLED" for wi in items)

    # history preserved
    history = adapter.get_process_history(instance_id)
    assert history, "history must survive cancellation"
