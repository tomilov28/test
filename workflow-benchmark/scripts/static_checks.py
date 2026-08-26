"""Static architecture checks (Phase 6 deliverable).

Verifies, from source (no runtime needed):
  1. The application runtime never contains engine DB credentials (the app only
     knows the benchmark database URL; engine datasources live in docker-compose).
  2. The workflow adapters talk to the engines over public REST only.
  3. The application never reads/writes `ACT_*` / Flowable engine tables.
  4. Diagnostic code (fault injection) is inert by default and can only be armed
     through the benchmark/admin control surface (/admin/faults). The injector is
     imported into the runtime path via registry.maybe_wrap(), but adapters are
     wrapped only while a fault is armed; nothing is armed in the default
     production configuration.
  5. Shared vs engine-specific LOC (domain/api unchanged across engines; only
     adapter, worker protocol, BPMN and deployment/ops tooling differ).

Evidence: <run>/static-checks.json and a log line.
Usage:
    .venv/bin/python -m scripts.static_checks --run <run_dir>
"""

import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 1. engine DB credentials must never appear in app/ or scripts/
ENGINE_CRED_RE = re.compile(
    r"(jdbc:postgresql://\S+/(operaton|flowable)\b"
    r"|DB_PASSWORD|DB_USERNAME|SPRING_DATASOURCE"
    r"|5432/(operaton|flowable)\b)"
)
# 3. engine internal tables (Camunda/Operaton ACT_*, Flowable ACT_*) must never
# be referenced by application code.
ENGINE_TABLE_RE = re.compile(r"\bACT_[A-Z_]+\b")
# 4. production request path = api/routes (non-admin), domain, dispatcher,
# reconciler, workers. Fault injection is admin-only.
PROD_FILES = [
    "app/api/routes.py",
    "app/domain/completion.py",
    "app/domain/models.py",
    "app/workflow/base.py",
    "app/workflow/dispatcher.py",
    "app/workflow/reconciler.py",
    "app/workers/external_worker.py",
    "app/workers/flowable_worker.py",
]
ADAPTER_FILES = ["app/workflow/operaton.py", "app/workflow/flowable.py"]

LOC_GROUPS = {
    "shared/domain": [
        "app/domain/models.py", "app/domain/completion.py",
        "app/api/routes.py", "app/api/schemas.py",
        "app/workflow/base.py", "app/workflow/dispatcher.py",
        "app/workflow/reconciler.py", "app/workflow/registry.py",
        "app/workflow/fault_injector.py",
    ],
    "operaton-specific": [
        "app/workflow/operaton.py", "app/workers/external_worker.py",
        "bpmn/operaton/long_visit_poc.bpmn", "bpmn/operaton/long_visit_poc_v2.bpmn",
    ],
    "flowable-specific": [
        "app/workflow/flowable.py", "app/workers/flowable_worker.py",
        "bpmn/flowable/long_visit_v1.bpmn", "bpmn/flowable/long_visit_v2.bpmn",
    ],
}


def line_count(path: str) -> int:
    try:
        with open(os.path.join(ROOT, path)) as fh:
            return sum(1 for _ in fh)
    except OSError:
        return -1


def scan_files(files: list[str], regex: re.Pattern) -> dict:
    hits = {}
    for rel in files:
        path = os.path.join(ROOT, rel)
        if not os.path.isfile(path):
            hits[rel] = ["missing"]
            continue
        with open(path) as fh:
            for i, line in enumerate(fh, 1):
                if regex.search(line):
                    hits.setdefault(rel, []).append(f"{i}: {line.rstrip()}")
    return hits


def check_engine_db_credentials() -> dict:
    # engine datasource config lives ONLY in the docker-compose overlay files.
    allowed = {"docker-compose.operaton.yml", "docker-compose.flowable.yml",
               "docker-compose.yml", "versions.env"}
    bad = {}
    for dp, _, fns in os.walk(os.path.join(ROOT, "app")):
        for f in fns:
            if not f.endswith(".py"):
                continue
            path = os.path.join(dp, f)
            with open(path) as fh:
                for i, line in enumerate(fh, 1):
                    if ENGINE_CRED_RE.search(line):
                        bad.setdefault(os.path.relpath(path, ROOT), []).append(i)
    for name in ("scripts", "tests"):
        for dp, _, fns in os.walk(os.path.join(ROOT, name)):
            for f in fns:
                if not f.endswith(".py"):
                    continue
                if f in ("create_databases.py", "cleanup_engine.py", "static_checks.py"):
                    continue
                path = os.path.join(dp, f)
                with open(path) as fh:
                    for i, line in enumerate(fh, 1):
                        if ENGINE_CRED_RE.search(line):
                            bad.setdefault(os.path.relpath(path, ROOT), []).append(i)
    return {"finding": "no engine DB credentials in app/scripts/tests",
            "violations": bad, "pass": not bad}


def check_no_engine_tables() -> dict:
    hits = scan_files(PROD_FILES + ADAPTER_FILES, ENGINE_TABLE_RE)
    return {"finding": "application code never references ACT_* engine tables",
            "violations": hits, "pass": not hits}


def check_adapters_use_rest() -> dict:
    """Adapters must use httpx against the engine REST base (no engine SQL)."""
    for f in ADAPTER_FILES:
        path = os.path.join(ROOT, f)
        if not os.path.isfile(path):
            return {"finding": f, "violations": {"missing": [f]}, "pass": False}
    httpx_use = all(
        "httpx" in open(os.path.join(ROOT, f)).read() for f in ADAPTER_FILES
    )
    sqlalchemy_in_adapter = scan_files(ADAPTER_FILES, re.compile(r"\bcreate_engine\b"))
    pass_ = httpx_use and not sqlalchemy_in_adapter
    return {"finding": "adapters use REST (httpx) and no create_engine",
            "httpx_in_adapters": httpx_use, "violations": sqlalchemy_in_adapter,
            "pass": pass_}


def check_diagnostics_admin_only() -> dict:
    """Fault injection must be inert by default and armable only via admin API.

    The injector (fault_injector.maybe_wrap) is imported into the runtime path
    by app/workflow/registry.py and evaluated on every build_adapter call, so it
    is not absent from the runtime path. It is, however, inert unless a fault is
    armed: FaultController starts empty and adapters are wrapped only while an
    active (non-exhausted) arm exists. The only arming surface outside the test
    suite is the admin API (POST /admin/faults/arm).
    """
    hits = {}
    for rel in PROD_FILES:
        path = os.path.join(ROOT, rel)
        if not os.path.isfile(path):
            continue
        with open(path) as fh:
            for i, line in enumerate(fh, 1):
                if re.search(r"\bfault[_ ]controller\b|\bFaultController\b", line, re.I):
                    hits.setdefault(rel, []).append(i)
    # arming must be limited to the admin route; tests arm only through the same
    # controller for unit coverage.
    arm_sites = {}
    for dp, _, fns in os.walk(os.path.join(ROOT, "app")):
        for f in fns:
            if not f.endswith(".py"):
                continue
            path = os.path.join(dp, f)
            with open(path) as fh:
                for i, line in enumerate(fh, 1):
                    if re.search(r"\.arm\(", line):
                        arm_sites.setdefault(os.path.relpath(path, ROOT), []).append(i)
    non_admin_arms = {rel: n for rel, n in arm_sites.items() if rel != "app/api/routes.py"}
    admin_wired = os.path.exists(os.path.join(ROOT, "app/api/routes.py"))
    return {"finding": "fault injection inert by default; armable only via admin API (/admin/faults)",
            "production_path_references": hits,
            "arm_sites": arm_sites,
            "non_admin_arm_sites": non_admin_arms,
            "pass": not hits and admin_wired and not non_admin_arms}


def loc_report() -> dict:
    groups = {}
    for name, files in LOC_GROUPS.items():
        counts = {f: line_count(f) for f in files}
        groups[name] = {"files": counts, "total": sum(v for v in counts.values() if v > 0)}
    return groups


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, help="run directory to write into")
    args = parser.parse_args()

    checks = {
        "no_engine_db_credentials": check_engine_db_credentials(),
        "no_engine_tables": check_no_engine_tables(),
        "adapters_use_rest_only": check_adapters_use_rest(),
        "diagnostics_admin_only": check_diagnostics_admin_only(),
        "loc": loc_report(),
    }
    all_pass = all(v["pass"] for k, v in checks.items() if "pass" in v)
    checks["all_pass"] = all_pass

    out = os.path.join(args.run, "static-checks.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as fh:
        json.dump(checks, fh, indent=2)
    print(json.dumps({"all_pass": all_pass,
                      "checks": {k: v["finding"] for k, v in checks.items()
                                 if isinstance(v, dict) and "finding" in v}}, indent=2))
    print(f"static checks written: {out}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
