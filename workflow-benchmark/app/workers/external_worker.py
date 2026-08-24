"""Operaton external task worker (Camunda External Task pattern).

The `load_prisoner_data` step in the BPMN fixture is an external service task.
The engine holds the wait state; this worker fetches-and-locks the task via the
public External Task REST API, performs the "remote" call locally (simulated
external service) and either reports a technical failure (triggering Operaton's
real retry mechanism) or completes the task with the fetched data.

Business logic is intentionally kept here, never inside the engine.
"""

import json
import uuid

import httpx

ENGINE_DEFAULT = "http://localhost:8080/engine-rest"
TOPIC_DEFAULT = "load_prisoner_data"


def to_camunda_variables(data: dict) -> dict:
    """Encode a flat Python dict into Camunda/Operaton variableDto."""
    out: dict = {}
    for key, value in (data or {}).items():
        if isinstance(value, bool):
            out[key] = {"value": value, "type": "Boolean"}
        elif isinstance(value, int):
            out[key] = {"value": value, "type": "Long"}
        elif isinstance(value, float):
            out[key] = {"value": value, "type": "Double"}
        elif isinstance(value, (dict, list)):
            out[key] = {"value": json.dumps(value), "type": "Json"}
        else:
            out[key] = {"value": str(value), "type": "String"}
    return out


class ExternalTaskWorker:
    """Polls one external task topic and completes it.

    fail_first mode simulates an unreliable external service: the first task
    handed to this process reports a technical failure with retries remaining,
    so Operaton's retry mechanism re-activates the task and the next attempt
    succeeds.
    """

    def __init__(
        self,
        engine_url: str | None = None,
        worker_id: str | None = None,
        topic: str = TOPIC_DEFAULT,
        lock_duration: int = 30000,
        fail_first: bool = False,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = (engine_url or ENGINE_DEFAULT).rstrip("/")
        self.worker_id = worker_id or f"benchmark-worker-{uuid.uuid4().hex[:8]}"
        self.topic = topic
        self.lock_duration = lock_duration
        self.fail_first = fail_first
        self._failed_once = False
        self.results = {"fetched": 0, "completed": 0, "failed": 0}
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)

    # ---- engine interaction ------------------------------------------------

    def fetch_and_lock(self, max_tasks: int = 5) -> list[dict]:
        resp = self._client.post(
            "/external-task/fetchAndLock",
            json={
                "workerId": self.worker_id,
                "maxTasks": max_tasks,
                "topics": [{"topicName": self.topic, "lockDuration": self.lock_duration}],
            },
        )
        resp.raise_for_status()
        return resp.json()

    def complete(self, task_id: str, variables: dict | None = None) -> None:
        resp = self._client.post(
            f"/external-task/{task_id}/complete",
            json={"workerId": self.worker_id, "variables": to_camunda_variables(variables or {})},
        )
        resp.raise_for_status()

    def fail(
        self,
        task_id: str,
        error_message: str,
        error_details: str | None = None,
        retries: int | None = None,
        retry_timeout: int | None = None,
    ) -> None:
        body: dict = {"workerId": self.worker_id, "errorMessage": error_message}
        if error_details:
            body["errorDetails"] = error_details
        if retries is not None:
            body["retries"] = retries
        if retry_timeout is not None:
            body["retryTimeout"] = retry_timeout
        resp = self._client.post(f"/external-task/{task_id}/failure", json=body)
        resp.raise_for_status()

    # ---- orchestration -----------------------------------------------------

    def poll_once(self, handle=None, max_tasks: int = 5, instance_filter: str | None = None) -> int:
        """Process one batch of external tasks.

        handle(task: dict) -> dict | None: returns variables to complete the
        task with, or None to report a failure. Defaults to the standard
        prisoner-data payload. If instance_filter is set, only tasks belonging
        to that process instance are handled.
        """
        tasks = self.fetch_and_lock(max_tasks=max_tasks)
        processed = 0
        for task in tasks:
            if instance_filter and task.get("processInstanceId") != instance_filter:
                # leave other instances' tasks alone; unlock happens via lock expiry
                continue
            task_id = task["id"]
            self.results["fetched"] += 1

            if self.fail_first and not self._failed_once:
                self._failed_once = True
                self.fail(
                    task_id,
                    error_message="simulated technical failure",
                    error_details="first execution fails by design (worker fail-first test mode)",
                    retries=2,
                    retry_timeout=3000,
                )
                self.results["failed"] += 1
                processed += 1
                continue

            variables = handle(task) if handle else default_prisoner_payload(task)
            if variables is None:
                self.fail(task_id, error_message="external service returned no data", retries=2, retry_timeout=5000)
                self.results["failed"] += 1
            else:
                self.complete(task_id, variables)
                self.results["completed"] += 1
            processed += 1
        return processed

    def close(self) -> None:
        self._client.close()


def default_prisoner_payload(task: dict) -> dict:
    """Simulated external service response for the prisoner-data topic."""
    return {"prisoner_exists": True, "unit": 3}
