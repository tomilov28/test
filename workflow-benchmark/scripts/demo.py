"""Operaton demo: drive a LONG_VISIT_POC request end-to-end and print the result.

Assumes `make up-operaton` (full stack incl. worker daemon) is running: the
daemon worker fetches the external task and completes human tasks through the
harness API, so the request flows to COMPLETED on its own.

Prints, at the end:
  * Swagger UI URL + API base
  * Operaton Cockpit / Tasklist UI URLs + demo/demo credentials
  * the demo Request number + process instance id
  * the process lifecycle summary

Usage:  .venv/bin/python -m scripts.demo --engine OPERATON
"""

import argparse
import sys
import time

import httpx

API_BASE = "http://localhost:8000"
ENGINE_BASE = "http://localhost:8080/engine-rest"
REQUEST_TYPE = "LONG_VISIT_POC"

UI_COCKPIT = "http://localhost:8080/operaton/app/cockpit/"
UI_TASKLIST = "http://localhost:8080/operaton/app/tasklist/"
UI_SWAGGER = "http://localhost:8080/engine-rest/swaggerui/"


def _get(path: str, client: httpx.Client) -> dict:
    resp = client.get(f"{API_BASE}{path}")
    resp.raise_for_status()
    return resp.json()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=["OPERATON", "FLOWABLE"], default="OPERATON")
    parser.add_argument("--api", default=API_BASE)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    client = httpx.Client(base_url=args.api, timeout=20.0)
    try:
        health = _get("/health", client)
        print(f"health: {health}")

        body = {
            "request_type": REQUEST_TYPE,
            "request_type_version": 1,
            "workflow_engine": args.engine,
            "variables": {"initiator": "operaton-demo", "amount": 1000, "currency": "USD"},
        }
        resp = client.post("/requests", json=body)
        resp.raise_for_status()
        created = resp.json()
        request_id = created["id"]
        print(f"created request: {created['number']} id={request_id} engine={args.engine}")

        # wait for the daemon worker to complete the whole process (incl. 15s timer)
        deadline = time.time() + args.timeout
        last_req = None
        while time.time() < deadline:
            req = _get(f"/requests/{request_id}", client)
            last_req = req
            if req["lifecycle_state"] == "CLOSED":
                break
            time.sleep(2.0)
        else:
            print("timed out waiting for the request to close", file=sys.stderr)
            print(f"last state: {last_req and last_req['lifecycle_state']}")
            return 1

        req = _get(f"/requests/{request_id}", client)
        items = _get(f"/requests/{request_id}/work-items", client)
        commands = _get(f"/requests/{request_id}/commands", client)

        print("\n=== result ===")
        print(f"request number      : {req['number']}")
        print(f"request id          : {req['id']}")
        print(f"workflow_instance_id: {req['workflow_instance_id']}")
        print(f"state / outcome     : {req['lifecycle_state']} / {req['outcome']}")
        print(f"work items          : {len(items)}")
        for wi in items:
            print(f"  - {wi['task_definition_key']:<18} {wi['state']}")
        print(f"commands            : {[(c['command_type'], c['state']) for c in commands]}")

        print("\n=== URLs ===")
        print(f"Swagger UI          : {UI_SWAGGER}")
        print(f"Operaton Cockpit    : {UI_COCKPIT}")
        print(f"Operaton Tasklist   : {UI_TASKLIST}")
        print(f"Credentials         : demo / demo")
        print("\ndemo complete")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
