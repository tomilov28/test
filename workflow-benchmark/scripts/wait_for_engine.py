"""Wait until an engine REST API is reachable. Used by make up-operaton/up-flowable.

Operaton: GET {base}/engine  (engine-rest)
Flowable: GET {base}/management/engine  (flowable-rest)
"""

import argparse
import os
import sys
import time

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_settings  # noqa: E402

ENGINE_PROBES = {
    "OPERATON": ("engine_operaton_url", "/engine"),
    "FLOWABLE": ("engine_flowable_url", "/management/engine"),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", choices=list(ENGINE_PROBES), required=True)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    settings = get_settings()
    setting_name, probe_path = ENGINE_PROBES[args.engine]
    base = getattr(settings, setting_name).rstrip("/")

    deadline = time.time() + args.timeout
    last_error = None
    while time.time() < deadline:
        try:
            resp = httpx.get(f"{base}{probe_path}", timeout=5.0)
            if resp.status_code < 500:
                print(f"engine {args.engine} ready ({resp.status_code}) at {base}")
                return 0
            last_error = f"HTTP {resp.status_code}"
        except Exception as exc:
            last_error = str(exc)
        time.sleep(3)

    print(f"engine {args.engine} not ready in {args.timeout}s: {last_error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
