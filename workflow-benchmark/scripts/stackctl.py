"""Start/stop/status helpers for the benchmark FastAPI app and worker daemon.

Both run as detached processes on the host (the shared Postgres + engines run in
Docker). Pidfiles under artifacts/operaton/ let `make up-operaton` and
`make down` manage their lifecycle deterministically.

Usage:
    .venv/bin/python -m scripts.stackctl start-app
    .venv/bin/python -m scripts.stackctl stop-app
    .venv/bin/python -m scripts.stackctl status-app
    .venv/bin/python -m scripts.stackctl start-worker
    .venv/bin/python -m scripts.stackctl stop-worker
    .venv/bin/python -m scripts.stackctl status-worker
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


def _paths(engine: str) -> dict:
    artifacts = ARTIFACT_DIRS[engine]
    return {
        "artifacts": artifacts,
        "app_pidfile": os.path.join(artifacts, "app.pid"),
        "app_log": os.path.join(artifacts, "app.log"),
        "worker_pidfile": os.path.join(artifacts, "worker.pid"),
        "worker_log": os.path.join(artifacts, "worker.log"),
    }


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
            paths["app_pidfile"],
            paths["app_log"],
            [VENV_PY, "-u", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"],
        )
        deadline = time.time() + 30
        while time.time() < deadline and not _app_healthy():
            time.sleep(1.0)
        print(f"app health: {_app_healthy()}")
        return 0 if _app_healthy() else 1

    if args.action == "stop-app":
        _stop_pid(paths["app_pidfile"], "app")
        return 0

    if args.action == "status-app":
        _status("app", paths["app_pidfile"], _app_healthy())
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
        return 0

    if args.action == "status-worker":
        _status("worker", paths["worker_pidfile"], _engine_up(args.engine))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
