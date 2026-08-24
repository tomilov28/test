"""Fault recovery + restart + stress scenarios (benchmark phase 4).

Orchestrates the restart-heavy scenarios that cannot live inside pytest (they
stop/start the harness mid-flight), plus the ~50-instance stress smoke and the
Flowable cancellation-deadlock reproduction. Evidence is written under
artifacts/<engine>/faults/.

Scenarios:
  R1  app restart while a human task is active
  R2  app restart while the timer wait state is pending
  R3  app restart with a PENDING outbox command (pre-dispatch crash)
  R4  app restart right after an engine action, before reconciliation
  R5  worker lock held across a harness restart (lock expiry re-acquire)
  STRESS  ~50 instances per engine (duplicates / lost / failed-job checks,
          rough latency + throughput only)
  DEADLOCK  [FLOWABLE] cancellation 20-50x racing the async executor

Usage:
    .venv/bin/python -m scripts.fault_scenarios --engine OPERATON [--scenario all|r1|...]
    .venv/bin/python -m scripts.fault_scenarios --engine FLOWABLE
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

from app.workflow.flowable import FlowableAdapter  # noqa: E402
from app.workflow.operaton import OperatonAdapter  # noqa: E402
from app.workers.external_worker import ExternalTaskWorker  # noqa: E402
from app.workers.flowable_worker import FlowableExternalTaskWorker  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PY = os.path.join(ROOT, ".venv", "bin", "python")
API_BASE = os.environ.get("BENCH_API", "http://localhost:8000")
PROCESS_KEY = "LONG_VISIT_POC"
TOPIC = "load_prisoner_data"
PARALLEL_TASKS = {"finance_check", "relative_check"}

COMPOSE = {
    "OPERATON": f"docker compose -f {ROOT}/docker-compose.yml -f {ROOT}/docker-compose.operaton.yml",
    "FLOWABLE": f"docker compose -f {ROOT}/docker-compose.yml -f {ROOT}/docker-compose.flowable.yml",
}
ENGINE_BASES = {
    "OPERATON": os.environ.get("ENGINE_OPERATON_URL", "http://localhost:8080/engine-rest"),
    "FLOWABLE": os.environ.get("ENGINE_FLOWABLE_URL", "http://localhost:8081/flowable-rest/service"),
}
ENGINE_PROBE = {"OPERATON": "/engine", "FLOWABLE": "/management/engine"}
ADAPTERS = {"OPERATON": OperatonAdapter, "FLOWABLE": FlowableAdapter}
WORKERS = {"OPERATON": ExternalTaskWorker, "FLOWABLE": FlowableExternalTaskWorker}
ARTIFACTS = {e: os.path.join(ROOT, "artifacts", e.lower()) for e in ("OPERATON", "FLOWABLE")}


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


def engine_up(engine: str) -> bool:
    url = ENGINE_BASES[engine] + ENGINE_PROBE[engine]
    try:
        return httpx.get(url, timeout=3.0).status_code < 500
    except Exception:
        return False


def app_up() -> bool:
    try:
        return httpx.get(f"{API_BASE}/health", timeout=3.0).status_code == 200
    except Exception:
        return False


def make_adapter(engine: str):
    return ADAPTERS[engine](base_url=ENGINE_BASES[engine])


def save_evidence(engine: str, name: str, payload: dict) -> str:
    out_dir = os.path.join(ARTIFACTS[engine], "faults")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, name)
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2, default=str)
    log(f"evidence written: {path}")
    return path


# -- harness control -----------------------------------------------------------


def create_request(api: httpx.Client, engine: str, tag: str, version: int = 1) -> dict:
    resp = api.post(
        "/requests",
        json={
            "request_type": PROCESS_KEY,
            "request_type_version": version,
            "workflow_engine": engine,
            "variables": {"initiator": f"{engine.lower()}-{tag}"},
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


def run_external_task(engine: str, instance_id: str) -> None:
    worker = WORKERS[engine](engine_url=ENGINE_BASES[engine])
    try:
        for _ in range(15):
            worker.poll_once(instance_filter=instance_id)
            if worker.results["completed"]:
                return
            time.sleep(1.0)
        raise AssertionError(f"external task never completed for {instance_id}: {worker.results}")
    finally:
        worker.close()


def drain_external_topic(engine: str, timeout: float = 90.0) -> dict:
    """Complete ALL pending external jobs for the topic.

    Flowable's acquire endpoint returns at most `numberOfTasks` jobs of a topic
    (oldest first); leftover jobs from earlier scenarios can starve a target
    instance's job out of the batch. Draining before each scenario keeps the
    queue clean so per-instance acquisition is deterministic.
    """
    worker = WORKERS[engine](engine_url=ENGINE_BASES[engine])
    try:
        deadline = time.time() + timeout
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
        return dict(worker.results)
    finally:
        worker.close()


def wait_parallel(adapter, instance_id: str) -> None:
    wait_for(
        lambda: adapter.get_active_activity_ids(instance_id) == PARALLEL_TASKS,
        timeout=30.0,
        label="parallel tasks active",
    )


def wait_activity(adapter, instance_id: str, activity: str, present: bool = True, timeout: float = 90.0):
    wait_for(
        lambda: (activity in adapter.get_active_activity_ids(instance_id)) == present,
        timeout=timeout,
        label=f"activity {activity} present={present}",
    )


def complete_work_item(api: httpx.Client, request_id: str, task_key: str, data: dict) -> None:
    items = api.get(f"/requests/{request_id}/work-items").json()
    target = next(wi for wi in items if wi["task_definition_key"] == task_key and wi["state"] == "ACTIVE")
    resp = api.post(f"/work-items/{target['id']}/complete", json={"data": data, "version": 1})
    resp.raise_for_status()


def reconcile(api: httpx.Client) -> dict:
    resp = api.post("/admin/reconcile")
    resp.raise_for_status()
    return resp.json()


def wait_closed(api: httpx.Client, request_id: str, outcome: str = "COMPLETED") -> dict:
    req = wait_for(
        lambda: api.get(f"/requests/{request_id}").json() if (
            api.get(f"/requests/{request_id}").json().get("lifecycle_state") == "CLOSED"
        ) else None,
        timeout=60.0,
        label="request CLOSED",
    )
    assert req["outcome"] == outcome, req
    return req


def command_states(api: httpx.Client, request_id: str) -> list[dict]:
    return api.get(f"/requests/{request_id}/commands").json()


def _cancel_terminal(api: httpx.Client, request_id: str) -> dict | None:
    for c in command_states(api, request_id):
        if c["command_type"] == "CANCEL_PROCESS" and c["state"] in ("DONE", "FAILED"):
            return c
    return None


def _command_in_state(api: httpx.Client, request_id: str, command_type: str, state: str) -> dict | None:
    for c in command_states(api, request_id):
        if c["command_type"] == command_type and c["state"] == state:
            return c
    return None


def no_failed_commands(api: httpx.Client, request_id: str) -> bool:
    return not [c for c in command_states(api, request_id) if c["state"] == "FAILED"]


def fire_timers(adapter, instance_ids: list[str]) -> int:
    return sum(adapter.fire_timer(i) for i in instance_ids)


def _find_app_pids() -> list[int]:
    try:
        out = subprocess.run(
            ["pgrep", "-f", "uvicorn app.main:app"], capture_output=True, text=True
        ).stdout
        return [int(p) for p in out.split() if p.strip()]
    except Exception:
        return []


def _kill_app() -> None:
    for pid in _find_app_pids():
        log(f"stopping old app pid {pid}")
        os.kill(pid, signal.SIGTERM)
    deadline = time.time() + 15
    while time.time() < deadline and _find_app_pids():
        time.sleep(0.5)


def _start_app() -> int:
    # track the pid in every engine's artifact dir so `make down` finds it
    for engine in ("OPERATON", "FLOWABLE"):
        os.makedirs(ARTIFACTS[engine], exist_ok=True)
    with open(os.path.join(ARTIFACTS["OPERATON"], "app.log"), "ab") as log_fh:
        proc = subprocess.Popen(
            [VENV_PY, "-u", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"],
            stdout=log_fh,
            stderr=log_fh,
            cwd=ROOT,
            start_new_session=True,
        )
    for engine in ("OPERATON", "FLOWABLE"):
        with open(os.path.join(ARTIFACTS[engine], "app.pid"), "w") as fh:
            fh.write(str(proc.pid))
    wait_for(app_up, timeout=30.0, interval=1.0, label="app back after restart")
    log(f"app started (pid {proc.pid})")
    return proc.pid


def restart_app() -> None:
    log("restarting FastAPI harness")
    _kill_app()
    _start_app()


def restart_engine(engine: str) -> None:
    log(f"restarting {engine} container (shared DB preserved)")
    service = "operaton" if engine == "OPERATON" else "flowable"
    subprocess.run(f"{COMPOSE[engine]} restart {service}".split(), check=True, capture_output=True)
    wait_for(lambda: engine_up(engine), timeout=180.0, interval=2.0, label="engine back after restart")


def set_up_request_for_cancel(api: httpx.Client, adapter, engine: str, tag: str) -> tuple[str, str]:
    request = create_request(api, engine, tag=tag)
    instance_id = wait_instance(api, request["id"])
    run_external_task(engine, instance_id)
    wait_parallel(adapter, instance_id)
    reconcile(api)
    return request["id"], instance_id


def scenario_r1(api: httpx.Client, adapter, engine: str) -> dict:
    log(f"=== R1 [{engine}]: app restart during active human task ===")
    evidence: dict = {"scenario": "r1", "engine": engine, "steps": []}
    drain_external_topic(engine)

    request = create_request(api, engine, tag="r1")
    request_id = request["id"]
    instance_id = wait_instance(api, request_id)
    run_external_task(engine, instance_id)
    wait_parallel(adapter, instance_id)
    reconcile(api)
    items_before = sorted(
        (wi["task_definition_key"], wi["state"])
        for wi in api.get(f"/requests/{request_id}/work-items").json()
    )
    evidence["before_restart"] = {
        "active_activities": sorted(adapter.get_active_activity_ids(instance_id)),
        "work_items": items_before,
    }

    restart_app()

    active = adapter.get_active_activity_ids(instance_id)
    assert "relative_check" in active, active
    req = api.get(f"/requests/{request_id}").json()
    assert req["lifecycle_state"] == "ACTIVE"
    items_after = sorted(
        (wi["task_definition_key"], wi["state"])
        for wi in api.get(f"/requests/{request_id}/work-items").json()
    )
    assert items_after == items_before, (items_before, items_after)
    summary = reconcile(api)
    items = api.get(f"/requests/{request_id}/work-items").json()
    assert len(items) == 2, f"reconcile duplicated work items after restart: {len(items)}"
    evidence["after_restart"] = {
        "active_activities": sorted(active),
        "request_state": req["lifecycle_state"],
        "work_items": items_after,
        "reconcile_after_restart": {"work_item_count": len(items), "summary": summary},
    }

    complete_work_item(api, request_id, "finance_check", {"result": "OK", "balance_sufficient": True})
    complete_work_item(api, request_id, "relative_check", {"result": "OK", "relatives_verified": True})
    if engine == "FLOWABLE":
        fire_timers(adapter, [instance_id])
    wait_activity(adapter, instance_id, "final_decision", present=True)
    reconcile(api)
    complete_work_item(api, request_id, "final_decision", {"decision": "APPROVE"})
    req = wait_closed(api, request_id)
    assert no_failed_commands(api, request_id)
    evidence["final"] = {"request_state": req["lifecycle_state"], "outcome": req["outcome"]}
    save_evidence(engine, "r1-restart-during-human-task.json", evidence)
    log(f"=== R1 [{engine}] PASS ===")
    return evidence


def scenario_r2(api: httpx.Client, adapter, engine: str) -> dict:
    log(f"=== R2 [{engine}]: app restart during timer wait ===")
    evidence: dict = {"scenario": "r2", "engine": engine, "steps": []}
    drain_external_topic(engine)

    request = create_request(api, engine, tag="r2")
    request_id = request["id"]
    instance_id = wait_instance(api, request_id)
    run_external_task(engine, instance_id)
    wait_parallel(adapter, instance_id)
    reconcile(api)
    complete_work_item(api, request_id, "finance_check", {"result": "OK", "balance_sufficient": True})
    complete_work_item(api, request_id, "relative_check", {"result": "OK", "relatives_verified": True})
    wait_activity(adapter, instance_id, "Timer_1", present=True, timeout=30.0)
    evidence["before_restart"] = {"active_activities": sorted(adapter.get_active_activity_ids(instance_id))}

    restart_app()

    # timer is engine-side: fires after restart, reconciler surfaces final_decision
    wait_activity(adapter, instance_id, "final_decision", present=True, timeout=90.0)
    reconcile(api)
    items = api.get(f"/requests/{request_id}/work-items").json()
    assert any(wi["task_definition_key"] == "final_decision" for wi in items), items
    evidence["after_restart"] = {
        "active_activities": sorted(adapter.get_active_activity_ids(instance_id)),
        "work_items": sorted((wi["task_definition_key"], wi["state"]) for wi in items),
    }

    complete_work_item(api, request_id, "final_decision", {"decision": "APPROVE"})
    req = wait_closed(api, request_id)
    assert no_failed_commands(api, request_id)
    evidence["final"] = {"request_state": req["lifecycle_state"], "outcome": req["outcome"]}
    save_evidence(engine, "r2-restart-during-timer-wait.json", evidence)
    log(f"=== R2 [{engine}] PASS ===")
    return evidence


def scenario_r3(api: httpx.Client, adapter, engine: str) -> dict:
    log(f"=== R3 [{engine}]: restart with a PENDING outbox command ===")
    evidence: dict = {"scenario": "r3", "engine": engine, "steps": []}

    # Create a request and stop the app before the dispatcher can claim its
    # START_PROCESS command (2s dispatch interval gives a wide window).
    captured = None
    for attempt in range(3):
        request = create_request(api, engine, tag=f"r3-{attempt}")
        request_id = request["id"]
        cmds = command_states(api, request_id)
        start = next(c for c in cmds if c["command_type"] == "START_PROCESS")
        if start["state"] == "PENDING":
            captured = {"request_id": request_id, "number": request["number"], "cmd": start}
            log(f"captured PENDING START_PROCESS on attempt {attempt}")
            break
        log(f"attempt {attempt}: command already {start['state']}; retrying")
        time.sleep(0.5)
    assert captured, "could not capture a PENDING START_PROCESS command"

    evidence["before_restart"] = {"request_id": captured["request_id"], "command": captured["cmd"]}

    # kill the app right now: the command is durable in Postgres, not yet dispatched
    _kill_app()
    time.sleep(2.0)  # leave the app down briefly, then bring it back
    _start_app()

    instance_id = wait_instance(api, captured["request_id"])
    cmd = wait_for(
        lambda: _command_in_state(api, captured["request_id"], "START_PROCESS", "DONE"),
        timeout=40.0,
        label="START_PROCESS DONE after restart",
    )
    matches = adapter.find_process_instance_by_business_key(PROCESS_KEY, captured["number"])
    assert len(matches) == 1, matches
    assert matches[0].process_instance_id == instance_id
    assert cmd["attempts"] == 1, cmd
    evidence["after_restart"] = {
        "instance_id": instance_id,
        "command": cmd,
        "engine_instances": [m.process_instance_id for m in matches],
    }
    save_evidence(engine, "r3-restart-with-pending-command.json", evidence)
    log(f"=== R3 [{engine}] PASS ===")
    return evidence


def scenario_r4(api: httpx.Client, adapter, engine: str) -> dict:
    log(f"=== R4 [{engine}]: app restart after engine action, before reconciliation ===")
    evidence: dict = {"scenario": "r4", "engine": engine, "steps": []}
    drain_external_topic(engine)

    request = create_request(api, engine, tag="r4")
    request_id = request["id"]
    instance_id = wait_instance(api, request_id)
    run_external_task(engine, instance_id)
    wait_parallel(adapter, instance_id)
    reconcile(api)

    # engine action: dispatcher completes finance_check on the engine
    complete_work_item(api, request_id, "finance_check", {"result": "OK", "balance_sufficient": True})
    wait_activity(adapter, instance_id, "finance_check", present=False, timeout=40.0)
    cmd = next(c for c in command_states(api, request_id) if c["command_type"] == "COMPLETE_TASK")
    assert cmd["state"] == "DONE", cmd
    evidence["before_restart"] = {
        "instance_id": instance_id,
        "engine_has_finance_check": "finance_check" in adapter.get_active_activity_ids(instance_id),
        "complete_task_command": cmd,
    }

    restart_app()

    active = adapter.get_active_activity_ids(instance_id)
    assert "relative_check" in active and "finance_check" not in active, active
    items = api.get(f"/requests/{request_id}/work-items").json()
    states = sorted((wi["task_definition_key"], wi["state"]) for wi in items)
    assert ("finance_check", "COMPLETED") in states and ("relative_check", "ACTIVE") in states, states
    summary = reconcile(api)
    items = api.get(f"/requests/{request_id}/work-items").json()
    assert len(items) == 2, f"reconcile duplicated work items after R4 restart: {len(items)}"
    assert no_failed_commands(api, request_id)
    evidence["after_restart"] = {
        "active_activities": sorted(active),
        "work_items": states,
        "reconcile_after_restart": {"work_item_count": len(items), "summary": summary},
    }

    complete_work_item(api, request_id, "relative_check", {"result": "OK", "relatives_verified": True})
    if engine == "FLOWABLE":
        fire_timers(adapter, [instance_id])
    wait_activity(adapter, instance_id, "final_decision", present=True)
    reconcile(api)
    complete_work_item(api, request_id, "final_decision", {"decision": "APPROVE"})
    req = wait_closed(api, request_id)
    assert no_failed_commands(api, request_id)
    evidence["final"] = {"request_state": req["lifecycle_state"], "outcome": req["outcome"]}
    save_evidence(engine, "r4-restart-post-engine-action.json", evidence)
    log(f"=== R4 [{engine}] PASS ===")
    return evidence


def scenario_r5(api: httpx.Client, adapter, engine: str) -> dict:
    log(f"=== R5 [{engine}]: worker lock held across a harness restart ===")
    evidence: dict = {"scenario": "r5", "engine": engine, "steps": []}
    drain_external_topic(engine)

    request = create_request(api, engine, tag="r5")
    request_id = request["id"]
    instance_id = wait_instance(api, request_id)

    # first worker acquires and LOCKS the external task, then "crashes"
    worker = WORKERS[engine](engine_url=ENGINE_BASES[engine])
    try:
        locked = wait_for(
            lambda: next(
                (t for t in worker.fetch_and_lock() if t.get("processInstanceId") == instance_id), None
            ),
            timeout=20.0,
            label="external task locked",
        )
        evidence["before_restart"] = {
            "instance_id": instance_id,
            "locked_external_task": {"id": locked["id"], "worker_id": worker.worker_id},
            "lock_owner": worker.worker_id,
        }
    finally:
        worker.close()  # simulated crash: task stays locked until lock expiry

    restart_app()

    # lock expires -> a fresh worker re-acquires and completes. Flowable's async
    # executor resets expired external-job locks on its own cadence (~60-90s),
    # so allow a generous window and record the observed re-acquisition latency.
    fresh = WORKERS[engine](engine_url=ENGINE_BASES[engine])
    reacquire_start = time.time()
    try:
        for _ in range(240):
            fresh.poll_once(instance_filter=instance_id)
            if fresh.results["completed"] >= 1:
                break
            time.sleep(1.0)
        assert fresh.results["completed"] >= 1, fresh.results
        reacquire_latency_s = time.time() - reacquire_start
    finally:
        fresh.close()
    evidence["after_restart"] = {
        "reacquired_and_completed": fresh.results["completed"] >= 1,
        "reacquire_latency_s": round(reacquire_latency_s, 1),
    }
    log(f"R5: lock re-acquired after {reacquire_latency_s:.1f}s")

    wait_parallel(adapter, instance_id)
    reconcile(api)
    complete_work_item(api, request_id, "finance_check", {"result": "OK", "balance_sufficient": True})
    complete_work_item(api, request_id, "relative_check", {"result": "OK", "relatives_verified": True})
    if engine == "FLOWABLE":
        fire_timers(adapter, [instance_id])
    wait_activity(adapter, instance_id, "final_decision", present=True)
    reconcile(api)
    complete_work_item(api, request_id, "final_decision", {"decision": "APPROVE"})
    req = wait_closed(api, request_id)
    assert no_failed_commands(api, request_id)
    evidence["final"] = {"request_state": req["lifecycle_state"], "outcome": req["outcome"]}
    save_evidence(engine, "r5-restart-during-worker-lock.json", evidence)
    log(f"=== R5 [{engine}] PASS ===")
    return evidence


def scenario_stress(api: httpx.Client, adapter, engine: str, n: int = 50) -> dict:
    log(f"=== STRESS [{engine}]: {n} instances (smoke) ===")
    evidence: dict = {"scenario": "stress", "engine": engine, "n": n}
    drain_external_topic(engine)

    t0 = time.time()
    created = [create_request(api, engine, tag=f"stress-{i}") for i in range(n)]
    request_ids = [r["id"] for r in created]
    numbers = {r["number"] for r in created}
    assert len(numbers) == n, "business keys must be unique"

    t_start_done = time.time()
    instances = {rid: wait_instance(api, rid) for rid in request_ids}
    t_start_done = time.time() - t_start_done
    evidence["start"] = {
        "requests_created": n,
        "all_instances_started": len(instances),
        "start_elapsed_s": round(t_start_done, 2),
        "throughput_start_per_s": round(n / max(t_start_done, 0.1), 2),
    }

    # no duplicate instances: each business key maps to exactly one engine instance
    dupes = {}
    for r in created:
        matches = adapter.find_process_instance_by_business_key(PROCESS_KEY, r["number"])
        if len(matches) != 1:
            dupes[r["number"]] = [m.process_instance_id for m in matches]
    assert not dupes, f"duplicate/missing instances: {dupes}"
    evidence["instances_unique_per_request"] = True

    # drain the external topic for all 50 instances
    worker = WORKERS[engine](engine_url=ENGINE_BASES[engine])
    try:
        t_drain = time.time()
        remaining = set(instances.values())
        for _ in range(120):
            worker.poll_once()
            fetched = worker.results["fetched"]
            if fetched:
                pass
            still = {
                iid
                for iid in remaining
                if "load_prisoner_data" in adapter.get_active_activity_ids(iid)
            }
            remaining = still
            if not remaining:
                break
            time.sleep(1.0)
        assert not remaining, f"external tasks not drained: {remaining}"
        evidence["external_drain"] = {
            "elapsed_s": round(time.time() - t_drain, 2),
            "remaining": len(remaining),
        }
    finally:
        worker.close()

    # complete both parallel branches for all, fire timers, complete finals
    t_complete = time.time()
    for rid in request_ids:
        reconcile(api)
        for key in sorted(PARALLEL_TASKS):
            try:
                complete_work_item(api, rid, key, {"result": "OK", "ok": True})
            except StopIteration:
                pass
    for rid in request_ids:
        instance_id = instances[rid]
        fire_timers(adapter, [instance_id])
    for rid in request_ids:
        wait_for(
            lambda rid=rid: "final_decision"
            in adapter.get_active_activity_ids(instances[rid]),
            timeout=90.0,
            label=f"final_decision for {rid}",
        )
    for rid in request_ids:
        reconcile(api)
        complete_work_item(api, rid, "final_decision", {"decision": "APPROVE"})
    closed = {rid: wait_closed(api, rid) for rid in request_ids}
    evidence["completion"] = {
        "closed": len(closed),
        "all_completed": all(c["outcome"] == "COMPLETED" for c in closed.values()),
        "elapsed_s": round(time.time() - t_complete, 2),
        "total_elapsed_s": round(time.time() - t0, 2),
        "throughput_instances_per_s": round(n / max(time.time() - t0, 0.1), 2),
    }

    failed_commands = sum(
        len([c for c in command_states(api, rid) if c["state"] == "FAILED"]) for rid in request_ids
    )
    engine_failed_jobs = sum(len(adapter.get_failed_jobs(iid)) for iid in instances.values())
    evidence["no_failures"] = {
        "failed_commands": failed_commands,
        "engine_failed_jobs": engine_failed_jobs,
    }
    assert failed_commands == 0, f"FAILED commands in stress run: {failed_commands}"
    assert engine_failed_jobs == 0, f"engine failed jobs: {engine_failed_jobs}"
    save_evidence(engine, "stress-smoke.json", evidence)
    log(f"=== STRESS [{engine}] PASS ===")
    return evidence


def scenario_deadlock(api: httpx.Client, adapter, engine: str, n: int = 25) -> dict:
    """Flowable cancellation 20-50x racing the async executor.

    Each iteration cancels a running instance right as engine-side async work
    is happening; a PostgreSQL deadlock can surface as a transient 5xx on the
    DELETE. We record recurrence, the exact error, and that retries converge.
    """
    log(f"=== DEADLOCK [{engine}]: cancellation race x{n} ===")
    assert engine == "FLOWABLE", "deadlock reproduction targets Flowable"
    evidence: dict = {"scenario": "deadlock", "engine": engine, "iterations": n, "recurrence": 0}
    drain_external_topic(engine)

    for i in range(n):
        request = create_request(api, engine, tag=f"deadlock-{i}")
        request_id = request["id"]
        instance_id = wait_instance(api, request_id)
        run_external_task(engine, instance_id)
        wait_parallel(adapter, instance_id)
        reconcile(api)

        resp = api.post(f"/requests/{request_id}/cancel", json={"reason": "deadlock-race"})
        resp.raise_for_status()

        outcome = wait_for(
            lambda: _cancel_terminal(api, request_id),
            timeout=60.0,
            label=f"iteration {i} CANCEL terminal state",
        )
        evidence.setdefault("events", []).append(
            {
                "iteration": i,
                "request_id": request_id,
                "cancel_state": outcome["state"],
                "attempts": outcome["attempts"],
                "last_error": outcome["last_error"],
            }
        )
        if outcome["state"] == "FAILED" or (outcome["last_error"] and "deadlock" in outcome["last_error"].lower()):
            evidence["recurrence"] += 1

        # convergence: request must end CLOSED/CANCELLED regardless
        req = wait_closed(api, request_id, outcome="CANCELLED")
        assert req["lifecycle_state"] == "CLOSED" and req["outcome"] == "CANCELLED", req
        log(f"iteration {i}: cancel state={outcome['state']} attempts={outcome['attempts']}")

    evidence["retries_converge"] = True
    save_evidence(engine, "cancel-deadlock-reproduction.json", evidence)
    log(f"=== DEADLOCK [{engine}] done: recurrence={evidence['recurrence']}/{n} ===")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=["OPERATON", "FLOWABLE"], required=True)
    parser.add_argument(
        "--scenario", choices=["r1", "r2", "r3", "r4", "r5", "stress", "deadlock", "all"],
        default="all",
    )
    parser.add_argument("--api", default=API_BASE)
    parser.add_argument("--stress-n", type=int, default=50)
    parser.add_argument("--deadlock-n", type=int, default=25)
    args = parser.parse_args()
    engine = args.engine

    if not app_up():
        log(f"app not reachable at {args.api}; start the harness first")
        return 2
    if not engine_up(engine):
        log(f"engine {engine} not reachable; start its infra first")
        return 2

    api = httpx.Client(base_url=args.api, timeout=20.0)
    adapter = make_adapter(engine)
    results: dict = {}
    try:
        wanted = (
            ["r1", "r2", "r3", "r4", "r5", "stress"]
            if args.scenario == "all"
            else [args.scenario]
        )
        if args.scenario == "all" and engine == "FLOWABLE":
            wanted.append("deadlock")
        if "r1" in wanted:
            results["r1"] = scenario_r1(api, adapter, engine)
        if "r2" in wanted:
            results["r2"] = scenario_r2(api, adapter, engine)
        if "r3" in wanted:
            results["r3"] = scenario_r3(api, adapter, engine)
        if "r4" in wanted:
            results["r4"] = scenario_r4(api, adapter, engine)
        if "r5" in wanted:
            results["r5"] = scenario_r5(api, adapter, engine)
        if "stress" in wanted:
            results["stress"] = scenario_stress(api, adapter, engine, n=args.stress_n)
        if "deadlock" in wanted:
            results["deadlock"] = scenario_deadlock(api, adapter, engine, n=args.deadlock_n)
        save_evidence(engine, "summary.json", {"results": results})
        print(json.dumps({"scenarios": results}, indent=2, default=str))
        return 0
    finally:
        adapter.close()
        api.close()


if __name__ == "__main__":
    raise SystemExit(main())
