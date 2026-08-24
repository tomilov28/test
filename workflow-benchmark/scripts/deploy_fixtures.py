"""Deploy BPMN fixtures to an engine.

Process key is read from the BPMN XML (`<bpmn:process id="...">`), so multiple
files may carry the same process key (e.g. LONG_VISIT_POC v1 and v2).

Usage:
  .venv/bin/python -m scripts.deploy_fixtures --engine OPERATON
  .venv/bin/python -m scripts.deploy_fixtures --engine OPERATON --reset
"""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.workflow.flowable import FlowableAdapter  # noqa: E402
from app.workflow.operaton import OperatonAdapter  # noqa: E402

FIXTURES = {
    "OPERATON": ("bpmn/operaton", OperatonAdapter),
    "FLOWABLE": ("bpmn/flowable", FlowableAdapter),
}

_PROCESS_ID_RE = re.compile(r'<bpmn:process\s+id="([^"]+)"')


def process_key_from_xml(xml: str, fallback: str) -> str:
    match = _PROCESS_ID_RE.search(xml)
    return match.group(1) if match else fallback


def _reset_process_key(adapter, process_key: str) -> None:
    if not hasattr(adapter, "get_running_instances") or not hasattr(adapter, "delete_deployment"):
        print(f"  (skip reset for {process_key}: adapter has no reset support)")
        return
    for instance_id in adapter.get_running_instances(process_key):
        adapter.cancel_process(instance_id)
    for definition in adapter.get_process_definitions(process_key):
        adapter.delete_deployment(definition["deploymentId"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=list(FIXTURES), required=True)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="cancel instances + delete existing deployments of each process key first",
    )
    args = parser.parse_args()

    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dirname, adapter_cls = FIXTURES[args.engine]
    fixtures_dir = os.path.join(root_dir, dirname)
    if not os.path.isdir(fixtures_dir):
        print(f"no fixtures dir: {fixtures_dir}")
        return 1

    adapter = adapter_cls()
    try:
        keys: set[str] = set()
        for entry in sorted(os.listdir(fixtures_dir)):
            if not entry.endswith(".bpmn"):
                continue
            with open(os.path.join(fixtures_dir, entry)) as fh:
                xml = fh.read()
            process_key = process_key_from_xml(xml, fallback=os.path.splitext(entry)[0])
            keys.add(process_key)
        if args.reset:
            for process_key in sorted(keys):
                _reset_process_key(adapter, process_key)
        for entry in sorted(os.listdir(fixtures_dir)):
            if not entry.endswith(".bpmn"):
                continue
            with open(os.path.join(fixtures_dir, entry)) as fh:
                xml = fh.read()
            process_key = process_key_from_xml(xml, fallback=os.path.splitext(entry)[0])
            info = adapter.deploy_process(xml, process_key, name=f"benchmark-fixture-{entry}")
            print(f"deployed {entry} -> deployment {info.deployment_id} (key={process_key})")
        return 0
    finally:
        adapter.close()


if __name__ == "__main__":
    raise SystemExit(main())
