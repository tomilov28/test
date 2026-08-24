"""Deploy BPMN fixtures to an engine.

Idempotent: uses the engine's duplicate filtering / changed-only semantics.

Usage:
  .venv/bin/python -m scripts.deploy_fixtures --engine OPERATON
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.workflow.flowable import FlowableAdapter  # noqa: E402
from app.workflow.operaton import OperatonAdapter  # noqa: E402

FIXTURES = {
    "OPERATON": ("bpmn/operaton", OperatonAdapter),
    "FLOWABLE": ("bpmn/flowable", FlowableAdapter),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=list(FIXTURES), required=True)
    args = parser.parse_args()

    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dirname, adapter_cls = FIXTURES[args.engine]
    fixtures_dir = os.path.join(root_dir, dirname)
    if not os.path.isdir(fixtures_dir):
        print(f"no fixtures dir: {fixtures_dir}")
        return 1

    adapter = adapter_cls()
    try:
        for entry in sorted(os.listdir(fixtures_dir)):
            if not entry.endswith(".bpmn"):
                continue
            process_key = os.path.splitext(entry)[0]
            with open(os.path.join(fixtures_dir, entry)) as fh:
                xml = fh.read()
            info = adapter.deploy_process(xml, process_key, name=f"benchmark-fixture-{process_key}")
            print(f"deployed {entry} -> deployment {info.deployment_id}")
        return 0
    finally:
        adapter.close()


if __name__ == "__main__":
    raise SystemExit(main())
