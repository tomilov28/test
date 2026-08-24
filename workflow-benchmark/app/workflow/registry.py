from app.domain.enums import WorkflowEngine
from app.workflow.base import WorkflowAdapter
from app.workflow.flowable import FlowableAdapter
from app.workflow.operaton import OperatonAdapter


def build_adapter(engine: str) -> WorkflowAdapter:
    """Return a real engine adapter for the given engine name."""
    if engine == WorkflowEngine.OPERATON.value:
        return OperatonAdapter()
    if engine == WorkflowEngine.FLOWABLE.value:
        return FlowableAdapter()
    raise ValueError(f"Unsupported workflow engine: {engine}")
