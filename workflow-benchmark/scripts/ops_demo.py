"""Part C operational demo + incident scripts (audit A12 deliverable).

Two modes, one per engine:

  --mode demo     Happy-path walkthrough: create a request, wait for the two
                  parallel human tasks, run the external worker, complete the
                  final decision, print the request/instance IDs, management
                  URLs, credentials and the next commands to reproduce it.

  --mode incident Terminal technical external-worker failure: the external task
                  fails with retries=0 so the process is stuck on
                  `load_prisoner_data`. The script then diagnoses it purely via
                  the engine's public REST API (find-process, activity,
                  failure/error, retries), counts the REST actions used, shows
                  the operator's retry action (Operaton: reset task retries;
                  Flowable OSS: move dead-letter job back to the normal queue),
                  and completes the recovery. UI steps are documented for
                  Operaton Cockpit / Flowable OSS (REST-only).

Usage:
    .venv/bin/python -m scripts.ops_demo --engine OPERATON --mode demo
    .venv/bin/python -m scripts.ops_demo --engine FLOWABLE --mode incident
"""

import argparse
import json
import os
import sys
import time

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.workflow.flowable import FlowableAdapter  # noqa: E402
from app.workflow.operaton import OperatonAdapter  # noqa: E402
from app.workers.external_worker import ExternalTaskWorker, default_prisoner_payload  # noqa: E402
from app.workers.flowable_worker import FlowableExternalTaskWorker  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_BASE = os.environ.get("BENCH_API", "http://localhost:8000")
PROCESS_KEY = "LONG_VISIT_POC"
TOPIC = "load_prisoner_data"
PARALLEL_TASKS = {"finance_check", "relative_check"}

ENGINE_BASES = {
    "OPERATON": os.environ.get("ENGINE_OPERATON_URL", "http://localhost:8080/engine-rest"),
    "FLOWABLE": os.environ.get("ENGINE_FLOWABLE_URL", "http://localhost:8081/flowable-rest/service"),
}
ADAPTERS = {"OPERATON": OperatonAdapter, "FLOWABLE": FlowableAdapter}
WORKERS = {"OPERATON": ExternalTaskWorker, "FLOWABLE": FlowableExternalTaskWorker}
ARTIFACTS = {e: os.path.join(ROOT, "artifacts", e.lower()) for e in ("OPERATON", "FLOWABLE")}

URLS = {
    "OPERATON": {
        "rest": ENGINE_BASES["OPERATON"],
        "cockpit": "http://localhost:8080/app/cockpit",
        "tasklist": "http://localhost:8080/app/tasklist",
        "admin": "http://localhost:8080/app/admin",
        "webapps": "http://localhost:8080",
        "creds": "demo / demo",
    },
    "FLOWABLE": {
        "rest": ENGINE_BASES["FLOWABLE"],
        "external_job_api": "http://localhost:8081/flowable-rest/external-job-api",
        "idm_console": "http://localhost:8081/flowable-idm",
        "creds": "rest-admin / test",
    },
}

UI_STEPS = {
    "OPERATON": [
        "Cockpit -> Process Instances -> search by business key",
        "Open instance -> Incidents tab (retries=0, exception shown)",
        "Cockpit -> External Tasks / Job definitions",
        "Incident -> Retry (or set retries via REST)",
    ],
    "FLOWABLE": [
        "OSS build has no Cockpit equivalent (management UI is EE-only)",
        "Diagnosis/recovery is REST-only: management/deadletter-jobs + move action",
    ],
}


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


def create_request(api: httpx.Client, engine: str, tag: str) -> dict:
    resp = api.post(
        "/requests",
        json={
            "request_type": PROCESS_KEY,
            "request_type_version": 1,
            "workflow_engine": engine,
            "variables": {"initiator": f"ops-{tag}"},
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


def wait_parallel(adapter, instance_id: str) -> None:
    wait_for(
        lambda: adapter.get_active_activity_ids(instance_id) == PARALLEL_TASKS,
        timeout=30.0,
        label="parallel tasks active",
    )


def complete_work_item(api: httpx.Client, request_id: str, task_key: str, data: dict) -> None:
    # The reconciler upserts WorkItems from engine human tasks asynchronously
    # (every ~2s); poll for the target task before completing it.
    def _find() -> dict | None:
        items = api.get(f"/requests/{request_id}/work-items").json()
        return next(
            (wi for wi in items if wi["task_definition_key"] == task_key and wi["state"] == "ACTIVE"),
            None,
        )

    target = wait_for(_find, timeout=20.0, label=f"work item {task_key} active")
    resp = api.post(f"/work-items/{target['id']}/complete", json={"data": data, "version": 1})
    resp.raise_for_status()


def wait_closed(api: httpx.Client, request_id: str) -> dict:
    req = wait_for(
        lambda: api.get(f"/requests/{request_id}").json()
        if api.get(f"/requests/{request_id}").json().get("lifecycle_state") == "CLOSED"
        else None,
        timeout=60.0,
        label="request CLOSED",
    )
    assert req["outcome"] == "COMPLETED", req
    return req


def run_external_task(engine: str, instance_id: str, *, fail: bool = False) -> dict:
    """Complete (or terminally fail) the external task for ONE instance."""
    worker = WORKERS[engine](engine_url=ENGINE_BASES[engine])
    try:
        jobs = worker.fetch_and_lock(max_tasks=5)
        target = next(j for j in jobs if j.get("processInstanceId") == instance_id)
        job_id = target["id"]
        if fail:
            worker.fail(
                job_id,
                error_message="external service down (simulated terminal failure)",
                error_details="retries exhausted by design for ops incident demo",
                retries=0,
            )
            return {"job_id": job_id, "action": "fail", "retries": 0}
        payload = default_prisoner_payload(target)
        worker.complete(job_id, variables=payload)
        return {"job_id": job_id, "action": "complete"}
    finally:
        worker.close()


class CountingClient:
    """Wrap an httpx.Client so every REST call made through the adapter during
    diagnosis is counted (method + path)."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client
        self.counts: dict[str, int] = {}

    def _count(self, method: str, path: str) -> None:
        short = f"{method} {path.split('?')[0]}"
        self.counts[short] = self.counts.get(short, 0) + 1

    def request(self, method, url, **kw):
        self._count(method, str(url))
        return self._client.request(method, url, **kw)

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def put(self, url, **kw):
        return self.request("PUT", url, **kw)

    def delete(self, url, **kw):
        return self.request("DELETE", url, **kw)

    def close(self) -> None:
        self._client.close()


def save_evidence(engine: str, mode: str, payload: dict) -> str:
    out_dir = os.path.join(ARTIFACTS[engine], "faults")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"ops_demo_{mode}.json")
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2, default=str)
    log(f"evidence written: {path}")
    return path


def _print_stack(engine: str, request_id: str, instance_id: str) -> None:
    u = URLS[engine]
    print("\n== stack info ==")
    print(f"engine:  {engine}")
    print(f"request:  {request_id}")
    print(f"instance: {instance_id}")
    for k, v in u.items():
        print(f"  {k:16} {v}")


def run_demo(engine: str) -> dict:
    log(f"demo mode ({engine}): happy path")
    api = httpx.Client(base_url=API_BASE, timeout=30.0)
    try:
        req = create_request(api, engine, "demo")
        request_id = req["id"]
        log(f"request created: {request_id}")
        instance_id = wait_instance(api, request_id)
        log(f"instance started: {instance_id}")

        adapter = ADAPTERS[engine](base_url=ENGINE_BASES[engine])
        try:
            run_external_task(engine, instance_id)
            log("external task (load_prisoner_data) completed")

            wait_parallel(adapter, instance_id)
            log("parallel tasks active: finance_check, relative_check")

            for task_key in sorted(PARALLEL_TASKS):
                complete_work_item(api, request_id, task_key, {"outcome": "PASS", "checked_by": "ops-demo"})
                log(f"human task completed: {task_key}")

            wait_for(
                lambda: "final_decision" in adapter.get_active_activity_ids(instance_id),
                timeout=90.0,
                label="final_decision active",
            )
            complete_work_item(api, request_id, "final_decision", {"decision": "APPROVE"})
            log("final decision completed: APPROVE")

            req = wait_closed(api, request_id)
            log(f"request closed: outcome={req['outcome']}")
        finally:
            adapter.close()
        _print_stack(engine, request_id, instance_id)

        result = {
            "mode": "demo",
            "engine": engine,
            "request_id": request_id,
            "instance_id": instance_id,
            "outcome": req["outcome"],
            "next_steps": [
                f"Browse cockpit: {URLS[engine].get('cockpit')}",
                f"Query REST: curl {URLS[engine]['rest']}/process-instance?processDefinitionKey={PROCESS_KEY}",
                "Repeat with: make incident-<engine>",
            ],
        }
        save_evidence(engine, "demo", result)
        return result
    finally:
        api.close()


def run_incident(engine: str) -> dict:
    log(f"incident mode ({engine}): terminal external-worker failure")
    api = httpx.Client(base_url=API_BASE, timeout=30.0)
    try:
        req = create_request(api, engine, "incident")
        request_id = req["id"]
        log(f"request created: {request_id}")
        instance_id = wait_instance(api, request_id)
        log(f"instance started: {instance_id}")

        # terminal technical failure (retries=0)
        fired = run_external_task(engine, instance_id, fail=True)
        log(f"external task terminally failed: {fired}")

        adapter = ADAPTERS[engine](base_url=ENGINE_BASES[engine])
        wrapped = CountingClient(adapter._client)
        adapter._client = wrapped
        diagnosis = {}
        try:
            diagnosis["find_process"] = {
                "state": adapter.get_process_instance(instance_id).state,
                "instance_id": instance_id,
            }
            activities = adapter.get_active_activity_ids(instance_id)
            diagnosis["activity"] = {
                "stuck_on": "load_prisoner_data" if "load_prisoner_data" in activities else None,
                "active": sorted(activities),
            }
            jobs = adapter.get_failed_jobs(instance_id)
            diagnosis["failure"] = [
                {
                    "job_id": j.job_id,
                    "retries": j.retries,
                    "exception_message": j.exception_message,
                }
                for j in jobs
            ]
            diagnosis["error"] = {
                "failed_jobs": len(jobs),
                "message": jobs[0].exception_message if jobs else None,
            }
            diagnosis["retries"] = {"remaining": 0, "expected_terminal": True}
            diagnosis["history"] = adapter.get_process_history(instance_id)[-5:]
            diagnosis["rest_actions"] = wrapped.counts
            log(f"diagnosis complete: stuck on load_prisoner_data, {len(jobs)} failed job(s), retries=0")
        finally:
            adapter.close()

        # operator retry + recovery
        recovery = {"action": None, "rest_actions": None}
        worker = WORKERS[engine](engine_url=ENGINE_BASES[engine])
        try:
            if engine == "OPERATON":
                # reset retries on the REAL external task (the incident id from
                # get_failed_jobs is not the task id), then complete it.
                base = ENGINE_BASES[engine].rstrip("/")
                tasks = httpx.get(
                    f"{base}/external-task",
                    params={"processInstanceId": instance_id},
                    timeout=30.0,
                ).json()
                ext = next(t for t in tasks if t.get("activityId") == "load_prisoner_data")
                ext_id = ext["id"]
                resp = httpx.put(
                    f"{base}/external-task/{ext_id}/retries",
                    json={"retries": 1},
                    timeout=30.0,
                )
                resp.raise_for_status()
                recovery["action"] = f"PUT /external-task/{ext_id}/retries {{retries:1}}"
                run_external_task(engine, instance_id)
                recovery["result"] = "external task completed after retry"
            else:
                # Flowable OSS: move the dead-letter job back to the normal queue
                job_id = diagnosis["failure"][0]["job_id"]
                resp = httpx.post(
                    f"{ENGINE_BASES['FLOWABLE'].rstrip('/')}/management/deadletter-jobs/{job_id}",
                    json={"action": "move"},
                    auth=("rest-admin", "test"),
                    timeout=30.0,
                )
                resp.raise_for_status()
                recovery["action"] = f"POST /management/deadletter-jobs/{job_id} {{action: move}}"
                run_external_task(engine, instance_id)
                recovery["result"] = "external job completed after dead-letter move"
        finally:
            worker.close()

        adapter = ADAPTERS[engine](base_url=ENGINE_BASES[engine])
        try:
            for task_key in sorted(PARALLEL_TASKS):
                complete_work_item(api, request_id, task_key, {"outcome": "PASS", "checked_by": "ops-incident"})
            wait_for(
                lambda: "final_decision" in adapter.get_active_activity_ids(instance_id),
                timeout=90.0,
                label="final_decision active",
            )
            complete_work_item(api, request_id, "final_decision", {"decision": "APPROVE"})
            req = wait_closed(api, request_id)
            log(f"recovery complete, request closed: outcome={req['outcome']}")
        finally:
            adapter.close()

        recovery["rest_actions"] = diagnosis["rest_actions"]
        result = {
            "mode": "incident",
            "engine": engine,
            "request_id": request_id,
            "instance_id": instance_id,
            "terminal_failure": fired,
            "diagnosis": diagnosis,
            "recovery": recovery,
            "ui_steps": UI_STEPS[engine],
            "ui_actions": {
                "type": "none" if engine == "FLOWABLE" else "cockpit",
                "steps": len(UI_STEPS[engine]) if engine == "FLOWABLE" else 4,
            },
        }
        _print_stack(engine, request_id, instance_id)
        save_evidence(engine, "incident", result)
        return result
    finally:
        api.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=["OPERATON", "FLOWABLE"], required=True)
    parser.add_argument("--mode", choices=["demo", "incident"], required=True)
    args = parser.parse_args()

    if args.mode == "demo":
        run_demo(args.engine)
    else:
        run_incident(args.engine)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
