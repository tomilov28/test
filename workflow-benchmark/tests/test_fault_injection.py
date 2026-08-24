"""Fault recovery + idempotency integration tests (benchmark phase 4).

Runs against the LIVE harness (FastAPI on :8000 with dispatcher + reconciler)
and a live engine, so it exercises the real outbox path including the
dispatcher's fault-injected adapter boundary.

Coverage:
  * T12 - lost START_PROCESS response -> exactly one engine instance per Request
  * T13 - lost COMPLETE_TASK response  -> retry detects "state already achieved"
  * T14 - lost CANCEL_PROCESS response -> retry terminates idempotently
  * T15 - exhausted technical retries  -> Request stays ACTIVE, no business
          outcome, engine failure storage recorded (incident/dead-letter)
  * INV - system invariants (one instance per request, one engine task per
          work item, completed/cancelled never ACTIVE again, version pinning,
          technical failure never a business outcome)

Requires the fault-injection admin surface (/admin/faults/*) on the harness.
"""

import os
import time

import httpx
import pytest

from app.workflow.flowable import DEFAULT_AUTH, FlowableAdapter
from app.workflow.operaton import OperatonAdapter
from app.workers.external_worker import ExternalTaskWorker
from app.workers.flowable_worker import FlowableExternalTaskWorker

pytestmark = pytest.mark.integration

API_BASE = os.environ.get("BENCH_API", "http://localhost:8000")
ENGINE_BASES = {
    "OPERATON": os.environ.get("ENGINE_OPERATON_URL", "http://localhost:8080/engine-rest"),
    "FLOWABLE": os.environ.get("ENGINE_FLOWABLE_URL", "http://localhost:8081/flowable-rest/service"),
}
PROCESS_KEY = "LONG_VISIT_POC"
TOPIC = "load_prisoner_data"

PARALLEL_TASKS = {"finance_check", "relative_check"}

PROBE_PATHS = {"OPERATON": "/engine", "FLOWABLE": "/management/engine"}


def _engine_up(engine: str) -> bool:
    deadline = time.time() + 15.0
    url = ENGINE_BASES[engine] + PROBE_PATHS[engine]
    kwargs = {"auth": DEFAULT_AUTH} if engine == "FLOWABLE" else {}
    while time.time() < deadline:
        try:
            r = httpx.get(url, timeout=3.0, **kwargs)
            if r.status_code < 500:
                return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


@pytest.fixture(scope="module")
def api() -> httpx.Client:
    client = httpx.Client(base_url=API_BASE, timeout=20.0)
    yield client
    client.close()


def _adapter(engine: str):
    if engine == "OPERATON":
        return OperatonAdapter(base_url=ENGINE_BASES["OPERATON"])
    return FlowableAdapter(base_url=ENGINE_BASES["FLOWABLE"])


# -- harness control ----------------------------------------------------------


def arm_fault(api: httpx.Client, engine: str, operation: str, mode: str, remaining: int = -1) -> dict:
    resp = api.post(
        "/admin/faults/arm",
        json={"engine": engine, "operation": operation, "mode": mode, "remaining": remaining},
    )
    resp.raise_for_status()
    return resp.json()


def clear_faults(api: httpx.Client, engine: str | None = None) -> dict:
    resp = api.post("/admin/faults/clear", json={"engine": engine})
    resp.raise_for_status()
    return resp.json()


# -- flow helpers --------------------------------------------------------------


def create_request(api: httpx.Client, engine: str, version: int = 1, tag: str = "fault") -> dict:
    resp = api.post(
        "/requests",
        json={
            "request_type": PROCESS_KEY,
            "request_type_version": version,
            "workflow_engine": engine,
            "variables": {"initiator": f"{engine.lower()}-fault-{tag}"},
        },
    )
    resp.raise_for_status()
    return resp.json()


def wait_for(predicate, timeout: float, interval: float = 1.0, label: str = "condition"):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except Exception as exc:
            last = exc
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {label} (last={last!r})")


def wait_instance(api: httpx.Client, request_id: str) -> str:
    return wait_for(
        lambda: api.get(f"/requests/{request_id}").json().get("workflow_instance_id"),
        timeout=30.0,
        label="START_PROCESS dispatched",
    )


def run_external_task(engine: str, instance_id: str) -> None:
    worker = (
        ExternalTaskWorker(engine_url=ENGINE_BASES["OPERATON"])
        if engine == "OPERATON"
        else FlowableExternalTaskWorker(engine_url=ENGINE_BASES["FLOWABLE"])
    )
    try:
        for _ in range(15):
            worker.poll_once(instance_filter=instance_id)
            if worker.results["completed"]:
                return
            time.sleep(1.0)
        raise AssertionError(f"external task never completed for {instance_id}: {worker.results}")
    finally:
        worker.close()


def wait_parallel(adapter, instance_id: str) -> None:
    wait_for(
        lambda: adapter.get_active_activity_ids(instance_id) == PARALLEL_TASKS,
        timeout=30.0,
        label="parallel tasks active",
    )


def wait_activity(adapter, instance_id: str, activity: str, present: bool = True, timeout: float = 60.0):
    wait_for(
        lambda: (activity in adapter.get_active_activity_ids(instance_id)) == present,
        timeout=timeout,
        label=f"activity {activity} present={present}",
    )


def complete_work_item(api: httpx.Client, request_id: str, task_key: str, data: dict) -> dict:
    items = api.get(f"/requests/{request_id}/work-items").json()
    target = next(wi for wi in items if wi["task_definition_key"] == task_key and wi["state"] == "ACTIVE")
    resp = api.post(f"/work-items/{target['id']}/complete", json={"data": data, "version": 1})
    resp.raise_for_status()
    return resp.json()


def wait_command_state(api: httpx.Client, request_id: str, command_type: str, state: str) -> dict:
    def _match():
        for c in api.get(f"/requests/{request_id}/commands").json():
            if c["command_type"] == command_type:
                return c if c["state"] == state else None
        return None

    return wait_for(_match, timeout=60.0, label=f"command {command_type} state={state}")


def command_list(api: httpx.Client, request_id: str) -> list[dict]:
    return api.get(f"/requests/{request_id}/commands").json()


def assert_no_failed_commands(api: httpx.Client, request_id: str) -> None:
    failed = [c for c in command_list(api, request_id) if c["state"] == "FAILED"]
    assert not failed, f"unexpected FAILED commands: {failed}"


def reconcile(api: httpx.Client) -> dict:
    resp = api.post("/admin/reconcile")
    resp.raise_for_status()
    return resp.json()


@pytest.fixture(autouse=True)
def _clean_topic(api):
    """Drain leftover external jobs before each test.

    Operaton/Flowable external-task acquisition returns a fixed-size batch
    (oldest first); jobs left pending by an earlier test can starve a target
    instance's job out of the batch, so each test starts with a clean topic.
    """
    yield
    for engine in ENGINES:
        worker = (
            ExternalTaskWorker(engine_url=ENGINE_BASES["OPERATON"])
            if engine == "OPERATON"
            else FlowableExternalTaskWorker(engine_url=ENGINE_BASES["FLOWABLE"])
        )
        try:
            deadline = time.time() + 60.0
            idle = 0
            while time.time() < deadline:
                before = worker.results["fetched"]
                worker.poll_once()
                if worker.results["fetched"] == before:
                    idle += 1
                    if idle >= 3:
                        break
                else:
                    idle = 0
                time.sleep(1.0)
        finally:
            worker.close()


# -- tests ---------------------------------------------------------------------


def test_t12_lost_start_response_single_instance(api, engine):
    """A hidden START_PROCESS response must not create a second engine instance."""
    try:
        clear_faults(api, engine)
        # Arm a loss that NEVER expires: if the retry path called start a
        # second time it would keep hiding successes and spawn duplicates.
        arm_fault(api, engine, "start_process", "loss", remaining=100)
        for i in range(2):
            request = create_request(api, engine, tag=f"t12-{i}")
            request_id = request["id"]
            instance_id = wait_instance(api, request_id)
            cmd = wait_command_state(api, request_id, "START_PROCESS", "DONE")

            assert cmd["attempts"] >= 2, f"expected a retry after lost response: {cmd}"
            adapter = _adapter(engine)
            try:
                matches = adapter.find_process_instance_by_business_key(PROCESS_KEY, request["number"])
            finally:
                adapter.close()
            assert len(matches) == 1, f"expected exactly one instance for {request['number']}: {matches}"
            assert matches[0].process_instance_id == instance_id

            # the hidden attempt must have been recorded exactly once
            status = api.get("/admin/faults").json()
            op = status["operations"].get(engine, {}).get("start_process", {})
            assert op.get("injected", 0) == i + 1, op

            req = api.get(f"/requests/{request_id}").json()
            assert req["workflow_instance_id"] == instance_id
            assert_no_failed_commands(api, request_id)
    finally:
        clear_faults(api, engine)


def test_t13_lost_complete_response_state_already_achieved(api, engine):
    """A hidden COMPLETE_TASK response must be treated as success on retry."""
    try:
        clear_faults(api, engine)
        request = create_request(api, engine, tag="t13")
        request_id = request["id"]
        instance_id = wait_instance(api, request_id)
        run_external_task(engine, instance_id)
        adapter = _adapter(engine)
        try:
            wait_parallel(adapter, instance_id)
        finally:
            adapter.close()
        reconcile(api)
        # Arm the loss BEFORE completing, so the COMPLETE_TASK command's first
        # engine call is the one whose success gets hidden.
        arm_fault(api, engine, "complete_human_task", "loss", remaining=1)
        complete_work_item(api, request_id, "finance_check", {"result": "OK", "balance_sufficient": True})
        cmd = wait_command_state(api, request_id, "COMPLETE_TASK", "DONE")
        clear_faults(api, engine)

        assert cmd["attempts"] >= 2, f"expected retry after lost COMPLETE_TASK response: {cmd}"
        assert_no_failed_commands(api, request_id)

        # requested state achieved: engine task gone, process on the correct branch
        adapter = _adapter(engine)
        try:
            items = api.get(f"/requests/{request_id}/work-items").json()
            task_id = next(
                wi["external_task_id"] for wi in items if wi["task_definition_key"] == "finance_check"
            )
            assert adapter.get_human_task(task_id) is None
            active = adapter.get_active_activity_ids(instance_id)
            assert "finance_check" not in active, active
            assert "relative_check" in active, active

            # complete the remaining branch -> no breakage, correct outcome
            complete_work_item(api, request_id, "relative_check", {"result": "OK", "relatives_verified": True})
            wait_activity(adapter, instance_id, "final_decision", present=True, timeout=60.0)
        finally:
            adapter.close()
        reconcile(api)
        complete_work_item(api, request_id, "final_decision", {"decision": "APPROVE"})
        wait_for(
            lambda: api.get(f"/requests/{request_id}").json().get("lifecycle_state") == "CLOSED",
            timeout=40.0,
            label="request CLOSED",
        )
        req = api.get(f"/requests/{request_id}").json()
        assert req["outcome"] == "COMPLETED"
        assert_no_failed_commands(api, request_id)
    finally:
        clear_faults(api, engine)


def test_t14_lost_cancel_response_idempotent_termination(api, engine):
    """A hidden CANCEL_PROCESS response must terminate idempotently on retry."""
    try:
        clear_faults(api, engine)
        request = create_request(api, engine, tag="t14")
        request_id = request["id"]
        instance_id = wait_instance(api, request_id)
        run_external_task(engine, instance_id)
        adapter = _adapter(engine)
        try:
            wait_parallel(adapter, instance_id)
        finally:
            adapter.close()
        reconcile(api)

        # Arm the loss BEFORE cancelling so the CANCEL command's first engine
        # call is the one whose success gets hidden.
        arm_fault(api, engine, "cancel_process", "loss", remaining=1)
        resp = api.post(f"/requests/{request_id}/cancel", json={"reason": "t14 lost cancel response"})
        resp.raise_for_status()
        cmd = wait_command_state(api, request_id, "CANCEL_PROCESS", "DONE")
        clear_faults(api, engine)

        assert cmd["attempts"] >= 2, f"expected retry after lost CANCEL_PROCESS response: {cmd}"
        req = api.get(f"/requests/{request_id}").json()
        assert req["lifecycle_state"] == "CLOSED", req
        assert req["outcome"] == "CANCELLED", req

        adapter = _adapter(engine)
        try:
            state = adapter.get_process_instance(instance_id).state
            assert state == "ENDED", state
        finally:
            adapter.close()
        items = api.get(f"/requests/{request_id}/work-items").json()
        assert all(wi["state"] == "CANCELLED" for wi in items), items
        assert_no_failed_commands(api, request_id)
    finally:
        clear_faults(api, engine)


def test_t15_exhausted_retries_request_stays_active(api, engine):
    """Exhausting outbox technical retries must NOT set a business outcome."""
    try:
        clear_faults(api, engine)
        request = create_request(api, engine, tag="t15")
        request_id = request["id"]
        instance_id = wait_instance(api, request_id)
        run_external_task(engine, instance_id)
        adapter = _adapter(engine)
        try:
            wait_parallel(adapter, instance_id)
        finally:
            adapter.close()
        reconcile(api)
        # Persistent technical failure: every COMPLETE_TASK dispatch fails.
        arm_fault(api, engine, "complete_human_task", "fail", remaining=-1)
        complete_work_item(api, request_id, "finance_check", {"result": "OK", "balance_sufficient": True})
        cmd = wait_command_state(api, request_id, "COMPLETE_TASK", "FAILED")
        clear_faults(api, engine)

        assert cmd["state"] == "FAILED"
        assert cmd["attempts"] >= 5, f"expected max attempts exhausted: {cmd}"
        assert "connect" in (cmd["last_error"] or "").lower() or "error" in (cmd["last_error"] or "").lower()

        req = api.get(f"/requests/{request_id}").json()
        assert req["lifecycle_state"] == "ACTIVE", "technical failure must not close the request"
        assert req["outcome"] is None, "technical failure must not set a business outcome"
        # engine never saw the completion -> task still present (technical, not business, state)
        adapter = _adapter(engine)
        try:
            items = api.get(f"/requests/{request_id}/work-items").json()
            task_id = next(
                wi["external_task_id"] for wi in items if wi["task_definition_key"] == "finance_check"
            )
            task = adapter.get_human_task(task_id)
        finally:
            adapter.close()
        assert task is not None, "engine task must still be open (completion never reached the engine)"
    finally:
        clear_faults(api, engine)


def test_t15_engine_failure_storage_not_business_outcome(api, engine):
    """Engine-side technical failure (external task/job) is recorded by the
    engine, retried via its own path, and never turns into a business outcome."""
    try:
        clear_faults(api, engine)
        request = create_request(api, engine, tag="t15-engine")
        request_id = request["id"]
        instance_id = wait_instance(api, request_id)

        # Make the external worker report a terminal technical failure.
        worker = (
            ExternalTaskWorker(engine_url=ENGINE_BASES["OPERATON"])
            if engine == "OPERATON"
            else FlowableExternalTaskWorker(engine_url=ENGINE_BASES["FLOWABLE"])
        )
        try:
            failed_task = None
            for _ in range(15):
                tasks = worker.fetch_and_lock()
                candidate = next(
                    (t for t in tasks if t.get("processInstanceId") == instance_id), None
                )
                if candidate is None:
                    time.sleep(1.0)
                    continue
                worker.fail(candidate["id"], "simulated terminal external failure", retries=0)
                failed_task = candidate["id"]
                break
            assert failed_task, f"external task never acquired for {instance_id}"
        finally:
            worker.close()

        # The engine stores the failure: failed job / incident / dead-letter.
        adapter = _adapter(engine)
        try:
            failed = adapter.get_failed_jobs(instance_id)
            assert failed, f"engine must record the failed job for {instance_id}"
        finally:
            adapter.close()

        req = api.get(f"/requests/{request_id}").json()
        assert req["lifecycle_state"] == "ACTIVE", "engine failure must not close the request"
        assert req["outcome"] is None, "engine failure must not set a business outcome"
    finally:
        clear_faults(api, engine)


def test_invariants_single_instance_and_no_revert(api, engine):
    """Cross-cutting invariants over several recovered requests."""
    try:
        clear_faults(api, engine)
        created: list[dict] = []
        for tag in ("inv-a", "inv-b"):
            request = create_request(api, engine, tag=tag)
            request_id = request["id"]
            wait_instance(api, request_id)
            created.append(api.get(f"/requests/{request_id}").json())

        adapter = _adapter(engine)
        try:
            for req in created:
                # one Request -> exactly one engine instance ever (active+historic)
                matches = adapter.find_process_instance_by_business_key(PROCESS_KEY, req["number"])
                assert len(matches) == 1, (req["number"], matches)
                instance_id = matches[0].process_instance_id
                assert req["workflow_instance_id"] == instance_id
                # version pinning: instance runs the requested fixture version
                ver = adapter.get_instance_definition_version(instance_id)
                assert ver == req["request_type_version"], (ver, req["request_type_version"])

            # one engine task -> at most one WorkItem (no duplicate upserts)
            for req in created:
                items = api.get(f"/requests/{req['id']}/work-items").json()
                ids = [wi["external_task_id"] for wi in items if wi["external_task_id"]]
                assert len(ids) == len(set(ids)), f"duplicate WorkItems for {req['id']}"
        finally:
            adapter.close()

        # no command may be FAILED (everything above used the happy path)
        for req in created:
            assert_no_failed_commands(api, req["id"])
    finally:
        clear_faults(api, engine)


ENGINES = [e for e in ("OPERATON", "FLOWABLE") if _engine_up(e)]


@pytest.fixture(params=ENGINES, scope="module")
def engine(request):
    return request.param
