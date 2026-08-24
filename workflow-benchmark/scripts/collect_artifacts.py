"""Collect Operaton benchmark artifacts under artifacts/operaton/.

Writes:
  * engine.log        - docker logs of the Operaton container
  * docker-stats.json - docker stats snapshot of the operaton stack containers
  * benchmark.json    - resource/metrics snapshot (versions, image sizes, RAM,
                        startup time, adapter LOC, BPMN extensions) plus key
                        engine REST responses under api-evidence/

Usage:  .venv/bin/python -m scripts.collect_artifacts
"""

import datetime
import json
import os
import re
import subprocess
import sys

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARTIFACTS = os.path.join(ROOT, "artifacts", "operaton")
EVIDENCE_DIR = os.path.join(ARTIFACTS, "api-evidence")
ENGINE_BASE = "http://localhost:8080/engine-rest"
PROCESS_KEY = "LONG_VISIT_POC"

ADAPTER_LOC_FILES = [
    "app/workflow/operaton.py",
    "app/workers/external_worker.py",
]
BPMN_FILES = [
    "bpmn/operaton/long_visit_poc.bpmn",
    "bpmn/operaton/long_visit_poc_v2.bpmn",
]


def sh(args: list[str], **kw) -> str:
    return subprocess.run(args, capture_output=True, text=True, **kw).stdout.strip()


def line_count(path: str) -> int:
    with open(os.path.join(ROOT, path)) as fh:
        return sum(1 for _ in fh)


def main() -> int:
    os.makedirs(ARTIFACTS, exist_ok=True)
    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    collected_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # 1. engine log
    engine_log = sh(["docker", "logs", "benchmark-operaton"])
    with open(os.path.join(ARTIFACTS, "engine.log"), "w") as fh:
        fh.write(engine_log)
        fh.write("\n")

    # 2. docker stats snapshot
    stats_out = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
        capture_output=True,
        text=True,
    ).stdout
    stats = []
    for line in stats_out.splitlines():
        if line.strip():
            try:
                stats.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    with open(os.path.join(ARTIFACTS, "docker-stats.json"), "w") as fh:
        json.dump({"collected_at": collected_at, "containers": stats}, fh, indent=2)

    # 3. image info (size)
    images = subprocess.run(
        ["docker", "images", "operaton/operaton", "--format", "{{json .}}"],
        capture_output=True,
        text=True,
    ).stdout
    image_rows = []
    for line in images.splitlines():
        if line.strip():
            try:
                image_rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # 4. engine boot duration from log ("Started ... in N seconds")
    started_match = re.search(r"Started\s+\S+\s+in\s+([\d.]+)\s+seconds", engine_log)
    boot_seconds = float(started_match.group(1)) if started_match else None

    # 5. adapter LOC + BPMN extensions
    adapter_loc = {p: line_count(p) for p in ADAPTER_LOC_FILES}
    bpmn_extensions = {}
    for path in BPMN_FILES:
        with open(os.path.join(ROOT, path)) as fh:
            xml = fh.read()
        extensions = {
            "vendor_namespace_declarations": xml.count("xmlns:camunda=")
            + xml.count("xmlns:operaton="),
            "camunda_attribute_usages": len(re.findall(r"camunda:[a-zA-Z]+=", xml)),
            "operaton_attribute_usages": len(re.findall(r"operaton:[a-zA-Z]+=", xml)),
        }
        bpmn_extensions[path] = extensions

    # 6. engine REST evidence snapshots
    evidence = {}
    with httpx.Client(base_url=ENGINE_BASE, timeout=15.0) as client:
        evidence["process_definitions"] = client.get(
            "/process-definition", params={"key": PROCESS_KEY}
        ).json()
        evidence["deployments_count"] = len(
            client.get("/deployment", params={"nameLike": "benchmark-fixture"}).json()
        )
        evidence["running_instances"] = client.get(
            "/process-instance", params={"processDefinitionKey": PROCESS_KEY}
        ).json()
        evidence["external_task_topics"] = client.get("/external-task/topic-names").json()
        evidence["engine_info"] = client.get("/engine").json()

    benchmark = {
        "collected_at": collected_at,
        "engine": {"name": "Operaton", "version": "2.1.4", "base": ENGINE_BASE},
        "images": image_rows,
        "containers": len(stats),
        "docker_stats": stats,
        "engine_boot_seconds": boot_seconds,
        "adapter_loc": adapter_loc,
        "adapter_loc_total": sum(adapter_loc.values()),
        "bpmn_vendor_extensions": bpmn_extensions,
        "evidence": evidence,
    }
    with open(os.path.join(ARTIFACTS, "benchmark.json"), "w") as fh:
        json.dump(benchmark, fh, indent=2, default=str)

    with open(os.path.join(EVIDENCE_DIR, "process-definitions.json"), "w") as fh:
        json.dump(evidence["process_definitions"], fh, indent=2)
    with open(os.path.join(EVIDENCE_DIR, "running-instances.json"), "w") as fh:
        json.dump(evidence["running_instances"], fh, indent=2)
    with open(os.path.join(EVIDENCE_DIR, "external-task-topics.json"), "w") as fh:
        json.dump(evidence["external_task_topics"], fh, indent=2)

    print(f"engine.log bytes   : {len(engine_log)}")
    print(f"containers statted : {len(stats)}")
    print(f"engine boot seconds: {boot_seconds}")
    print(f"adapter LOC total  : {sum(adapter_loc.values())}")
    print(f"artifacts written under {ARTIFACTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
