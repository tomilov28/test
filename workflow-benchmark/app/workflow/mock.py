"""In-memory engine adapter used ONLY for tests and local demos.

Keeps the outbox dispatcher / reconciler testable while the real Operaton and
Flowable adapters are still skeletons. Not wired into production configuration.
"""

import itertools
import uuid

from app.workflow.base import (
    DeploymentInfo,
    EngineTask,
    FailedJobInfo,
    HistoryEvent,
    ProcessInstanceInfo,
)


class MockAdapter:
    def __init__(self) -> None:
        self._seq = itertools.count(1)
        self.deployments: dict[str, DeploymentInfo] = {}
        self.process_instances: dict[str, ProcessInstanceInfo] = {}
        self.tasks: dict[str, EngineTask] = {}
        self.completed: list[str] = []
        self.cancelled: list[str] = []
        self.fail_start: bool = False
        self.fail_complete: bool = False

    # ---- lifecycle ---------------------------------------------------------

    def deploy_process(self, bpmn_xml: str, process_key: str, name: str | None = None) -> DeploymentInfo:
        info = DeploymentInfo(deployment_id=f"dep-{next(self._seq)}", process_key=process_key)
        self.deployments[process_key] = info
        return info

    def start_process(
        self,
        process_key: str,
        business_key: str | None = None,
        variables: dict | None = None,
        version: int | None = None,
    ) -> ProcessInstanceInfo:
        if self.fail_start:
            raise RuntimeError("mock: start_process failure injected")
        instance = ProcessInstanceInfo(
            process_instance_id=f"pi-{next(self._seq)}", state="ACTIVE", business_key=business_key
        )
        self.process_instances[instance.process_instance_id] = instance
        return instance

    def cancel_process(self, process_instance_id: str, reason: str | None = None) -> None:
        if process_instance_id in self.process_instances:
            self.process_instances[process_instance_id].state = "CANCELLED"
        self.cancelled.append(process_instance_id)

    # ---- queries -----------------------------------------------------------

    def get_process_instance(self, process_instance_id: str) -> ProcessInstanceInfo:
        # Unknown ids default to ACTIVE so the dispatcher's idempotent-cancel
        # state check still routes through cancel_process (matches real engine
        # behavior for instances the mock never explicitly registered).
        return self.process_instances.get(
            process_instance_id,
            ProcessInstanceInfo(process_instance_id=process_instance_id, state="ACTIVE"),
        )

    def get_active_human_tasks(self, process_instance_id: str | None = None) -> list[EngineTask]:
        return [
            t
            for t in self.tasks.values()
            if (process_instance_id is None or t.process_instance_id == process_instance_id)
        ]

    def get_human_task(self, external_task_id: str) -> EngineTask | None:
        return self.tasks.get(external_task_id)

    def find_process_instance_by_business_key(
        self, process_key: str, business_key: str
    ) -> list[ProcessInstanceInfo]:
        return [
            inst
            for inst in self.process_instances.values()
            if inst.business_key == business_key
        ]

    def complete_human_task(self, external_task_id: str, variables: dict | None = None) -> None:
        if self.fail_complete:
            raise RuntimeError("mock: complete failure injected")
        if external_task_id in self.tasks:
            del self.tasks[external_task_id]
        self.completed.append(external_task_id)

    def get_process_history(self, process_instance_id: str) -> list[HistoryEvent]:
        return []

    def get_failed_jobs(self, process_instance_id: str | None = None) -> list[FailedJobInfo]:
        return []
