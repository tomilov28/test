"""Start/stop/status helpers for the benchmark FastAPI app and worker daemon.

Both run as detached processes on the host (the shared Postgres + engines run in
Docker). The FastAPI app is a SHARED process (not engine-specific, audit A10):
its pid/log live under artifacts/ (single location), NOT under an engine
subdirectory. Workers keep an engine-specific pid/log so both engines can run a
daemon worker independently.

Usage:
    .venv/bin/python -m scripts.stackctl start-app
    .venv/bin/python -m scripts.stackctl stop-app
    .venv/bin/python -m scripts.stackctl status-app
    .venv/bin/python -m scripts.stackctl start-worker --engine OPERATON
    .venv/bin/python -m scripts.stackctl stop-worker --engine OPERATON
    .venv/bin/python -m scripts.stackctl stop-all
    .venv/bin/python -m scripts.stackctl check-stale
"""

import argparse
import os
import signal
import subprocess
import sys
import time

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PY = os.path.join(ROOT, ".venv", "bin", "python")

# Shared-process locations (audit A10): the app is NOT engine-specific.
ARTIFACTS_ROOT = os.path.join(ROOT, "artifacts")
APP_PIDFILE = os.path.join(ARTIFACTS_ROOT, "app.pid")
APP_LOG = os.path.join(ARTIFACTS_ROOT, "app.log")

ARTIFACT_DIRS = {
    "OPERATON": os.path.join(ROOT, "artifacts", "operaton"),
    "FLOWABLE": os.path.join(ROOT, "artifacts", "flowable"),
}
ENGINE_BASES = {
    "OPERATON": "http://localhost:8080/engine-rest",
    "FLOWABLE": "http://localhost:8081/flowable-rest/service",
}
ENGINE_PROBE_PATHS = {
    "OPERATON": "/engine",
    "FLOWABLE": "/management/engine",
}

API_BASE = "http://localhost:8000"

# Host process fingerprints that must NOT survive `make down` (audit A10).
APP_FINGERPRINTS = ["uvicorn app.main:app"]
WORKER_FINGERPRINTS = ["scripts.worker"]


def _paths(engine: str) -> dict:
    artifacts = ARTIFACT_DIRS[engine]
    return {
        "artifacts": artifacts,
        "worker_pidfile": os.path.join(artifacts, "worker.pid"),
        "worker_log": os.path.join(artifacts, "worker.log"),
    }


def _find_pids(fingerprints: list[str]) -> list[int]:
    """Find host process ids whose command line contains any fingerprint."""
    pids: list[int] = []
    for fp in fingerprints:
        try:
            out = subprocess.run(["pgrep", "-f", fp], capture_output=True, text=True).stdout
            pids.extend(int(p) for p in out.split() if p.strip())
        except Exception:
            continue
    return sorted(set(pids))


def _pid_alive(pidfile: str) -> int | None:
    try:
        with open(pidfile) as fh:
            pid = int(fh.read().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
        return pid
    except OSError:
        return None


def _write_pidfile(pidfile: str, pid: int) -> None:
    with open(pidfile, "w") as fh:
        fh.write(str(pid))


def _stop_pid(pidfile: str, name: str) -> None:
    pid = _pid_alive(pidfile)
    if pid is None:
        print(f"{name}: not running")
        return
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.3)
    print(f"{name}: stopped pid {pid}")


def _spawn(name: str, pidfile: str, logfile: str, argv: list[str]) -> None:
    if _pid_alive(pidfile) is not None:
        print(f"{name}: already running (pid {_pid_alive(pidfile)}); skipping")
        return
    os.makedirs(os.path.dirname(pidfile), exist_ok=True)
    with open(logfile, "ab") as log_fh:
        proc = subprocess.Popen(
            argv,
            stdout=log_fh,
            stderr=log_fh,
            cwd=ROOT,
            start_new_session=True,
        )
    _write_pidfile(pidfile, proc.pid)
    print(f"{name}: started pid {proc.pid} (log {logfile})")


def _status(name: str, pidfile: str, healthy: bool) -> None:
    pid = _pid_alive(pidfile)
    print(f"{name}: {'running pid ' + str(pid) if pid else 'not running'} healthy={healthy}")


def _app_healthy() -> bool:
    try:
        return httpx.get(f"{API_BASE}/health", timeout=3.0).status_code == 200
    except Exception:
        return False


def _engine_up(engine: str) -> bool:
    """Probe an engine. Flowable answers 401 without credentials (Spring
    security) but the server is up; Operaton answers 200. Treat any response
    below 500 as reachable (mirrors scripts/wait_for_engine.py)."""
    try:
        resp = httpx.get(
            f"{ENGINE_BASES[engine]}{ENGINE_PROBE_PATHS[engine]}", timeout=3.0
        )
        return resp.status_code < 500
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "start-app",
            "stop-app",
            "status-app",
            "start-worker",
            "stop-worker",
            "status-worker",
            "stop-all",
            "check-stale",
        ],
    )
    parser.add_argument(
        "--engine", choices=list(ARTIFACT_DIRS), default="OPERATON",
        help="engine the worker serves (selects pidfile dir + engine URL)",
    )
    args = parser.parse_args()
    paths = _paths(args.engine)

    if args.action == "start-app":
        _spawn(
            "app",
            APP_PIDFILE,
            APP_LOG,
            [VENV_PY, "-u", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"],
        )
        deadline = time.time() + 30
        while time.time() < deadline and not _app_healthy():
            time.sleep(1.0)
        print(f"app health: {_app_healthy()}")
        return 0 if _app_healthy() else 1

    if args.action == "stop-app":
        _stop_pid(APP_PIDFILE, "app")
        _kill_fingerprint(APP_FINGERPRINTS, "app")
        return 0

    if args.action == "status-app":
        _status("app", APP_PIDFILE, _app_healthy())
        return 0

    if args.action == "start-worker":
        if not _engine_up(args.engine):
            print("worker: engine not reachable; start infra first")
            return 1
        _spawn(
            "worker",
            paths["worker_pidfile"],
            paths["worker_log"],
            [
                VENV_PY, "-u", "-m", "scripts.worker",
                "--engine", args.engine, "--daemon", "--poll-requests",
            ],
        )
        return 0

    if args.action == "stop-worker":
        _stop_pid(paths["worker_pidfile"], "worker")
        _kill_fingerprint([f"scripts.worker --engine {args.engine}"], "worker")
        return 0

    if args.action == "status-worker":
        _status("worker", paths["worker_pidfile"], _engine_up(args.engine))
        return 0

    if args.action == "stop-all":
        _stop_pid(APP_PIDFILE, "app")
        for engine in ARTIFACT_DIRS:
            _stop_pid(_paths(engine)["worker_pidfile"], f"worker[{engine}]")
        _kill_fingerprint(APP_FINGERPRINTS + WORKER_FINGERPRINTS, "stale host processes")
        return 0

    if args.action == "check-stale":
        return check_stale()

    return 2


def _kill_fingerprint(fingerprints: list[str], name: str) -> None:
    """Terminate any leftover host process matching the fingerprints (A10)."""
    pids = _find_pids(fingerprints)
    for pid in pids:
        try:
            print(f"{name}: stopping stale pid {pid}")
            os.kill(pid, signal.SIGTERM)
        except OSError:
            continue
    deadline = time.time() + 15
    while time.time() < deadline and _find_pids(fingerprints):
        time.sleep(0.5)


def check_stale() -> int:
    """Verify no benchmark host processes are left after `make down` (A10)."""
    stale = _find_pids(APP_FINGERPRINTS + WORKER_FINGERPRINTS)
    if stale:
        print(f"STALE benchmark host processes found: {stale}", file=sys.stderr)
        return 1
    print("no stale benchmark host processes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
