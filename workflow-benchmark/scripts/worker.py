"""External Python worker for the benchmark.

Two responsibilities, both on the "business logic" side of the ownership
boundary (never inside the engine):

1. External Task worker: fetches-and-locks Operaton external tasks on topic
   `load_prisoner_data` (simulated remote service) and completes them. With
   --fail-first the first invocation reports a technical failure, exercising
   Operaton's real retry mechanism.
2. Human task completer: picks up WorkItems surfaced by the reconciler for a
   request and completes them through OUR API (POST /work-items/{id}/complete
   -> transactional outbox -> engine via adapter).

Usage:
  .venv/bin/python -m scripts.worker --engine OPERATON --create --fail-first
  .venv/bin/python -m scripts.worker --engine OPERATON --request-id <uuid>
  .venv/bin/python -m scripts.worker --engine OPERATON --daemon
"""

import argparse
import sys
import time
import uuid

import httpx

from app.workers.external_worker import ExternalTaskWorker

API_BASE = "http://localhost:8000"
ENGINE_BASE = "http://localhost:8080/engine-rest"

LONG_VISIT_TOPIC = "load_prisoner_data"

# Human-task decision payloads for the LONG_VISIT_POC process.
HUMAN_DECISIONS = {
    "finance_check": {"result": "OK", "balance_sufficient": True},
    "relative_check": {"result": "OK", "relatives_verified": True},
    "discipline_check": {"result": "OK", "no_violations": True},
    "final_decision": {"decision": "APPROVE", "reason": "all checks passed"},
}

FALLBACK_DECISION = {"result": "ACK", "task": "unknown"}


def decide(task_definition_key: str, work_item: dict) -> dict:
    return HUMAN_DECISIONS.get(task_definition_key, FALLBACK_DECISION)


def complete_active_work_items(client: httpx.Client, request_id: uuid.UUID) -> int:
    req = client.get(f"/requests/{request_id}").json()
    if req["lifecycle_state"] == "CLOSED":
        return 0
    items = client.get(f"/requests/{request_id}/work-items").json()
    completed = 0
    for wi in items:
        if wi["state"] != "ACTIVE":
            continue
        payload = decide(wi["task_definition_key"], wi)
        resp = client.post(
            f"/work-items/{wi['id']}/complete",
            json={"data": payload, "version": 1},
        )
        resp.raise_for_status()
        completed += 1
        print(f"completed work_item {wi['task_definition_key']} -> {payload}")
    return completed


def run_request_flow(args) -> int:
    """One-shot flow for a specific (or freshly created) request."""
    client = httpx.Client(base_url=args.api, timeout=15.0)
    ext = ExternalTaskWorker(engine_url=args.engine_url, fail_first=args.fail_first)
    request_id = args.request_id

    try:
        if request_id is None and args.create:
            resp = client.post(
                "/requests",
                json={
                    "request_type": "LONG_VISIT_POC",
                    "request_type_version": 1,
                    "workflow_engine": args.engine,
                    "variables": {"initiator": "worker-demo"},
                },
            )
            resp.raise_for_status()
            request_id = uuid.UUID(resp.json()["id"])
            print(f"created request {request_id} (engine={args.engine})")
        if request_id is None:
            print("provide --request-id or --create", file=sys.stderr)
            return 2

        last_change = time.time()
        completed_items = 0
        while time.time() - last_change < args.quiet_seconds:
            # 1) external service task
            ext.poll_once()
            # 2) reconciler-surfaced human tasks through our API
            completed_items += complete_active_work_items(client, request_id)

            req = client.get(f"/requests/{request_id}").json()
            if req["lifecycle_state"] == "CLOSED":
                print(f"request closed (outcome={req['outcome']})")
                break
            if ext.results["completed"] or completed_items:
                last_change = time.time()
            time.sleep(1.0)

        req = client.get(f"/requests/{request_id}").json()
        print(
            f"worker finished: external_tasks={ext.results} "
            f"work_items_completed={completed_items} request_state={req['lifecycle_state']}"
        )
        return 0
    finally:
        ext.close()
        client.close()


def run_daemon(args) -> int:
    """Long-running worker: keep both the external topic and all active
    requests' human tasks drained."""
    client = httpx.Client(base_url=args.api, timeout=15.0)
    ext = ExternalTaskWorker(engine_url=args.engine_url, fail_first=args.fail_first)
    try:
        while True:
            ext.poll_once()
            reqs = client.get("/requests").json() if args.poll_requests else []
            for req in reqs:
                if req["lifecycle_state"] != "ACTIVE":
                    continue
                try:
                    complete_active_work_items(client, uuid.UUID(req["id"]))
                except Exception as exc:  # request may close between list and complete
                    print(f"worker: request {req['id']} -> {exc}")
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("worker daemon stopped")
        return 0
    finally:
        ext.close()
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default=API_BASE)
    parser.add_argument("--engine-url", default=ENGINE_BASE)
    parser.add_argument("--engine", choices=["OPERATON", "FLOWABLE"], default="OPERATON")
    parser.add_argument("--request-id", type=uuid.UUID, default=None)
    parser.add_argument("--create", action="store_true", help="create a LONG_VISIT_POC request first")
    parser.add_argument("--daemon", action="store_true", help="run forever")
    parser.add_argument("--fail-first", action="store_true",
                        help="external task test mode: first execution fails (technical failure)")
    parser.add_argument("--quiet-seconds", type=float, default=8.0)
    parser.add_argument("--poll-requests", action="store_true",
                        help="daemon: also poll /requests for active human tasks")
    args = parser.parse_args()

    if args.daemon:
        return run_daemon(args)
    return run_request_flow(args)


if __name__ == "__main__":
    raise SystemExit(main())
