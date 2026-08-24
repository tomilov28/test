import pytest

from app.domain.enums import WorkflowEngine
from app.workflow.base import EngineTask
from app.workflow.flowable import FlowableAdapter
from app.workflow.mock import MockAdapter
from app.workflow.operaton import OperatonAdapter
from app.workflow.registry import build_adapter


def test_build_adapter_maps_engines():
    assert isinstance(build_adapter(WorkflowEngine.OPERATON.value), OperatonAdapter)
    assert isinstance(build_adapter(WorkflowEngine.FLOWABLE.value), FlowableAdapter)


def test_build_adapter_rejects_unknown():
    with pytest.raises(ValueError):
        build_adapter("UNKNOWN")


def test_mock_adapter_full_cycle():
    mock = MockAdapter()
    deployment = mock.deploy_process("<bpmn/>", "credit_decision")
    instance = mock.start_process(deployment.process_key, business_key="REQ-1")
    assert instance.process_instance_id is not None
    mock.tasks["t1"] = EngineTask(id="t1", task_definition_key="review", process_instance_id=instance.process_instance_id)
    tasks = mock.get_active_human_tasks(instance.process_instance_id)
    assert [t.id for t in tasks] == ["t1"]
    mock.complete_human_task("t1")
    assert mock.get_active_human_tasks(instance.process_instance_id) == []
