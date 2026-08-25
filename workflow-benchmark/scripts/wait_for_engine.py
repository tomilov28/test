"""Wait until an engine REST API is reachable AND its process engine is ready.

A bare reachability probe is not enough: on a freshly recreated engine database
Operaton's `/engine` endpoint answers 200 before the engine tables exist, so a
follow-up process-engine query (e.g. `/process-instance`) can still 500 with
`relation "act_ru_job" does not exist`. This script therefore probes a
process-engine-backed endpoint and only reports ready once it answers 2xx.

Used by make up-operaton / up-flowable and the *_infra targets.

Probes:
  OPERATON: GET {base}/process-definition?firstResult=0&maxResults=1
  FLOWABLE: GET {base}/repository/process-definitions?size=1 (rest-admin/test;
            engine_flowable_url already includes the /service segment)
"""

import argparse
import os
import sys
import time

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_settings  # noqa: E402

ENGINE_PROBES = {
    "OPERATON": ("engine_operaton_url", "/process-definition", None),
    "FLOWABLE": (
        "engine_flowable_url",
        "/repository/process-definitions",
        ("rest-admin", "test"),
    ),
}
PROBE_PARAMS = {
    "OPERATON": {"firstResult": 0, "maxResults": 1},
    "FLOWABLE": {"size": 1},
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", choices=list(ENGINE_PROBES), required=True)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    setting_name, probe_path, auth = ENGINE_PROBES[args.engine]
    settings = get_settings()
    base = getattr(settings, setting_name).rstrip("/")

    deadline = time.time() + args.timeout
    last_error = None
    while time.time() < deadline:
        try:
            resp = httpx.get(
                f"{base}{probe_path}",
                params=PROBE_PARAMS[args.engine],
                auth=auth,
                timeout=5.0,
            )
            if resp.status_code < 300:
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
