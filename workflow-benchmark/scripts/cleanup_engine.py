"""Reset an engine to a clean state for benchmark runs.

Deletes all running process instances and all deployments except the ones
whose key starts with --keep. Safe to re-run.

Usage:
  .venv/bin/python -m scripts.cleanup_engine --engine OPERATON --keep credit_decision
"""

import argparse
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_settings  # noqa: E402

ENGINE_BASE = {
    "OPERATON": ("engine_operaton_url", None),
    "FLOWABLE": ("engine_flowable_url", "/management/engine"),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=list(ENGINE_BASE), required=True)
    parser.add_argument("--keep", default="credit_decision", help="deployment name prefix to keep")
    parser.add_argument("--also-clear-history", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    base_setting, probe = ENGINE_BASE[args.engine]
    base = getattr(settings, base_setting).rstrip("/")

    # ---- cancel all running instances -------------------------------------
    instances = httpx.get(f"{base}/process-instance").json()
    for inst in instances:
        httpx.delete(f"{base}/process-instance/{inst['id']}", params={"failIfNotExists": "false"})
    print(f"cancelled {len(instances)} running instances")

    # ---- delete deployments (keep fixture) --------------------------------
    deployments = httpx.get(f"{base}/deployment").json()
    kept = 0
    for dep in deployments:
        name = dep.get("name") or ""
        if name.startswith(args.keep) or args.keep in name:
            kept += 1
            continue
        httpx.delete(f"{base}/deployment/{dep['id']}?cascade=true")
    print(f"deleted {len(deployments) - kept} deployments, kept {kept}")

    # ---- optionally clear history ------------------------------------------
    if args.also_clear_history:
        try:
            hist = httpx.get(f"{base}/history/process-instance").json()
            for h in hist:
                httpx.delete(f"{base}/history/process-instance/{h['id']}")
            print(f"deleted {len(hist)} historic process instances")
        except Exception as exc:
            print(f"history cleanup skipped: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
