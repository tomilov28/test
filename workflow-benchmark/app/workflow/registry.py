from app.domain.enums import WorkflowEngine
from app.workflow.base import WorkflowAdapter
from app.workflow.fault_injector import controller, maybe_wrap
from app.workflow.flowable import FlowableAdapter
from app.workflow.operaton import OperatonAdapter


def build_adapter(engine: str) -> WorkflowAdapter:
    """Return a real engine adapter for the given engine name.

    When a fault is armed for the engine (test/benchmark control surface), the
    adapter is wrapped so the dispatcher observes the injected failure at its
    adapter boundary. Nothing is wrapped in production default state.
    """
    if engine == WorkflowEngine.OPERATON.value:
        adapter: WorkflowAdapter = OperatonAdapter()
    elif engine == WorkflowEngine.FLOWABLE.value:
        adapter = FlowableAdapter()
    else:
        raise ValueError(f"Unsupported workflow engine: {engine}")
    return maybe_wrap(engine, adapter)
