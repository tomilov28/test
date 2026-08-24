"""Demo scenario: drive the harness API and print what happened.

Phase 1: exercises request creation + outbox command flow. The engine phases
will extend it to complete work items end-to-end.

Usage:  .venv/bin/python -m scripts.demo --engine OPERATON
"""

import argparse
import sys

import httpx

API_BASE = "http://localhost:8000"


def _get(path: str, client: httpx.Client) -> dict:
    resp = client.get(f"{API_BASE}{path}")
    resp.raise_for_status()
    return resp.json()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=["OPERATON", "FLOWABLE"], default="OPERATON")
    parser.add_argument("--api", default=API_BASE)
    args = parser.parse_args()

    client = httpx.Client(base_url=args.api, timeout=15.0)
    try:
        health = _get("/health", client)
        print(f"health: {health}")

        body = {
            "request_type": "credit_decision",
            "request_type_version": 1,
            "workflow_engine": args.engine,
            "variables": {"amount": 1000, "currency": "USD"},
        }
        resp = client.post("/requests", json=body)
        resp.raise_for_status()
        created = resp.json()
        print(f"created request: {created['number']} engine={created['workflow_engine']} id={created['id']}")

        req = _get(f"/requests/{created['id']}", client)
        print(f"request state: {req['lifecycle_state']} outcome={req['outcome']}")

        commands = _get(f"/requests/{created['id']}/commands", client)
        for cmd in commands:
            print(
                f"command: type={cmd['command_type']} state={cmd['state']} "
                f"attempts={cmd['attempts']} error={cmd['last_error']}"
            )

        print("demo complete")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
