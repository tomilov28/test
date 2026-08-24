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
    """Operaton (Camunda 7 fork) REST adapter.

    Phase 1: skeleton. Method stubs document the intended REST mapping and
    raise NotImplementedError until the Operaton benchmark phase lands.
    """

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or get_settings().engine_operaton_url).rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, timeout=10.0)

    # ---- lifecycle ---------------------------------------------------------

    def deploy_process(self, bpmn_xml: str, process_key: str, name: str | None = None) -> DeploymentInfo:
        raise NotImplementedError("Operaton adapter: deploy_process not implemented in bootstrap phase")

    def start_process(
        self, process_key: str, business_key: str | None = None, variables: dict | None = None
    ) -> ProcessInstanceInfo:
        raise NotImplementedError("Operaton adapter: start_process not implemented in bootstrap phase")

    def cancel_process(self, process_instance_id: str, reason: str | None = None) -> None:
        raise NotImplementedError("Operaton adapter: cancel_process not implemented in bootstrap phase")

    # ---- queries -----------------------------------------------------------

    def get_process_instance(self, process_instance_id: str) -> ProcessInstanceInfo:
        raise NotImplementedError("Operaton adapter: get_process_instance not implemented in bootstrap phase")

    def get_active_human_tasks(self, process_instance_id: str | None = None) -> list[EngineTask]:
        raise NotImplementedError("Operaton adapter: get_active_human_tasks not implemented in bootstrap phase")

    def complete_human_task(self, external_task_id: str, variables: dict | None = None) -> None:
        raise NotImplementedError("Operaton adapter: complete_human_task not implemented in bootstrap phase")

    def get_process_history(self, process_instance_id: str) -> list[HistoryEvent]:
        raise NotImplementedError("Operaton adapter: get_process_history not implemented in bootstrap phase")

    def get_failed_jobs(self, process_instance_id: str | None = None) -> list[FailedJobInfo]:
        raise NotImplementedError("Operaton adapter: get_failed_jobs not implemented in bootstrap phase")

    def close(self) -> None:
        self._client.close()
