"""Operaton (Camunda 7 fork) REST adapter.

Operaton is Camunda-compatible: REST API lives at /engine-rest.
Docs: https://docs.operaton.org/docs/documentation/reference/rest/

No business logic lives here; only engine REST calls + variable codecs.
"""

import json

import httpx

from app.config import get_settings
from app.workflow.base import (
    DeploymentInfo,
    EngineTask,
    FailedJobInfo,
    HistoryEvent,
    ProcessInstanceInfo,
)


class OperatonAdapter:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or get_settings().engine_operaton_url).rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, timeout=30.0)

    # ---- variable codec (Camunda variableDto) ------------------------------

    @staticmethod
    def _to_variables(data: dict | None) -> dict:
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

    # ---- lifecycle ---------------------------------------------------------

    def deploy_process(self, bpmn_xml: str, process_key: str, name: str | None = None) -> DeploymentInfo:
        resp = self._client.post(
            "/deployment/create",
            files={
                "data": (f"{process_key}.bpmn", bpmn_xml.encode("utf-8"), "text/xml"),
            },
            data={
                "deployment-name": name or process_key,
                "deploy-changed-only": "false",
                "enable-duplicate-filtering": "false",
            },
        )
        resp.raise_for_status()
        body = resp.json()
        defs = body.get("deployedProcessDefinitions") or {}
        # map is keyed by definition id like "credit_decision:5:<uuid>"
        for def_id, def_meta in defs.items():
            if def_id == process_key or def_id.startswith(process_key + ":"):
                return DeploymentInfo(deployment_id=body["id"], process_key=process_key)
        if defs:
            return DeploymentInfo(deployment_id=body["id"], process_key=process_key)
        raise RuntimeError(f"deployment created but no process definition for key {process_key}")

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
            url = f"/process-definition/key/{process_key}/start"
        else:
            target = next(
                (d for d in self.get_process_definitions(process_key) if d["version"] == version), None
            )
            if target is None:
                raise RuntimeError(f"process {process_key} version {version} not deployed")
            url = f"/process-definition/{target['id']}/start"
        resp = self._client.post(url, json=payload)
        resp.raise_for_status()
        body = resp.json()
        return ProcessInstanceInfo(
            process_instance_id=body["id"], state="ACTIVE", business_key=body.get("businessKey")
        )

    def cancel_process(self, process_instance_id: str, reason: str | None = None) -> None:
        # Operaton uses DELETE /process-instance/{id} (no POST /{id}/delete variant).
        # failIfNotExists=false makes cancelling an already-ended instance a no-op.
        resp = self._client.request(
            "DELETE",
            f"/process-instance/{process_instance_id}",
            params={"skipCustomListeners": "false", "failIfNotExists": "false"},
        )
        resp.raise_for_status()

    # ---- queries -----------------------------------------------------------

    def get_process_instance(self, process_instance_id: str) -> ProcessInstanceInfo:
        resp = self._client.get(f"/process-instance/{process_instance_id}")
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
        params = {"active": "true"}
        if process_instance_id:
            params["processInstanceId"] = process_instance_id
        resp = self._client.get("/task", params=params)
        resp.raise_for_status()
        tasks: list[EngineTask] = []
        for t in resp.json():
            tasks.append(
                EngineTask(
                    id=t["id"],
                    task_definition_key=t.get("taskDefinitionKey") or t.get("name") or t["id"],
                    process_instance_id=t.get("processInstanceId", ""),
                    priority=int(t.get("priority", 0)),
                )
            )
        return tasks

    def complete_human_task(self, external_task_id: str, variables: dict | None = None) -> None:
        resp = self._client.post(
            f"/task/{external_task_id}/complete",
            json={"variables": self._to_variables(variables or {})},
        )
        resp.raise_for_status()

    def get_process_definitions(self, process_key: str) -> list[dict]:
        """All deployed versions of a process key (id, key, version, deploymentId, ...)."""
        resp = self._client.get("/process-definition", params={"key": process_key})
        resp.raise_for_status()
        return resp.json()

    def get_instance_definition_version(self, process_instance_id: str) -> int | None:
        """Version of the process definition an instance is running on."""
        resp = self._client.get(f"/process-instance/{process_instance_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        definition_id = resp.json().get("definitionId")
        if not definition_id:
            return None
        definition = self._client.get(f"/process-definition/{definition_id}")
        definition.raise_for_status()
        return definition.json().get("version")

    def get_active_activity_ids(self, process_instance_id: str) -> set[str]:
        """Currently active wait-state activity ids (from the activity-instance tree)."""
        resp = self._client.get(f"/process-instance/{process_instance_id}/activity-instances")
        if resp.status_code == 404:
            return set()
        resp.raise_for_status()
        tree = resp.json()
        active: set[str] = set()

        def walk(node: dict) -> None:
            for child in node.get("childActivityInstances") or []:
                if child.get("activityId"):
                    active.add(child["activityId"])
                walk(child)
            for transition in node.get("childTransitionInstances") or []:
                if transition.get("active") and transition.get("activityId"):
                    active.add(transition["activityId"])

        walk(tree)
        return active

    def get_process_history(self, process_instance_id: str) -> list[HistoryEvent]:
        resp = self._client.get(
            "/history/activity-instance", params={"processInstanceId": process_instance_id}
        )
        resp.raise_for_status()
        events: list[HistoryEvent] = []
        for a in resp.json():
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
        params: dict = {}
        if process_instance_id:
            params["processInstanceId"] = process_instance_id
        resp = self._client.get("/job", params=params)
        resp.raise_for_status()
        jobs: list[FailedJobInfo] = []
        for j in resp.json():
            if j.get("retries") == 0 or j.get("exceptionMessage"):
                jobs.append(
                    FailedJobInfo(
                        job_id=j["id"],
                        process_instance_id=j.get("processInstanceId", ""),
                        retries=int(j.get("retries", 0)),
                        exception_message=j.get("exceptionMessage"),
                    )
                )
        return jobs

    def close(self) -> None:
        self._client.close()
