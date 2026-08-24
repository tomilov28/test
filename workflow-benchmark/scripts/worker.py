"""External Python worker for the benchmark.

Picks up WorkItems surfaced by the reconciler for a request and completes them
through OUR API (POST /work-items/{id}/complete -> transactional outbox ->
engine via adapter). All business logic lives here, never in the engine.

Usage:
  .venv/bin/python -m scripts.worker --engine OPERATON --create
  .venv/bin/python -m scripts.worker --request-id <uuid>
"""

import argparse
import sys
import time
import uuid

import httpx

API_BASE = "http://localhost:8000"


def decide(task_definition_key: str, work_item: dict) -> dict:
    """Simulated business logic for the credit-decision demo process."""
    if task_definition_key == "review_request":
        return {"review": "PASS", "score": 720, "notes": "automated pre-check"}
    if task_definition_key == "final_approval":
        return {"decision": "APPROVE", "limit": 5000, "currency": "USD"}
    return {"result": "ACK", "task": task_definition_key}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default=API_BASE)
    parser.add_argument("--engine", choices=["OPERATON", "FLOWABLE"], default="OPERATON")
    parser.add_argument("--request-id", type=uuid.UUID, default=None)
    parser.add_argument("--create", action="store_true", help="create a request first")
    parser.add_argument("--quiet-seconds", type=float, default=8.0)
    args = parser.parse_args()

    client = httpx.Client(base_url=args.api, timeout=15.0)

    if args.request_id is None and args.create:
        resp = client.post(
            "/requests",
            json={
                "request_type": "credit_decision",
                "request_type_version": 1,
                "workflow_engine": args.engine,
                "variables": {"amount": 1200, "currency": "USD"},
            },
        )
        resp.raise_for_status()
        args.request_id = uuid.UUID(resp.json()["id"])
        print(f"created request {args.request_id} (engine={args.engine})")
    if args.request_id is None:
        print("provide --request-id or --create", file=sys.stderr)
        return 2

    last_change = time.time()
    completed = 0
    while time.time() - last_change < args.quiet_seconds:
        req = client.get(f"/requests/{args.request_id}").json()
        if req["lifecycle_state"] == "CLOSED":
            print(f"request closed (outcome={req['outcome']})")
            break
        items = client.get(f"/requests/{args.request_id}/work-items").json()
        active = [wi for wi in items if wi["state"] == "ACTIVE"]
        for wi in active:
            payload = decide(wi["task_definition_key"], wi)
            resp = client.post(
                f"/work-items/{wi['id']}/complete",
                json={"data": payload, "version": 1},
            )
            resp.raise_for_status()
            completed += 1
            last_change = time.time()
            print(f"completed work_item {wi['task_definition_key']} -> {payload}")
        time.sleep(1.0)

    print(f"worker finished: {completed} work items completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
