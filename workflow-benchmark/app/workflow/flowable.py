"""Flowable (8.x) REST adapter.

Two REST surfaces are used:
  * Process REST under `<base>/service` (deploy, runtime, history, management)
  * External Job REST under `<base>/external-job-api` (external worker pattern:
    the `flowable:type="external"` service task wait state is an "external job")

Flowable's external-worker protocol differs from Camunda/Operaton: a single
acquire call locks jobs of ONE topic (`topic` field, ISO-8601 `lockDuration`),
and completion/failure go to `/external-job-api/acquire/jobs/{id}/...`.

No business logic lives here; only engine REST calls + the Flowable variable
codec (variableDto with lowercase types).
"""

import json
import time

import httpx

from app.config import get_settings
from app.workflow.base import (
    DeploymentInfo,
    EngineTask,
    FailedJobInfo,
    HistoryEvent,
    ProcessInstanceInfo,
)

# Default credentials created by flowable-rest's BootstrapConfiguration when no
# rest admin user is provisioned ("No rest admin user found, initializing
# default entities"). Kept next to the adapter so the REST mapping is obvious.
DEFAULT_AUTH = ("rest-admin", "test")

# Flowable REST collection endpoints default to 10 rows per page; benchmark
# instances are few but requests run many flows, so always ask for full pages.
_PAGE_SIZE = 10000


class FlowableAdapter:
    def __init__(self, base_url: str | None = None, auth: tuple[str, str] = DEFAULT_AUTH) -> None:
        configured = (base_url or get_settings().engine_flowable_url).rstrip("/")
        # Normalize: the configured URL may or may not carry the /service suffix.
        self.base_url = configured[: -len("/service")] if configured.endswith("/service") else configured
        self._service = f"{self.base_url}/service"
        self._external = f"{self.base_url}/external-job-api"
        self._client = httpx.Client(timeout=30.0, auth=auth)

    # ---- variable codec (Flowable variableDto) -----------------------------

    @staticmethod
    def _to_variables(data: dict | None) -> list[dict]:
        out: list[dict] = []
        for key, value in (data or {}).items():
            if isinstance(value, bool):
                out.append({"name": key, "value": value, "type": "boolean"})
            elif isinstance(value, int):
                out.append({"name": key, "value": value, "type": "integer"})
            elif isinstance(value, float):
                out.append({"name": key, "value": value, "type": "double"})
            elif isinstance(value, (dict, list)):
                out.append({"name": key, "value": value, "type": "json"})
            else:
                out.append({"name": key, "value": str(value), "type": "string"})
        return out

    # ---- lifecycle ---------------------------------------------------------

    def deploy_process(self, bpmn_xml: str, process_key: str, name: str | None = None) -> DeploymentInfo:
        # Flowable derives the stored deployment name from the uploaded file's
        # base name (it ignores the `deploymentName` form field), so pass the
        # requested name through the filename itself.
        deployment_name = name or process_key
        resp = self._client.post(
            f"{self._service}/repository/deployments",
            files={
                "file": (f"{deployment_name}.bpmn", bpmn_xml.encode("utf-8"), "text/xml"),
            },
        )
        resp.raise_for_status()
        body = resp.json()
        # The deployment response does NOT list the definitions; verify the key
        # is now resolvable and return the deployment id.
        definitions = self.get_process_definitions(process_key)
        if not definitions:
            raise RuntimeError(f"deployment created but no process definition for key {process_key}")
        return DeploymentInfo(deployment_id=body["id"], process_key=process_key)

    def start_process(
        self,
        process_key: str,
        business_key: str | None = None,
        variables: dict | None = None,
        version: int | None = None,
    ) -> ProcessInstanceInfo:
        payload: dict = {"variables": self._to_variables(variables or {})}
        if business_key:
            payload["businessKey"] = business_key
        if version is None:
            payload["processDefinitionKey"] = process_key
        else:
            target = next(
                (d for d in self.get_process_definitions(process_key) if d["version"] == version), None
            )
            if target is None:
                raise RuntimeError(f"process {process_key} version {version} not deployed")
            payload["processDefinitionId"] = target["id"]
        resp = self._client.post(f"{self._service}/runtime/process-instances", json=payload)
        resp.raise_for_status()
        body = resp.json()
        return ProcessInstanceInfo(
            process_instance_id=body["id"], state="ACTIVE", business_key=body.get("businessKey")
        )

    def cancel_process(self, process_instance_id: str, reason: str | None = None) -> None:
        # Cancellation races with the engine's async executor (timer/external-job
        # handling); PostgreSQL can report a transient deadlock. Retry briefly.
        for attempt in range(3):
            resp = self._client.delete(
                f"{self._service}/runtime/process-instances/{process_instance_id}",
                params={"deleteReason": reason or "benchmark-cancellation"},
            )
            if resp.status_code == 404:  # instance already ended/removed -> no-op
                return
            if resp.status_code < 500:
                resp.raise_for_status()
                return
            time.sleep(0.5 * (attempt + 1))
        resp.raise_for_status()

    # ---- queries -----------------------------------------------------------

    def get_process_instance(self, process_instance_id: str) -> ProcessInstanceInfo:
        resp = self._client.get(f"{self._service}/runtime/process-instances/{process_instance_id}")
        if resp.status_code == 404:
            return ProcessInstanceInfo(process_instance_id=process_instance_id, state="ENDED")
        resp.raise_for_status()
        body = resp.json()
        if body.get("ended"):
            state = "ENDED"
        elif body.get("suspended"):
            state = "SUSPENDED"
        else:
            state = "ACTIVE"
        return ProcessInstanceInfo(
            process_instance_id=body["id"], state=state, business_key=body.get("businessKey")
        )

    def get_active_human_tasks(self, process_instance_id: str | None = None) -> list[EngineTask]:
        params: dict = {"size": _PAGE_SIZE}
        if process_instance_id:
            params["processInstanceId"] = process_instance_id
        resp = self._client.get(f"{self._service}/runtime/tasks", params=params)
        resp.raise_for_status()
        tasks: list[EngineTask] = []
        for t in resp.json().get("data", []):
            tasks.append(
                EngineTask(
                    id=t["id"],
                    task_definition_key=t.get("taskDefinitionKey") or t.get("name") or t["id"],
                    process_instance_id=t.get("processInstanceId", ""),
                    priority=int(t.get("priority", 0)),
                )
            )
        return tasks

    def get_human_task(self, external_task_id: str) -> EngineTask | None:
        # Flowable removes completed tasks from the runtime tables, so a 404
        # here means the requested state (task completed) is already true.
        resp = self._client.get(f"{self._service}/runtime/tasks/{external_task_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        t = resp.json()
        return EngineTask(
            id=t["id"],
            task_definition_key=t.get("taskDefinitionKey") or t.get("name") or t["id"],
            process_instance_id=t.get("processInstanceId", ""),
            priority=int(t.get("priority", 0)),
        )

    def complete_human_task(self, external_task_id: str, variables: dict | None = None) -> None:
        resp = self._client.post(
            f"{self._service}/runtime/tasks/{external_task_id}",
            json={"action": "complete", "variables": self._to_variables(variables or {})},
        )
        resp.raise_for_status()

    def find_process_instance_by_business_key(
        self, process_key: str, business_key: str
    ) -> list[ProcessInstanceInfo]:
        seen: dict[str, ProcessInstanceInfo] = {}
        runtime = self._client.get(
            f"{self._service}/runtime/process-instances",
            params={"processDefinitionKey": process_key, "businessKey": business_key, "size": _PAGE_SIZE},
        )
        runtime.raise_for_status()
        for inst in runtime.json().get("data", []):
            seen[inst["id"]] = ProcessInstanceInfo(
                process_instance_id=inst["id"], state="ACTIVE", business_key=inst.get("businessKey")
            )
        historic = self._client.get(
            f"{self._service}/history/historic-process-instances",
            params={"processDefinitionKey": process_key, "businessKey": business_key, "size": _PAGE_SIZE},
        )
        historic.raise_for_status()
        for inst in historic.json().get("data", []):
            if inst["id"] not in seen:
                seen[inst["id"]] = ProcessInstanceInfo(
                    process_instance_id=inst["id"], state="ENDED", business_key=inst.get("businessKey")
                )
        return list(seen.values())

    def get_process_definitions(self, process_key: str) -> list[dict]:
        """All deployed versions of a process key (id, key, version, deploymentId, ...)."""
        resp = self._client.get(
            f"{self._service}/repository/process-definitions",
            params={"key": process_key, "size": _PAGE_SIZE},
        )
        resp.raise_for_status()
        return resp.json().get("data", [])

    def get_instance_definition_version(self, process_instance_id: str) -> int | None:
        resp = self._client.get(f"{self._service}/runtime/process-instances/{process_instance_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        definition_id = resp.json().get("processDefinitionId")
        if not definition_id:
            return None
        definition = self._client.get(f"{self._service}/repository/process-definitions/{definition_id}")
        definition.raise_for_status()
        return definition.json().get("version")

    def get_active_activity_ids(self, process_instance_id: str) -> set[str]:
        """Currently active wait-state activity ids (from the execution tree).

        Flowable exposes active executions; each active wait state (user task,
        timer, external job) carries a non-null `activityId`.
        """
        resp = self._client.get(
            f"{self._service}/runtime/executions",
            params={"processInstanceId": process_instance_id, "size": _PAGE_SIZE},
        )
        if resp.status_code == 404:
            return set()
        resp.raise_for_status()
        return {
            exec_["activityId"]
            for exec_ in resp.json().get("data", [])
            if exec_.get("activityId")
        }

    def get_process_history(self, process_instance_id: str) -> list[HistoryEvent]:
        resp = self._client.get(
            f"{self._service}/history/historic-activity-instances",
            params={"processInstanceId": process_instance_id, "size": _PAGE_SIZE},
        )
        resp.raise_for_status()
        events: list[HistoryEvent] = []
        for a in resp.json().get("data", []):
            events.append(
                HistoryEvent(
                    activity_id=a.get("activityId"),
                    activity_type=a.get("activityType"),
                    event_type="ended" if a.get("endTime") else "started",
                    started_at=a.get("startTime"),
                    ended_at=a.get("endTime"),
                )
            )
        return events

    def get_failed_jobs(self, process_instance_id: str | None = None) -> list[FailedJobInfo]:
        """Jobs that failed permanently land in the dead-letter table."""
        params: dict = {"size": _PAGE_SIZE}
        if process_instance_id:
            params["processInstanceId"] = process_instance_id
        resp = self._client.get(f"{self._service}/management/deadletter-jobs", params=params)
        resp.raise_for_status()
        jobs: list[FailedJobInfo] = []
        for j in resp.json().get("data", []):
            jobs.append(
                FailedJobInfo(
                    job_id=j["id"],
                    process_instance_id=j.get("processInstanceId", ""),
                    retries=int(j.get("retries", 0)),
                    exception_message=j.get("exceptionMessage"),
                )
            )
        return jobs

    # ---- engine-agnostic helpers used by fixture/cleanup tooling -----------

    def get_running_instances(self, process_key: str) -> list[str]:
        """Ids of currently running instances of a process key."""
        resp = self._client.get(
            f"{self._service}/runtime/process-instances",
            params={"processDefinitionKey": process_key, "size": _PAGE_SIZE},
        )
        resp.raise_for_status()
        return [inst["id"] for inst in resp.json().get("data", [])]

    def delete_deployment(self, deployment_id: str) -> None:
        resp = self._client.delete(
            f"{self._service}/repository/deployments/{deployment_id}", params={"cascade": "true"}
        )
        resp.raise_for_status()

    # ---- Flowable external-job protocol (used by the worker) ---------------

    def fetch_external_jobs(
        self,
        topic: str,
        worker_id: str,
        max_tasks: int = 5,
        lock_duration_ms: int = 30000,
    ) -> list[dict]:
        """Acquire+lock up to max_tasks external jobs of a single topic."""
        resp = self._client.post(
            f"{self._external}/acquire/jobs",
            json={
                "workerId": worker_id,
                "topic": topic,
                "lockDuration": f"PT{lock_duration_ms / 1000.0:g}S",
                "numberOfTasks": max_tasks,
            },
        )
        resp.raise_for_status()
        return resp.json()

    def complete_external_job(self, job_id: str, worker_id: str, variables: dict | None = None) -> None:
        resp = self._client.post(
            f"{self._external}/acquire/jobs/{job_id}/complete",
            json={"workerId": worker_id, "variables": self._to_variables(variables or {})},
        )
        resp.raise_for_status()

    def fail_external_job(
        self,
        job_id: str,
        worker_id: str,
        error_message: str,
        error_details: str | None = None,
        retries: int | None = None,
        retry_timeout_ms: int | None = None,
    ) -> None:
        body: dict = {"workerId": worker_id, "errorMessage": error_message}
        if error_details:
            body["errorDetails"] = error_details
        if retries is not None:
            body["retries"] = retries
        if retry_timeout_ms is not None:
            body["retryTimeout"] = f"PT{retry_timeout_ms / 1000.0:g}S"
        resp = self._client.post(f"{self._external}/acquire/jobs/{job_id}/fail", json=body)
        resp.raise_for_status()

    # ---- timer handling ----------------------------------------------------

    def fire_timer(self, process_instance_id: str) -> int:
        """Deterministically fire all pending timer jobs of an instance.

        Moves timer jobs into the executable queue so they run immediately
        instead of waiting for the natural due date (used by benchmark
        scenarios to keep timings deterministic).
        """
        resp = self._client.get(
            f"{self._service}/management/timer-jobs",
            params={"processInstanceId": process_instance_id, "size": _PAGE_SIZE},
        )
        resp.raise_for_status()
        fired = 0
        for job in resp.json().get("data", []):
            move = self._client.post(
                f"{self._service}/management/timer-jobs/{job['id']}", json={"action": "move"}
            )
            move.raise_for_status()
            fired += 1
        return fired

    def close(self) -> None:
        self._client.close()
