from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class DeploymentInfo:
    deployment_id: str
    process_key: str


@dataclass
class ProcessInstanceInfo:
    process_instance_id: str
    state: str
    business_key: str | None = None


@dataclass
class EngineTask:
    id: str
    task_definition_key: str
    process_instance_id: str
    priority: int = 0
    variables: dict[str, Any] = field(default_factory=dict)


@dataclass
class HistoryEvent:
    activity_id: str | None = None
    activity_type: str | None = None
    event_type: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class FailedJobInfo:
    job_id: str
    process_instance_id: str
    retries: int
    exception_message: str | None = None


class WorkflowAdapter(Protocol):
    """Vendor-neutral facade over a BPMN engine's public REST API.

    Deliberately NOT a perfect universal BPM abstraction. It keeps the real
    differences between engines visible so the benchmark can compare them.
    """

    def deploy_process(self, bpmn_xml: str, process_key: str, name: str | None = None) -> DeploymentInfo: ...

    def start_process(
        self, process_key: str, business_key: str | None = None, variables: dict[str, Any] | None = None
    ) -> ProcessInstanceInfo: ...

    def cancel_process(self, process_instance_id: str, reason: str | None = None) -> None: ...

    def get_process_instance(self, process_instance_id: str) -> ProcessInstanceInfo: ...

    def get_active_human_tasks(self, process_instance_id: str | None = None) -> list[EngineTask]: ...

    def get_human_task(self, external_task_id: str) -> EngineTask | None:
        """Look up a single runtime task; None when it no longer exists (e.g.
        already completed/removed). Used for idempotent task completion."""
        ...

    def complete_human_task(self, external_task_id: str, variables: dict[str, Any] | None = None) -> None: ...

    def find_process_instance_by_business_key(
        self, process_key: str, business_key: str
    ) -> list[ProcessInstanceInfo]:
        """Every instance ever created for a business key (runtime + history).
        Used to make START_PROCESS idempotent under at-least-once retries."""
        ...

    def get_process_history(self, process_instance_id: str) -> list[HistoryEvent]: ...

    def get_failed_jobs(self, process_instance_id: str | None = None) -> list[FailedJobInfo]: ...
