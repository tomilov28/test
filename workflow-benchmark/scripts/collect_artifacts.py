"""Collect benchmark artifacts under artifacts/<engine>/.

Writes:
  * engine.log        - docker logs of the engine container
  * docker-stats.json - docker stats snapshot of the running stack containers
  * benchmark.json    - resource/metrics snapshot (versions, image sizes, RAM,
                        startup time, adapter LOC, BPMN extensions) plus key
                        engine REST responses under api-evidence/

Usage:
    .venv/bin/python -m scripts.collect_artifacts --engine OPERATON
    .venv/bin/python -m scripts.collect_artifacts --engine FLOWABLE
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROCESS_KEY = "LONG_VISIT_POC"

# Engine-specific settings: artifact dir, container name, image filter, REST
# base + auth, adapter files and BPMN fixtures used for the metrics snapshot.
ENGINES = {
    "OPERATON": {
        "name": "Operaton",
        "version": "2.1.4",
        "artifacts": os.path.join(ROOT, "artifacts", "operaton"),
        "container": "benchmark-operaton",
        "image": "operaton/operaton",
        "base": "http://localhost:8080/engine-rest",
        "auth": None,
        "adapter_loc_files": [
            "app/workflow/operaton.py",
            "app/workers/external_worker.py",
        ],
        "bpmn_files": [
            "bpmn/operaton/long_visit_poc.bpmn",
            "bpmn/operaton/long_visit_poc_v2.bpmn",
        ],
        "ns_re": r"(xmlns:(camunda|operaton)=)",
        "attr_re": r"(camunda|operaton):[a-zA-Z]+=",
        "deployment_name_like": "benchmark-fixture",
    },
    "FLOWABLE": {
        "name": "Flowable",
        "version": "8.0.0",
        "artifacts": os.path.join(ROOT, "artifacts", "flowable"),
        "container": "benchmark-flowable",
        "image": "flowable/flowable-rest",
        "base": "http://localhost:8081/flowable-rest/service",
        "external_base": "http://localhost:8081/flowable-rest/external-job-api",
        "auth": ("rest-admin", "test"),
        "adapter_loc_files": [
            "app/workflow/flowable.py",
            "app/workers/flowable_worker.py",
        ],
        "bpmn_files": [
            "bpmn/flowable/long_visit_v1.bpmn",
            "bpmn/flowable/long_visit_v2.bpmn",
        ],
        "ns_re": r"(xmlns:(flowable)=)",
        "attr_re": r"flowable:[a-zA-Z]+=",
        "deployment_name_like": "%benchmark-fixture%",
    },
}


def sh(args: list[str], **kw) -> str:
    return subprocess.run(args, capture_output=True, text=True, **kw).stdout.strip()


def line_count(path: str) -> int:
    with open(os.path.join(ROOT, path)) as fh:
        return sum(1 for _ in fh)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=list(ENGINES), default="OPERATON")
    args = parser.parse_args()
    cfg = ENGINES[args.engine]

    artifacts = cfg["artifacts"]
    evidence_dir = os.path.join(artifacts, "api-evidence")
    os.makedirs(artifacts, exist_ok=True)
    os.makedirs(evidence_dir, exist_ok=True)
    collected_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # 1. engine log
    engine_log = sh(["docker", "logs", cfg["container"]])
    with open(os.path.join(artifacts, "engine.log"), "w") as fh:
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
    with open(os.path.join(artifacts, "docker-stats.json"), "w") as fh:
        json.dump({"collected_at": collected_at, "containers": stats}, fh, indent=2)

    # 3. image info (size)
    images = subprocess.run(
        ["docker", "images", cfg["image"], "--format", "{{json .}}"],
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
    adapter_loc = {p: line_count(p) for p in cfg["adapter_loc_files"]}
    bpmn_extensions = {}
    for path in cfg["bpmn_files"]:
        with open(os.path.join(ROOT, path)) as fh:
            xml = fh.read()
        extensions = {
            "vendor_namespace_declarations": len(re.findall(cfg["ns_re"], xml)),
            "vendor_attribute_usages": len(re.findall(cfg["attr_re"], xml)),
        }
        bpmn_extensions[path] = extensions

    # 6. engine REST evidence snapshots
    evidence = {}
    client_kwargs = {"base_url": cfg["base"], "timeout": 15.0}
    if cfg["auth"]:
        client_kwargs["auth"] = cfg["auth"]
    with httpx.Client(**client_kwargs) as client:
        evidence["process_definitions"] = client.get(
            "/repository/process-definitions",
            params={"key": PROCESS_KEY, "size": 100},
        ).json().get("data", [])
        deployments = client.get(
            "/repository/deployments",
            params={"nameLike": cfg["deployment_name_like"], "size": 100},
        ).json().get("data", [])
        evidence["deployments_count"] = len(deployments)
        evidence["running_instances"] = client.get(
            "/runtime/process-instances",
            params={"processDefinitionKey": PROCESS_KEY, "size": 100},
        ).json().get("data", [])
        evidence["engine_info"] = client.get("/management/engine").json()

    # external-task topics: Operaton exposes /external-task/topic-names; Flowable
    # has no topic listing endpoint, so record the external-job rows instead.
    if cfg["auth"]:
        with httpx.Client(base_url=cfg["external_base"], auth=cfg["auth"], timeout=15.0) as ext:
            evidence["external_jobs"] = ext.get("/jobs", params={"size": 100}).json().get("data", [])
    else:
        with httpx.Client(base_url=cfg["base"], timeout=15.0) as client:
            evidence["external_task_topics"] = client.get("/external-task/topic-names").json()

    benchmark = {
        "collected_at": collected_at,
        "engine": {"name": cfg["name"], "version": cfg["version"], "base": cfg["base"]},
        "images": image_rows,
        "containers": len(stats),
        "docker_stats": stats,
        "engine_boot_seconds": boot_seconds,
        "adapter_loc": adapter_loc,
        "adapter_loc_total": sum(adapter_loc.values()),
        "bpmn_vendor_extensions": bpmn_extensions,
        "evidence": evidence,
    }
    with open(os.path.join(artifacts, "benchmark.json"), "w") as fh:
        json.dump(benchmark, fh, indent=2, default=str)

    with open(os.path.join(evidence_dir, "process-definitions.json"), "w") as fh:
        json.dump(evidence["process_definitions"], fh, indent=2)
    with open(os.path.join(evidence_dir, "running-instances.json"), "w") as fh:
        json.dump(evidence["running_instances"], fh, indent=2)
    if "external_task_topics" in evidence:
        with open(os.path.join(evidence_dir, "external-task-topics.json"), "w") as fh:
            json.dump(evidence["external_task_topics"], fh, indent=2)
    if "external_jobs" in evidence:
        with open(os.path.join(evidence_dir, "external-jobs.json"), "w") as fh:
            json.dump(evidence["external_jobs"], fh, indent=2)

    print(f"engine.log bytes   : {len(engine_log)}")
    print(f"containers statted : {len(stats)}")
    print(f"engine boot seconds: {boot_seconds}")
    print(f"adapter LOC total  : {sum(adapter_loc.values())}")
    print(f"artifacts written under {artifacts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
