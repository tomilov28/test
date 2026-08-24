"""Operaton durability scenarios T07 and T09 (live stack orchestration).

T07 - full stack restart, shared DB preserved:
    * start + partially progress a LONG_VISIT_POC request (one of two parallel
      human tasks completed through our API)
    * restart Operaton AND the FastAPI harness (shared Postgres untouched)
    * verify engine + local state survive, reconciler re-syncs, then run the
      remaining branch to completion (timer -> final_decision -> request CLOSED)

T09 - durable timer across an engine restart:
    * progress a request to the parallel join -> timer wait state (PT15S)
    * restart ONLY Operaton while the timer is pending
    * verify the durable timer job fires after restart, final_decision becomes
      active and the reconciler surfaces it as a WorkItem; completing it closes
      the request

Both scenarios write JSON evidence under artifacts/operaton/api-evidence/.

Usage:
    .venv/bin/python -m scripts.operaton_scenarios --scenario t07
    .venv/bin/python -m scripts.operaton_scenarios --scenario t09
    .venv/bin/python -m scripts.operaton_scenarios --scenario all
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import uuid

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.workflow.operaton import OperatonAdapter  # noqa: E402
from app.workers.external_worker import ExternalTaskWorker  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARTIFACTS = os.path.join(ROOT, "artifacts", "operaton")
EVIDENCE_DIR = os.path.join(ARTIFACTS, "api-evidence")
APP_PIDFILE = os.path.join(ARTIFACTS, "app.pid")
APP_LOG = os.path.join(ARTIFACTS, "app.log")
VENV_PY = os.path.join(ROOT, ".venv", "bin", "python")
COMPOSE_OPERATON = (
    f"docker compose -f {ROOT}/docker-compose.yml -f {ROOT}/docker-compose.operaton.yml"
)
WAIT_ENGINE = [VENV_PY, os.path.join(ROOT, "scripts", "wait_for_engine.py"), "--engine", "OPERATON"]

API_BASE = "http://localhost:8000"
ENGINE_BASE = "http://localhost:8080/engine-rest"
PROCESS_KEY = "LONG_VISIT_POC"
TOPIC = "load_prisoner_data"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


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


def engine_up() -> bool:
    try:
        return httpx.get(f"{ENGINE_BASE}/engine", timeout=3.0).status_code == 200
    except Exception:
        return False


def app_up() -> bool:
    try:
        return httpx.get(f"{API_BASE}/health", timeout=3.0).status_code == 200
    except Exception:
        return False


def create_request(api: httpx.Client, version: int = 1) -> dict:
    resp = api.post(
        "/requests",
        json={
            "request_type": PROCESS_KEY,
            "request_type_version": version,
            "workflow_engine": "OPERATON",
            "variables": {"initiator": "operaton-scenario"},
        },
    )
    resp.raise_for_status()
    return resp.json()


def wait_instance(api: httpx.Client, request_id: str) -> str:
    return wait_for(
        lambda: api.get(f"/requests/{request_id}").json().get("workflow_instance_id"),
        timeout=30.0,
        label="START_PROCESS dispatched",
    )


def run_external_tasks(adapter: OperatonAdapter, instance_id: str) -> None:
    worker = ExternalTaskWorker(engine_url=ENGINE_BASE)
    try:
        for _ in range(5):
            worker.poll_once(instance_filter=instance_id)
            if worker.results["completed"]:
                log(f"external task completed: {worker.results}")
                return
            time.sleep(1.0)
        raise AssertionError(f"external task never completed: {worker.results}")
    finally:
        worker.close()


def wait_parallel(adapter: OperatonAdapter, instance_id: str) -> None:
    wait_for(
        lambda: adapter.get_active_activity_ids(instance_id) == {"finance_check", "relative_check"},
        timeout=20.0,
        label="parallel tasks active",
    )


def _finance_done(adapter: OperatonAdapter, instance_id: str) -> bool:
    """finance_check completed, relative_check is the only remaining task.
    A parallel join gateway may show up once one branch arrives."""
    active = adapter.get_active_activity_ids(instance_id)
    return "finance_check" not in active and "relative_check" in active


def complete_work_item(api: httpx.Client, request_id: str, task_key: str, data: dict) -> None:
    items = api.get(f"/requests/{request_id}/work-items").json()
    target = next(wi for wi in items if wi["task_definition_key"] == task_key)
    resp = api.post(
        f"/work-items/{target['id']}/complete", json={"data": data, "version": 1}
    )
    resp.raise_for_status()
    log(f"completed work_item {task_key}")


def reconcile(api: httpx.Client) -> dict:
    resp = api.post("/admin/reconcile")
    resp.raise_for_status()
    return resp.json()


def restart_engine() -> None:
    log("restarting Operaton container (shared DB preserved)")
    subprocess.run(f"{COMPOSE_OPERATON} restart operaton".split(), check=True, capture_output=True)
    wait_for(engine_up, timeout=180.0, interval=2.0, label="engine back after restart")


def _find_app_pids() -> list[int]:
    try:
        out = subprocess.run(
            ["pgrep", "-f", "uvicorn app.main:app"], capture_output=True, text=True
        ).stdout
        return [int(p) for p in out.split() if p.strip()]
    except Exception:
        return []


def restart_app() -> None:
    """Kill the FastAPI harness and start a fresh process (DB preserved)."""
    log("restarting FastAPI harness")
    for pid in _find_app_pids():
        log(f"stopping old app pid {pid}")
        os.kill(pid, signal.SIGTERM)
    deadline = time.time() + 15
    while time.time() < deadline and _find_app_pids():
        time.sleep(0.5)
    os.makedirs(ARTIFACTS, exist_ok=True)
    with open(APP_LOG, "ab") as log_fh:
        proc = subprocess.Popen(
            [VENV_PY, "-u", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"],
            stdout=log_fh,
            stderr=log_fh,
            cwd=ROOT,
            start_new_session=True,
        )
    with open(APP_PIDFILE, "w") as fh:
        fh.write(str(proc.pid))
    wait_for(app_up, timeout=30.0, interval=1.0, label="app back after restart")
    log(f"app restarted (pid {proc.pid})")


def save_evidence(name: str, payload: dict) -> None:
    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    path = os.path.join(EVIDENCE_DIR, name)
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2, default=str)
    log(f"evidence written: {path}")


def _reset_fixtures(adapter: OperatonAdapter) -> None:
    """Guarantee deterministic versions: cancel instances, drop deployments,
    redeploy v1 (-> version 1) and v2 (-> version 2) fresh."""
    log("resetting LONG_VISIT_POC fixtures (v1=1, v2=2)")
    for inst in adapter._client.get(
        "/process-instance", params={"processDefinitionKey": PROCESS_KEY}
    ).json():
        adapter.cancel_process(inst["id"])
    for definition in adapter.get_process_definitions(PROCESS_KEY):
        adapter._client.delete(f"/deployment/{definition['deploymentId']}", params={"cascade": "true"})
    bpmn_dir = os.path.join(ROOT, "bpmn", "operaton")
    adapter.deploy_process(
        open(os.path.join(bpmn_dir, "long_visit_poc.bpmn")).read(),
        PROCESS_KEY,
        name="scenario-long_visit_poc_v1",
    )
    adapter.deploy_process(
        open(os.path.join(bpmn_dir, "long_visit_poc_v2.bpmn")).read(),
        PROCESS_KEY,
        name="scenario-long_visit_poc_v2",
    )
    versions = sorted(d["version"] for d in adapter.get_process_definitions(PROCESS_KEY))
    assert 1 in versions and 2 in versions, f"fixture reset failed, versions={versions}"


def scenario_t07(api: httpx.Client, adapter: OperatonAdapter) -> dict:
    log("=== T07: full stack restart, shared DB preserved ===")
    evidence: dict = {"scenario": "t07", "steps": []}

    _reset_fixtures(adapter)
    request = create_request(api, version=1)
    request_id = request["id"]
    instance_id = wait_instance(api, request_id)
    evidence["steps"].append({"step": "start", "request_id": request_id, "instance_id": instance_id})

    run_external_tasks(adapter, instance_id)
    wait_parallel(adapter, instance_id)
    reconcile(api)
    complete_work_item(api, request_id, "finance_check", {"result": "OK", "balance_sufficient": True})
    wait_for(
        lambda: _finance_done(adapter, instance_id),
        timeout=20.0,
        label="engine reflects finance_check completion",
    )
    evidence["steps"].append({"step": "partial progress", "completed": ["finance_check"]})

    before = {
        "active_activities": sorted(adapter.get_active_activity_ids(instance_id)),
        "work_items": sorted(
            (wi["task_definition_key"], wi["state"]) for wi in api.get(f"/requests/{request_id}/work-items").json()
        ),
    }
    evidence["before_restart"] = before

    # full stack restart: engine + app (Postgres / shared DB untouched)
    restart_engine()
    restart_app()

    active = adapter.get_active_activity_ids(instance_id)
    assert "relative_check" in active, f"engine state lost after restart: {active}"
    req = api.get(f"/requests/{request_id}").json()
    assert req["lifecycle_state"] == "ACTIVE"
    items = api.get(f"/requests/{request_id}/work-items").json()
    states = sorted((wi["task_definition_key"], wi["state"]) for wi in items)
    assert ("finance_check", "COMPLETED") in states and ("relative_check", "ACTIVE") in states, states
    evidence["after_restart"] = {
        "active_activities": sorted(active),
        "request_state": req["lifecycle_state"],
        "work_items": states,
    }

    # reconciler re-sync must stay idempotent
    summary = reconcile(api)
    items = api.get(f"/requests/{request_id}/work-items").json()
    assert len(items) == 2, f"reconcile duplicated work items after restart: {len(items)}"
    evidence["reconcile_after_restart"] = {"summary": summary, "work_item_count": len(items)}

    # run the remaining branch to completion after restart
    complete_work_item(api, request_id, "relative_check", {"result": "OK", "relatives_verified": True})
    wait_for(
        lambda: "final_decision" in adapter.get_active_activity_ids(instance_id),
        timeout=60.0,
        label="final_decision after restart",
    )
    reconcile(api)
    complete_work_item(api, request_id, "final_decision", {"decision": "APPROVE"})
    wait_for(
        lambda: api.get(f"/requests/{request_id}").json().get("lifecycle_state") == "CLOSED",
        timeout=30.0,
        label="request CLOSED",
    )
    req = api.get(f"/requests/{request_id}").json()
    assert req["outcome"] == "COMPLETED"
    assert adapter.get_process_instance(instance_id).state == "ENDED"
    history = adapter.get_process_history(instance_id)
    assert history, "history must survive full restart"
    evidence["final"] = {
        "request_state": req["lifecycle_state"],
        "outcome": req["outcome"],
        "instance_state": adapter.get_process_instance(instance_id).state,
        "history_events": len(history),
    }

    save_evidence("t07-full-restart.json", evidence)
    log("=== T07 PASS ===")
    return evidence


def scenario_t09(api: httpx.Client, adapter: OperatonAdapter) -> dict:
    log("=== T09: durable timer across engine restart ===")
    evidence: dict = {"scenario": "t09", "steps": []}

    _reset_fixtures(adapter)
    request = create_request(api, version=1)
    request_id = request["id"]
    instance_id = wait_instance(api, request_id)
    evidence["steps"].append({"step": "start", "request_id": request_id, "instance_id": instance_id})

    run_external_tasks(adapter, instance_id)
    wait_parallel(adapter, instance_id)
    reconcile(api)
    complete_work_item(api, request_id, "finance_check", {"result": "OK", "balance_sufficient": True})
    complete_work_item(api, request_id, "relative_check", {"result": "OK", "relatives_verified": True})

    wait_for(
        lambda: "Timer_1" in adapter.get_active_activity_ids(instance_id),
        timeout=20.0,
        label="timer wait state",
    )
    jobs = adapter._client.get("/job", params={"processInstanceId": instance_id}).json()
    assert jobs, "timer job must exist before engine restart"
    evidence["before_restart"] = {
        "active_activities": sorted(adapter.get_active_activity_ids(instance_id)),
        "timer_jobs": len(jobs),
    }

    # engine-only restart while the timer is pending
    restart_engine()

    # durable timer fires after restart -> final_decision becomes active
    wait_for(
        lambda: "final_decision" in adapter.get_active_activity_ids(instance_id),
        timeout=120.0,
        interval=2.0,
        label="durable timer fired after restart",
    )
    evidence["after_restart"] = {
        "active_activities": sorted(adapter.get_active_activity_ids(instance_id)),
    }

    # reconciler surfaces the new wait state via our API
    summary = reconcile(api)
    items = api.get(f"/requests/{request_id}/work-items").json()
    keys = sorted(wi["task_definition_key"] for wi in items)
    assert "final_decision" in keys, f"reconciler missed final_decision: {keys}"
    evidence["reconciled"] = {"summary": summary, "work_items": keys}

    complete_work_item(api, request_id, "final_decision", {"decision": "APPROVE"})
    wait_for(
        lambda: api.get(f"/requests/{request_id}").json().get("lifecycle_state") == "CLOSED",
        timeout=30.0,
        label="request CLOSED",
    )
    req = api.get(f"/requests/{request_id}").json()
    assert req["outcome"] == "COMPLETED"
    assert adapter.get_process_instance(instance_id).state == "ENDED"
    evidence["final"] = {"request_state": req["lifecycle_state"], "outcome": req["outcome"]}

    save_evidence("t09-durable-timer-restart.json", evidence)
    log("=== T09 PASS ===")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=["t07", "t09", "all"], required=True)
    parser.add_argument("--api", default=API_BASE)
    parser.add_argument("--engine-url", default=ENGINE_BASE)
    args = parser.parse_args()

    if not app_up():
        log(f"app not reachable at {args.api}; run `make up-operaton` first")
        return 2

    api = httpx.Client(base_url=args.api, timeout=20.0)
    adapter = OperatonAdapter(base_url=args.engine_url)
    results = {}
    try:
        if args.scenario in ("t07", "all"):
            results["t07"] = scenario_t07(api, adapter)
        if args.scenario in ("t09", "all"):
            results["t09"] = scenario_t09(api, adapter)
        print(json.dumps({"scenarios": results}, indent=2, default=str))
        return 0
    finally:
        adapter.close()
        api.close()


if __name__ == "__main__":
    raise SystemExit(main())
