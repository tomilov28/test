"""Collect benchmark artifacts under artifacts/<engine>/.

Writes:
  * engine.log        - docker logs of the engine container
  * docker-stats.json - docker stats snapshot of the running stack containers
  * benchmark.json    - resource/metrics snapshot (versions, image size + ID +
                        RepoDigest, RAM, startup time, adapter LOC, BPMN
                        extensions) plus key engine REST responses under
                        api-evidence/

REST evidence is collected with ENGINE-SPECIFIC collectors (audit A06): Operaton
speaks the Camunda-style API (no /repository or /runtime prefixes, /engine
instead of /management/engine); Flowable uses /repository + /runtime under
/service. Flowable evidence must keep working unchanged.

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


def _image_evidence(image_ref: str) -> list[dict]:
    """Authoritative image pinning (audit A12): record the image ID, RepoDigest
    and tag of the actually running image, straight from docker image inspect,
    not just versions.env."""
    rows = []
    try:
        out = subprocess.run(
            ["docker", "image", "inspect", image_ref], capture_output=True, text=True
        ).stdout
        for line in [out]:
            data = json.loads(line)
            for img in data:
                rows.append(
                    {
                        "ref": image_ref,
                        "id": img.get("Id"),
                        "repo_digests": img.get("RepoDigests", []),
                        "created": img.get("Created"),
                        "size_bytes": img.get("Size"),
                        "config_entrypoint": img.get("Config", {}).get("Entrypoint"),
                    }
                )
    except Exception as exc:  # image may not be present locally
        rows.append({"ref": image_ref, "error": str(exc)})
    return rows


def _sh(args: list[str], **kw) -> str:
    return subprocess.run(args, capture_output=True, text=True, **kw).stdout.strip()


def _line_count(path: str) -> int:
    with open(os.path.join(ROOT, path)) as fh:
        return sum(1 for _ in fh)


def _get_json(client: httpx.Client, path: str, params: dict | None = None, key: str = "data"):
    """GET a collection endpoint; return the list payload. If `key` is given the
    response is expected to be an object with that list key (Flowable paged
    shape); otherwise the whole response is a list (Operaton shape)."""
    resp = client.get(path, params=params)
    if resp.status_code in (404, 405):
        return {"error": f"HTTP {resp.status_code} for {path}"}
    resp.raise_for_status()
    body = resp.json()
    if key is not None and isinstance(body, dict) and key in body:
        return body.get(key, [])
    if isinstance(body, dict) and "error" in body:
        return body
    return body if isinstance(body, list) else body


# ---- engine-specific REST collectors (audit A06) -----------------------------


def collect_operaton(client: httpx.Client, external_client: httpx.Client | None = None) -> dict:
    return {
        "process_definitions": _get_json(
            client, "/process-definition", {"key": PROCESS_KEY, "firstResult": 0, "maxResults": 100}, key=None
        ),
        "all_process_definitions": _get_json(
            client, "/process-definition", {"firstResult": 0, "maxResults": 200}, key=None
        ),
        "deployments": _get_json(
            client, "/deployment", {"nameLike": "benchmark-fixture", "firstResult": 0, "maxResults": 100}, key=None
        ),
        "running_instances": _get_json(
            client,
            "/process-instance",
            {"processDefinitionKey": PROCESS_KEY, "firstResult": 0, "maxResults": 100},
            key=None,
        ),
        "engine_info": _get_json(client, "/engine", key=None),
        "incidents": _get_json(
            client, "/incident", {"firstResult": 0, "maxResults": 100}, key=None
        ),
        "failed_jobs": _get_json(client, "/job", {"firstResult": 0, "maxResults": 100}, key=None),
        "external_tasks": _get_json(
            client, "/external-task", {"firstResult": 0, "maxResults": 100}, key=None
        ),
        "external_task_topics": _get_json(client, "/external-task/topic-names", key=None),
    }


def collect_flowable(client: httpx.Client, external_client: httpx.Client | None) -> dict:
    # The Flowable REST client base already includes the /service prefix
    # (ENGINES["FLOWABLE"]["base"]); do not add it a second time.
    service = ""
    evidence = {
        "process_definitions": _get_json(
            client, f"{service}/repository/process-definitions", {"key": PROCESS_KEY, "size": 100}
        ),
        "deployments": _get_json(
            client, f"{service}/repository/deployments", {"nameLike": "%benchmark-fixture%", "size": 100}
        ),
        "running_instances": _get_json(
            client, f"{service}/runtime/process-instances", {"processDefinitionKey": PROCESS_KEY, "size": 100}
        ),
        "engine_info": _get_json(client, f"{service}/management/engine", key=None),
        "deadletter_jobs": _get_json(
            client, f"{service}/management/deadletter-jobs", {"size": 100}
        ),
        "timer_jobs": _get_json(client, f"{service}/management/timer-jobs", {"size": 100}),
    }
    if external_client is not None:
        evidence["external_jobs"] = _get_json(
            external_client, "/jobs", {"size": 100}
        )
    return evidence


# Engine-specific settings: artifact dir, container name, image filter, REST
# base + auth, adapter files and BPMN fixtures used for the metrics snapshot.
ENGINES = {
    "OPERATON": {
        "name": "Operaton",
        "version": "2.1.4",
        "artifacts": os.path.join(ROOT, "artifacts", "operaton"),
        "container": "benchmark-operaton",
        "image_ref": "operaton/operaton:2.1.4",
        "base": "http://localhost:8080/engine-rest",
        "auth": None,
        "collector": collect_operaton,
        "external_base": None,
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
    },
    "FLOWABLE": {
        "name": "Flowable",
        "version": "8.0.0",
        "artifacts": os.path.join(ROOT, "artifacts", "flowable"),
        "container": "benchmark-flowable",
        "image_ref": "flowable/flowable-rest:8.0.0",
        "base": "http://localhost:8081/flowable-rest/service",
        "auth": ("rest-admin", "test"),
        "collector": collect_flowable,
        "external_base": "http://localhost:8081/flowable-rest/external-job-api",
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
    },
}


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
    engine_log = _sh(["docker", "logs", cfg["container"]])
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

    # 3. image info (size, ID, RepoDigest) - authoritative pinning (A12)
    image_rows = _image_evidence(cfg["image_ref"])
    image_rows += _image_evidence("postgres:16")

    # 4. engine boot duration from log ("Started ... in N seconds")
    started_match = re.search(r"Started\s+\S+\s+in\s+([\d.]+)\s+seconds", engine_log)
    boot_seconds = float(started_match.group(1)) if started_match else None

    # 5. adapter LOC + BPMN extensions
    adapter_loc = {p: _line_count(p) for p in cfg["adapter_loc_files"]}
    bpmn_extensions = {}
    for path in cfg["bpmn_files"]:
        with open(os.path.join(ROOT, path)) as fh:
            xml = fh.read()
        extensions = {
            "vendor_namespace_declarations": len(re.findall(cfg["ns_re"], xml)),
            "vendor_attribute_usages": len(re.findall(cfg["attr_re"], xml)),
        }
        bpmn_extensions[path] = extensions

    # 6. engine REST evidence snapshots (engine-specific collectors, A06)
    client_kwargs = {"base_url": cfg["base"], "timeout": 15.0}
    if cfg["auth"]:
        client_kwargs["auth"] = cfg["auth"]
    with httpx.Client(**client_kwargs) as client:
        external_client = None
        if cfg["external_base"]:
            external_client = httpx.Client(base_url=cfg["external_base"], auth=cfg["auth"], timeout=15.0)
        try:
            evidence = cfg["collector"](client, external_client)
        finally:
            if external_client is not None:
                external_client.close()

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

    for name, payload in evidence.items():
        if isinstance(payload, (list, dict)):
            with open(os.path.join(evidence_dir, f"{name}.json"), "w") as fh:
                json.dump(payload, fh, indent=2, default=str)

    print(f"engine.log bytes   : {len(engine_log)}")
    print(f"containers statted : {len(stats)}")
    print(f"engine boot seconds: {boot_seconds}")
    print(f"adapter LOC total  : {sum(adapter_loc.values())}")
    print(f"evidence keys      : {sorted(evidence)}")
    print(f"artifacts written under {artifacts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
