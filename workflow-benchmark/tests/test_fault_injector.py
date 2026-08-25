"""Unit tests for the fault controller remaining=N semantics (audit A05).

A fault armed with remaining=N must inject EXACTLY N times; once remaining
reaches 0 it must never fire again. remaining=0 at arm time means never fire.
"""

import pytest

import app.workflow.fault_injector as mod
from app.workflow.fault_injector import (
    FaultController,
    FaultInjectingAdapter,
    _SimulatedConnectionError,
    maybe_wrap,
)


class _Inner:
    def __init__(self) -> None:
        self.calls = 0
        self.ok = 0

    def deploy_process(self, bpmn_xml, process_key, name=None):
        self.calls += 1
        self.ok += 1
        return {"deployment_id": f"dep-{self.calls}"}

    def start_process(self, *args, **kwargs):
        self.calls += 1
        self.ok += 1
        return {"id": f"pi-{self.calls}"}

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _fresh_controller():
    ctrl = FaultController()
    import app.workflow.fault_injector as mod

    mod.controller = ctrl
    yield ctrl
    mod.controller = FaultController()


def test_fail_mode_injects_exactly_n_times():
    controller = mod.controller
    controller.arm("OPERATON", "start_process", "fail", remaining=2)
    inner = _Inner()
    adapter = FaultInjectingAdapter("OPERATON", inner)

    for i in range(2):
        with pytest.raises(_SimulatedConnectionError):
            adapter.start_process("p", "b")
        assert inner.ok == 0

    # third call: fault exhausted -> real call proceeds
    result = adapter.start_process("p", "b")
    assert inner.calls == 1 and inner.ok == 1
    assert result == {"id": "pi-1"}

    spec = controller.spec("OPERATON", "start_process")
    assert spec.remaining == 0
    assert spec.injected == 2


def test_loss_mode_injects_exactly_n_times_then_stops():
    controller = mod.controller
    controller.arm("OPERATON", "deploy_process", "loss", remaining=1)
    inner = _Inner()
    adapter = FaultInjectingAdapter("OPERATON", inner)

    with pytest.raises(_SimulatedConnectionError):
        adapter.deploy_process("<x/>", "p")
    assert inner.ok == 1  # real engine call happened, response hidden

    # exhausted: no more injection, response passes through
    result = adapter.deploy_process("<x/>", "p")
    assert inner.ok == 2
    assert result == {"deployment_id": "dep-2"}


def test_arm_with_remaining_zero_never_injects():
    controller = mod.controller
    controller.arm("OPERATON", "start_process", "fail", remaining=0)
    inner = _Inner()
    adapter = FaultInjectingAdapter("OPERATON", inner)

    for _ in range(3):
        adapter.start_process("p", "b")
    assert inner.calls == 3 and inner.ok == 3


def test_maybe_wrap_does_not_wrap_exhausted_arm():
    controller = mod.controller
    controller.arm("OPERATON", "start_process", "fail", remaining=1)
    inner = _Inner()
    adapter = FaultInjectingAdapter("OPERATON", inner)
    with pytest.raises(_SimulatedConnectionError):
        adapter.start_process("p", "b")
    # after exhaustion the arm must not trigger wrapping any more
    wrapped = maybe_wrap("OPERATON", inner)
    assert wrapped is inner


def test_snapshot_reports_injected_and_remaining():
    controller = mod.controller
    controller.arm("OPERATON", "start_process", "fail", remaining=1)
    controller.arm("OPERATON", "complete_human_task", "loss", remaining=-1)
    snap = controller.snapshot()
    assert snap["OPERATON"]["start_process"]["remaining"] == 1
    assert snap["OPERATON"]["start_process"]["injected"] == 0
    assert snap["OPERATON"]["complete_human_task"]["remaining"] == -1
