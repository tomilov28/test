"""Phase 6 authoritative benchmark orchestrator (make benchmark).

Runs the full two-engine benchmark from a clean state and writes every stage's
evidence into an immutable run directory:

    artifacts/runs/<UTC_TIMESTAMP>-<GIT_SHA>/

Layout (minimum contract):
    manifest.json
    environment/          (env capture, versions, image digests, docker ps)
    unit/                 (unit-junit.xml + stdout)
    operaton/  flowable/  (functional-junit.xml, fault-junit.xml,
                           audit-junit.xml, durability.log, fault-scenarios/,
                           engine.log, app.log, worker.log, api-evidence/,
                           resource-metrics.json, operational/ demo+incident)
    report-input.json
    sha256sums.txt

Every stage is recorded in manifest.json with command, UTC timestamps, exit
code, PASS/FAIL and evidence paths. A mandatory stage failure aborts the run
and exits non-zero: `make benchmark` therefore fails loudly (no `|| true`, no
fake PASS, no ignored exit codes).

Usage:
    .venv/bin/python -m scripts.run_benchmark [--engines OPERATON,FLOWABLE]
"""

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PY = os.path.join(ROOT, ".venv", "bin", "python")
ARTIFACTS = os.path.join(ROOT, "artifacts")
RUNS = os.path.join(ARTIFACTS, "runs")

ENGINES = {
    "OPERATON": {
        "name": "Operaton",
        "service": "operaton",
        "key": "operaton",
        "make_suffix": "operaton",
        "compose": "docker compose -f docker-compose.yml -f docker-compose.operaton.yml",
        "image_ref": "operaton/operaton:2.1.4",
    },
    "FLOWABLE": {
        "name": "Flowable",
        "service": "flowable",
        "key": "flowable",
        "make_suffix": "flowable",
        "compose": "docker compose -f docker-compose.yml -f docker-compose.flowable.yml",
        "image_ref": "flowable/flowable-rest:8.0.0",
    },
}

UNIT_TESTS = "tests/test_domain.py tests/test_dispatcher.py tests/test_reconciler.py " \
             "tests/test_completion.py tests/test_outbox_lease.py tests/test_registry.py"
AUDIT_TESTS = "tests/test_fault_injection.py tests/test_outbox_lease.py"

IMAGE_REFS = ["operaton/operaton:2.1.4", "flowable/flowable-rest:8.0.0", "postgres:16"]


def utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def sh(cmd: str, *, cwd: str | None = None, env: dict | None = None) -> dict:
    """Run a shell command from ROOT; capture stdout/stderr + exit code."""
    full_env = dict(os.environ)
    full_env.update(env or {})
    started = time.time()
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=cwd or ROOT, env=full_env,
            capture_output=True, text=True,
        )
        exit_code = proc.returncode
        out = proc.stdout or ""
    except Exception as exc:  # e.g. command not found
        exit_code = 127
        out = f"<exception running command: {exc}>\n"
    return {"exit": exit_code, "out": out, "err": "", "elapsed_s": round(time.time() - started, 1)}


def write_log(path: str, cmd: str, rec: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(f"$ {cmd}\n")
        fh.write(rec["out"])
        if not rec["out"].endswith("\n"):
            fh.write("\n")
        fh.write(f"\n[exit {rec['exit']} after {rec['elapsed_s']}s]\n")


def copy(src: str, dst: str) -> str:
    if os.path.exists(src):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        return dst
    return ""


def copy_tree(src: str, dst: str) -> int:
    if not os.path.isdir(src):
        return 0
    os.makedirs(dst, exist_ok=True)
    n = 0
    for name in sorted(os.listdir(src)):
        p = os.path.join(src, name)
        if os.path.isfile(p):
            copy(p, os.path.join(dst, name))
            n += 1
    return n


class Run:
    def __init__(self, run_dir: str):
        self.run_dir = run_dir
        self.git_sha = sh("git rev-parse HEAD")["out"].strip()
        self.git_dirty = bool(sh("git status --porcelain")["out"].strip())
        self.stages: list[dict] = []
        self.aborted = False

    def stage(self, name: str, cmd: str, *, mandatory: bool = True,
              evidence: list[str] | None = None, env: dict | None = None,
              log_path: str | None = None, tee: str | None = None) -> dict:
        rec = sh(cmd, env=env)
        record = {
            "stage": name,
            "command": cmd,
            "started_at_utc": utcnow(),
            "finished_at_utc": utcnow(),
            "elapsed_s": rec["elapsed_s"],
            "exit_code": rec["exit"],
            "status": "PASS" if rec["exit"] == 0 else "FAIL",
            "evidence": evidence or [],
        }
        if log_path:
            write_log(log_path, cmd, rec)
        if tee:
            os.makedirs(os.path.dirname(tee), exist_ok=True)
            with open(tee, "w") as fh:
                fh.write(rec["out"])
        self.stages.append(record)
        mark = "PASS" if rec["exit"] == 0 else "FAIL"
        log(f"stage {name}: {mark} (exit={rec['exit']}, {rec['elapsed_s']}s)")
        if mandatory and rec["exit"] != 0:
            self.aborted = True
            raise RuntimeError(f"mandatory stage '{name}' failed with exit {rec['exit']}")
        return rec


def image_evidence() -> dict:
    out = {}
    for ref in IMAGE_REFS:
        rec = sh(f"docker image inspect {ref}")
        row = {"ref": ref}
        if rec["exit"] == 0:
            try:
                data = json.loads(rec["out"])
                img = data[0]
                row.update({
                    "id": img.get("Id"),
                    "repo_digests": img.get("RepoDigests", []),
                    "size_bytes": img.get("Size"),
                    "created": img.get("Created"),
                })
            except Exception as exc:
                row["error"] = str(exc)
        else:
            row["error"] = "image not present locally"
        out[ref] = row
    return out


def capture_environment(run: Run) -> list[str]:
    env_dir = os.path.join(run.run_dir, "environment")
    os.makedirs(env_dir, exist_ok=True)
    evidence: list[str] = []
    pairs = [
        ("git-info.txt", "git rev-parse HEAD && echo '--- status ---' && git status --porcelain && echo '--- log ---' && git log -8 --oneline"),
        ("docker-version.txt", "docker version"),
        ("docker-compose-version.txt", "docker compose version"),
        ("python-version.txt", f"{VENV_PY} --version && {VENV_PY} -m pip --version"),
        ("free-h.txt", "free -h"),
        ("docker-ps-a.txt", "docker ps -a"),
        ("uname.txt", "uname -a && nproc && grep -c '^processor' /proc/cpuinfo && grep MemTotal /proc/meminfo"),
    ]
    for fname, cmd in pairs:
        rec = sh(cmd)
        write_log(os.path.join(env_dir, fname), cmd, rec)
        evidence.append(f"environment/{fname}")
    img = image_evidence()
    with open(os.path.join(env_dir, "images.json"), "w") as fh:
        json.dump(img, fh, indent=2)
    evidence.append("environment/images.json")
    return evidence


def parse_junit(path: str) -> dict:
    """Return {tests, failures, errors, skipped, testcases: [...names]}."""
    if not os.path.exists(path):
        return {"tests": 0, "failures": -1, "errors": -1, "skipped": -1, "testcases": [], "present": False}
    try:
        root = ET.parse(path).getroot()
        suites = root if root.tag == "testsuites" else [root]
        tests = failures = errors = skipped = 0
        cases: list[str] = []
        for suite in suites:
            tests += int(suite.get("tests", 0) or 0)
            failures += int(suite.get("failures", 0) or 0)
            errors += int(suite.get("errors", 0) or 0)
            skipped += int(suite.get("skipped", 0) or 0)
            for case in suite.iter("testcase"):
                name = case.get("name", "")
                cls = case.get("classname", "")
                cases.append(f"{cls}::{name}" if cls else name)
        return {"tests": tests, "failures": failures, "errors": errors,
                "skipped": skipped, "testcases": cases, "present": True}
    except Exception:
        return {"tests": 0, "failures": -1, "errors": -1, "skipped": -1, "testcases": [], "present": False}


def engine_evidence_dir(run: Run, engine: str) -> str:
    d = os.path.join(run.run_dir, ENGINES[engine]["key"])
    os.makedirs(d, exist_ok=True)
    return d


def collect_engine_evidence(run: Run, engine: str) -> None:
    key = ENGINES[engine]["key"]
    eng_dir = engine_evidence_dir(run, engine)
    src_dir = os.path.join(ARTIFACTS, key)
    app_log_src = os.path.join(ARTIFACTS, "app.log")

    copy(os.path.join(src_dir, "test-results.xml"), os.path.join(eng_dir, "functional-junit.xml"))
    copy(os.path.join(src_dir, "fault-test-results.xml"), os.path.join(eng_dir, "fault-junit.xml"))
    copy(os.path.join(src_dir, "engine.log"), os.path.join(eng_dir, "engine.log"))
    copy(app_log_src, os.path.join(eng_dir, "app.log"))
    copy(os.path.join(src_dir, "worker.log"), os.path.join(eng_dir, "worker.log"))
    copy(os.path.join(src_dir, "resources.json"), os.path.join(eng_dir, "resource-metrics.json"))
    copy_tree(os.path.join(src_dir, "faults"), os.path.join(eng_dir, "fault-scenarios"))
    copy_tree(os.path.join(src_dir, "api-evidence"), os.path.join(eng_dir, "api-evidence"))
    if not os.path.exists(os.path.join(eng_dir, "audit-junit.xml")):
        log(f"WARNING: audit-junit.xml missing for {engine}")
    log(f"collected engine evidence for {engine} under {eng_dir}")


def parse_evidence_faults(run: Run, engine: str) -> dict:
    d = os.path.join(run.run_dir, ENGINES[engine]["key"], "fault-scenarios")
    out = {}
    for fname in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if fname.endswith(".json"):
            try:
                with open(os.path.join(d, fname)) as fh:
                    data = json.load(fh)
                scenario = fname.replace(".json", "")
                if isinstance(data, dict) and "scenario" in data:
                    out[data["scenario"]] = {"file": fname, "present": True,
                                             "engine": data.get("engine")}
                    if "recurrence" in data:
                        out[data["scenario"]]["recurrence"] = data["recurrence"]
                else:
                    out[scenario] = {"file": fname, "present": True}
            except Exception:
                out[fname] = {"file": fname, "present": True, "parse_error": True}
    return out


def summarize_engine(run: Run, engine: str) -> dict:
    key = ENGINES[engine]["key"]
    eng_dir = os.path.join(run.run_dir, key)
    return {
        "name": ENGINES[engine]["name"],
        "functional_junit": parse_junit(os.path.join(eng_dir, "functional-junit.xml")),
        "fault_junit": parse_junit(os.path.join(eng_dir, "fault-junit.xml")),
        "audit_junit": parse_junit(os.path.join(eng_dir, "audit-junit.xml")),
        "fault_scenarios": parse_evidence_faults(run, engine),
        "resource_metrics": _read_json(os.path.join(eng_dir, "resource-metrics.json")),
        "evidence_files": sorted(
            os.path.relpath(os.path.join(dp, f), run.run_dir)
            for dp, _, fns in os.walk(eng_dir) for f in fns
        ),
    }


def _read_json(path: str) -> dict | None:
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return None


def build_report_input(run: Run) -> str:
    path = os.path.join(run.run_dir, "report-input.json")
    data = {
        "run_id": os.path.basename(run.run_dir),
        "git": {"sha": run.git_sha, "dirty": run.git_dirty},
        "generated_at_utc": utcnow(),
        "images": image_evidence(),
        "unit": parse_junit(os.path.join(run.run_dir, "unit", "unit-junit.xml")),
        "operaton": summarize_engine(run, "OPERATON"),
        "flowable": summarize_engine(run, "FLOWABLE"),
        "stages": run.stages,
    }
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2, default=str)
    log(f"report inputs written: {path}")
    return path


def write_manifest(run: Run) -> str:
    path = os.path.join(run.run_dir, "manifest.json")
    manifest = {
        "run_id": os.path.basename(run.run_dir),
        "started_at_utc": run.started_at_utc,
        "finished_at_utc": utcnow(),
        "git": {"sha": run.git_sha, "dirty": run.git_dirty},
        "environment": {
            "host": sh("uname -a")["out"].strip(),
            "nproc": sh("nproc")["out"].strip(),
            "mem": sh("grep MemTotal /proc/meminfo")["out"].strip(),
            "docker_version": sh("docker version --format '{{.Server.Version}}'")["out"].strip(),
            "docker_compose_version": sh("docker compose version")["out"].strip(),
            "python_version": sh(f"{VENV_PY} --version")["out"].strip(),
            "images": image_evidence(),
        },
        "stages": run.stages,
        "status": "ABORTED" if run.aborted else "COMPLETED",
    }
    with open(path, "w") as fh:
        json.dump(manifest, fh, indent=2, default=str)
    return path


def write_sha256sums(run: Run) -> str:
    path = os.path.join(run.run_dir, "sha256sums.txt")
    lines = []
    for dp, _, fns in os.walk(run.run_dir):
        for f in sorted(fns):
            if f == "sha256sums.txt":
                continue
            full = os.path.join(dp, f)
            rel = os.path.relpath(full, run.run_dir)
            rec = sh(f"sha256sum '{full}'")
            if rec["exit"] == 0 and rec["out"].strip():
                lines.append(rec["out"].strip().split()[0] + "  " + rel)
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def run_engine(run: Run, engine: str) -> None:
    cfg = ENGINES[engine]
    key = cfg["key"]
    make_s = cfg["make_suffix"]
    log(f"===== ENGINE {engine} ({cfg['name']}) =====")

    run.stage("reset-clean-state", "make reset-state",
              evidence=["environment/"])

    # truncate the shared app log so this engine's window is self-contained
    open(os.path.join(ARTIFACTS, "app.log"), "w").close()

    run.stage(
        f"functional-durability-{key}",
        f"make test-{make_s}",
        evidence=[f"{key}/functional-junit.xml", f"{key}/durability.log"],
        log_path=os.path.join(run.run_dir, key, "durability.log"),
    )

    run.stage(
        f"fault-audit-{key}",
        f"make fault-{make_s}",
        evidence=[f"{key}/fault-junit.xml", f"{key}/fault-scenarios/"],
        log_path=os.path.join(run.run_dir, key, "fault.log"),
    )

    run.stage(
        f"audit-regressions-{key}",
        f"BENCH_ENGINE={engine} {VENV_PY} -m pytest -m integration "
        f"--junitxml={os.path.join(run.run_dir, key, 'audit-junit.xml')} {AUDIT_TESTS}",
        evidence=[f"{key}/audit-junit.xml"],
        log_path=os.path.join(run.run_dir, key, "audit.log"),
    )

    run.stage(
        f"demo-{key}",
        f"make demo-{make_s}",
        evidence=[f"{key}/operational/demo.json"],
        log_path=os.path.join(run.run_dir, key, "demo.log"),
    )

    run.stage(
        f"incident-{key}",
        f"make incident-{make_s}",
        evidence=[f"{key}/operational/incident.json"],
        log_path=os.path.join(run.run_dir, key, "incident.log"),
    )

    # Part B clean-DB resource measurement: reset (only PG + engine), settle,
    # sample. No app/worker, no other engine container.
    run.stage(
        f"measure-resource-{key}",
        f"make reset-state && "
        f"{cfg['compose']} up -d {cfg['service']} && "
        f"{VENV_PY} scripts/wait_for_engine.py --engine {engine} && "
        f"{VENV_PY} -m scripts.measure_resources --engine {engine}",
        evidence=[f"{key}/resource-metrics.json"],
        log_path=os.path.join(run.run_dir, key, "resource-measure.log"),
    )

    # snapshot engine-scoped evidence into the run dir (after all engine runs)
    collect_engine_evidence(run, engine)

    run.stage(f"teardown-{key}", "make down", evidence=[])

    # copy operational evidence
    eng_dir = os.path.join(run.run_dir, key)
    op_dir = os.path.join(eng_dir, "operational")
    os.makedirs(op_dir, exist_ok=True)
    copy(os.path.join(ARTIFACTS, key, "faults", "ops_demo_demo.json"), os.path.join(op_dir, "demo.json"))
    copy(os.path.join(ARTIFACTS, key, "faults", "ops_demo_incident.json"), os.path.join(op_dir, "incident.json"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engines", default="OPERATON,FLOWABLE",
                        help="comma-separated subset of OPERATON,FLOWABLE")
    args = parser.parse_args()
    engines = [e.strip().upper() for e in args.engines.split(",") if e.strip()]
    for e in engines:
        if e not in ENGINES:
            print(f"unknown engine {e!r}", file=sys.stderr)
            return 2

    run_id = f"{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    started_at = utcnow()
    git_sha = sh("git rev-parse HEAD")["out"].strip()
    run_id = f"{started_at.replace('-', '').replace(':', '')}-{git_sha[:7]}"
    run = Run(os.path.join(RUNS, run_id))
    run.started_at_utc = started_at
    run.git_sha = git_sha
    run.git_dirty = bool(sh("git status --porcelain")["out"].strip())
    os.makedirs(run.run_dir, exist_ok=True)
    log(f"run dir: {run.run_dir}")

    try:
        env_evidence = capture_environment(run)
        run.stage(
            "unit",
            f"{VENV_PY} -m pytest -q -m 'not integration' "
            f"--junitxml={os.path.join(run.run_dir, 'unit', 'unit-junit.xml')}",
            evidence=["unit/unit-junit.xml"],
            log_path=os.path.join(run.run_dir, "unit", "unit.log"),
        )
        for engine in engines:
            run_engine(run, engine)
        run.stage("teardown", "make down", evidence=[])
        run.stage(
            "static-checks",
            f"{VENV_PY} -m scripts.static_checks --run {run.run_dir}",
            evidence=["static-checks.json"],
            log_path=os.path.join(run.run_dir, "static-checks.log"),
        )
        build_report_input(run)
        write_manifest(run)
        write_sha256sums(run)
        # generate REPORT.md from the authoritative run
        rc = sh(f"{VENV_PY} -m scripts.generate_report --run {run.run_dir}")
        if rc["exit"] != 0:
            log("REPORT.md generation failed")
            return 1
    except RuntimeError as exc:
        log(f"RUN ABORTED: {exc}")
        write_manifest(run)
        return 1

    log(f"== benchmark run complete: {run.run_dir} ==")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
