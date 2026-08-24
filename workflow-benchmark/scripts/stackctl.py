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
ARTIFACTS = os.path.join(ROOT, "artifacts", "operaton")
VENV_PY = os.path.join(ROOT, ".venv", "bin", "python")

APP_PIDFILE = os.path.join(ARTIFACTS, "app.pid")
APP_LOG = os.path.join(ARTIFACTS, "app.log")
WORKER_PIDFILE = os.path.join(ARTIFACTS, "worker.pid")
WORKER_LOG = os.path.join(ARTIFACTS, "worker.log")

API_BASE = "http://localhost:8000"
ENGINE_BASE = "http://localhost:8080/engine-rest"


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
    os.makedirs(ARTIFACTS, exist_ok=True)
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
    os.makedirs(ARTIFACTS, exist_ok=True)
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


def _engine_up() -> bool:
    try:
        return httpx.get(f"{ENGINE_BASE}/engine", timeout=3.0).status_code == 200
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
    args = parser.parse_args()

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
        return 0

    if args.action == "status-app":
        _status("app", APP_PIDFILE, _app_healthy())
        return 0

    if args.action == "start-worker":
        if not _engine_up():
            print("worker: engine not reachable; start infra first")
            return 1
        _spawn(
            "worker",
            WORKER_PIDFILE,
            WORKER_LOG,
            [VENV_PY, "-u", "-m", "scripts.worker", "--engine", "OPERATON", "--daemon", "--poll-requests"],
        )
        return 0

    if args.action == "stop-worker":
        _stop_pid(WORKER_PIDFILE, "worker")
        return 0

    if args.action == "status-worker":
        _status("worker", WORKER_PIDFILE, _engine_up())
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
