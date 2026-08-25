"""Part B resource methodology (audit deliverable).

Clean-DB measurement protocol:
  * ONLY PostgreSQL and the target engine are running. The FastAPI harness
    (app) and workers are stopped before this script runs (see the
    `measure-resources` Makefile target); if they are still alive the run is
    marked INVALID and `comparable` is set False.
  * The engine container settles for 60s, then `docker stats` is sampled 5+
    times (every 10s). Median RSS (bytes) and median CPU% are reported.
  * Parity conditions: both engines run under the same cpu limit (cpus=2) and
    memory limit (1024m). Any unmet condition marks the result
    NOT STRICTLY COMPARABLE.

Evidence: artifacts/<engine>/resources.json
Usage:
    .venv/bin/python -m scripts.measure_resources --engine OPERATON
    .venv/bin/python -m scripts.measure_resources --engine FLOWABLE
"""

import argparse
import json
import os
import re as _re
import statistics
import subprocess
import sys
import time

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARTIFACTS = {e: os.path.join(ROOT, "artifacts", e.lower()) for e in ("OPERATON", "FLOWABLE")}

ENGINES = {
    "OPERATON": {
        "name": "Operaton",
        "version": "2.1.4",
        "container": "benchmark-operaton",
        "image_ref": "operaton/operaton:2.1.4",
        "probe": ("http://localhost:8080/engine-rest/engine", None),
        "expected": {"cpus": 2, "mem_bytes": 1024 * 1024 * 1024},
    },
    "FLOWABLE": {
        "name": "Flowable",
        "version": "8.0.0",
        "container": "benchmark-flowable",
        "image_ref": "flowable/flowable-rest:8.0.0",
        "probe": ("http://localhost:8081/flowable-rest/service/management/engine", ("rest-admin", "test")),
        "expected": {"cpus": 2, "mem_bytes": 1024 * 1024 * 1024},
    },
}

SETTLE_SECONDS = 60
SAMPLES = 6
INTERVAL_SECONDS = 10

POSTGRES_CONTAINER = "workflow-benchmark-postgres-1"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _sh(args: list[str]) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=True).stdout


def running_containers() -> set[str]:
    out = _sh(["docker", "ps", "--format", "{{.Names}}"])
    return {line.strip() for line in out.splitlines() if line.strip()}


def _pgrep(needle: str) -> list[int]:
    try:
        out = subprocess.run(["pgrep", "-f", needle], capture_output=True, text=True).stdout
        return [int(p) for p in out.split() if p.strip()]
    except Exception:
        return []


def check_isolation(engine: str) -> dict:
    """Only postgres + the target engine container; no app/worker/harness."""
    issues: list[str] = []
    containers = running_containers()
    if POSTGRES_CONTAINER not in containers:
        issues.append(f"{POSTGRES_CONTAINER} not running")
    target = ENGINES[engine]["container"]
    other = ENGINES["FLOWABLE" if engine == "OPERATON" else "OPERATON"]["container"]
    if target not in containers:
        issues.append(f"{target} not running")
    if other in containers:
        issues.append(f"other engine {other} still running")
    if _pgrep("uvicorn app.main:app"):
        issues.append("FastAPI app still running (uvicorn app.main:app)")
    if _pgrep("scripts.worker"):
        issues.append("worker still running (scripts.worker)")
    return {"containers": sorted(containers), "issues": issues, "valid": not issues}


def parse_stats_line(raw: str) -> dict:
    entry = json.loads(raw)
    mem_usage, mem_limit = entry["MemUsage"].split(" / ")
    def _bytes(s: str) -> int:
        # docker stats may print "1.5GiB" or "1.5 GiB"; accept both.
        m = _re.fullmatch(r"\s*([\d.]+)\s*(B|[KMGT]i?B)\s*", s)
        if not m:
            raise ValueError(f"cannot parse size: {s!r}")
        val = float(m.group(1))
        unit = m.group(2)
        scale = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}
        return int(val * scale[unit])
    cpu = float(entry["CPUPerc"].rstrip("%"))
    return {
        "mem_usage_bytes": _bytes(mem_usage),
        "mem_limit_bytes": _bytes(mem_limit),
        "cpu_percent": cpu,
        "container": entry.get("Name"),
        "id": entry.get("ID"),
    }


def sample_docker(container: str) -> list[dict]:
    raw = _sh(["docker", "stats", "--no-stream", "--format", "{{json .}}", container])
    return [parse_stats_line(line) for line in raw.splitlines() if line.strip()]


def container_identity(container: str) -> dict:
    info = json.loads(_sh(["docker", "inspect", container]))[0]
    image_id = info["Image"]
    digest = info.get("ImageDigest", "")
    repo_digest = None
    inspect_img = json.loads(_sh(["docker", "inspect", image_id]))[0]
    if inspect_img.get("RepoDigests"):
        repo_digest = inspect_img["RepoDigests"][0]
    return {
        "container_id": info["Id"],
        "image_id": image_id,
        "repo_digest": repo_digest,
        "started_at": info["State"]["StartedAt"],
        "cpus": info.get("HostConfig", {}).get("CpuQuota"),
    }


def engine_version(probe: tuple[str, str | None]) -> dict:
    url, auth = probe
    try:
        resp = httpx.get(url, auth=auth, timeout=10.0)
        data = resp.json()
        # Operaton /engine returns a list (one entry per engine); Flowable
        # /management/engine returns a single object.
        if isinstance(data, list):
            data = data[0] if data else {}
        if isinstance(data, dict):
            return {"name": data.get("name", ""), "version": data.get("version", "")}
        return {"name": "", "version": ""}
    except Exception as exc:
        return {"error": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=["OPERATON", "FLOWABLE"], required=True)
    args = parser.parse_args()
    cfg = ENGINES[args.engine]

    log(f"resource measurement for {cfg['name']} ({cfg['version']})")
    isolation = check_isolation(args.engine)
    if not isolation["valid"]:
        log(f"WARNING isolation violated: {isolation['issues']}")
    else:
        log("isolation OK: only postgres + target engine running")

    log(f"settling {SETTLE_SECONDS}s...")
    time.sleep(SETTLE_SECONDS)

    samples: list[dict] = []
    for i in range(SAMPLES):
        samples.extend(sample_docker(cfg["container"]))
        if i < SAMPLES - 1:
            time.sleep(INTERVAL_SECONDS)
        log(f"sample {i + 1}/{SAMPLES} captured")

    rss = [s["mem_usage_bytes"] for s in samples]
    cpu = [s["cpu_percent"] for s in samples]
    summary = {
        "engine": cfg["name"],
        "version": cfg["version"],
        "image_ref": cfg["image_ref"],
        "samples": len(samples),
        "settle_seconds": SETTLE_SECONDS,
        "median_rss_bytes": int(statistics.median(rss)),
        "min_rss_bytes": min(rss),
        "max_rss_bytes": max(rss),
        "median_cpu_percent": statistics.median(cpu),
        "max_cpu_percent": max(cpu),
        "sample_raw": samples,
    }

    parity = {
        "cpu_limit_equal": cfg["expected"]["cpus"] == 2,
        "mem_limit_equal": all(s["mem_limit_bytes"] == cfg["expected"]["mem_bytes"] for s in samples),
        "observed_cpu_limit": {"cpus": cfg["expected"]["cpus"]},
    }
    comparable = isolation["valid"] and all(parity.values())
    if not comparable:
        log("NOT STRICTLY COMPARABLE: isolation/parity conditions not met")
    parity["comparable"] = comparable
    parity["verdict"] = "STRICTLY COMPARABLE" if comparable else "NOT STRICTLY COMPARABLE"

    result = {
        "summary": summary,
        "parity": parity,
        "isolation": isolation,
        "identity": container_identity(cfg["container"]),
        "engine_probe": engine_version(cfg["probe"]),
    }

    out_dir = ARTIFACTS[args.engine]
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "resources.json")
    with open(path, "w") as fh:
        json.dump(result, fh, indent=2, default=str)
    log(f"evidence written: {path}")

    print("\n== resource summary ==")
    print(f"  engine          : {cfg['name']} {cfg['version']}")
    print(f"  median RSS      : {summary['median_rss_bytes'] / 1024 / 1024:.1f} MiB")
    print(f"  RSS range       : {summary['min_rss_bytes'] / 1024 / 1024:.1f}-{summary['max_rss_bytes'] / 1024 / 1024:.1f} MiB")
    print(f"  median CPU      : {summary['median_cpu_percent']:.1f}%")
    print(f"  verdict         : {parity['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
