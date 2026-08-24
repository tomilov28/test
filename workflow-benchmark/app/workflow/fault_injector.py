"""Fault injection for the benchmark's fault-recovery phase.

This wraps a real engine adapter at the dispatcher boundary. It is a test /
benchmark control surface only: nothing is armed by default, so the harness
behaves exactly like the production path until a scenario arms a fault through
the admin API (POST /admin/faults/arm).

Two failure modes are supported:

  * "loss": the real engine call is made and MUST succeed first; only then does
    the wrapper hide the response and raise a simulated connection error. This
    reproduces an AMBIGUOUS outcome (the engine acted, the dispatcher saw a
    failure) rather than a pre-request failure.
  * "fail": a simulated technical failure (connection error) is raised before
    the engine is contacted, on every dispatch. Used to exhaust the outbox's
    technical retries.

The controller is process-global so arms survive adapter instances (the
dispatcher builds a fresh adapter per command dispatch via build_adapter).
"""

import logging

import httpx

logger = logging.getLogger(__name__)

SUPPORTED_OPERATIONS = (
    "deploy_process",
    "start_process",
    "cancel_process",
    "complete_human_task",
)


class _FaultSpec:
    def __init__(self, mode: str, remaining: int) -> None:
        self.mode = mode
        self.remaining = remaining
        self.injected = 0

    def consume(self) -> None:
        if self.remaining > 0:
            self.remaining -= 1


class FaultController:
    """Global registry of armed faults, keyed by engine name + operation."""

    def __init__(self) -> None:
        self._arms: dict[str, dict[str, _FaultSpec]] = {}

    def arm(self, engine: str, operation: str, mode: str, remaining: int = -1) -> None:
        if operation not in SUPPORTED_OPERATIONS:
            raise ValueError(f"unsupported fault operation: {operation}")
        if mode not in ("loss", "fail"):
            raise ValueError(f"unsupported fault mode: {mode}")
        self._arms.setdefault(engine, {})[operation] = _FaultSpec(mode, remaining)
        logger.warning("fault armed engine=%s op=%s mode=%s remaining=%s", engine, operation, mode, remaining)

    def clear(self, engine: str | None = None, operation: str | None = None) -> int:
        cleared = 0
        if engine is None:
            for specs in self._arms.values():
                cleared += len(specs)
            self._arms.clear()
            return cleared
        specs = self._arms.get(engine, {})
        if operation is None:
            cleared = len(specs)
            self._arms.pop(engine, None)
        else:
            if specs.pop(operation, None) is not None:
                cleared = 1
        return cleared

    def spec(self, engine: str, operation: str) -> _FaultSpec | None:
        return self._arms.get(engine, {}).get(operation)

    def snapshot(self) -> dict:
        return {
            engine: {
                op: {"mode": spec.mode, "remaining": spec.remaining, "injected": spec.injected}
                for op, spec in ops.items()
            }
            for engine, ops in self._arms.items()
        }


controller = FaultController()


class _SimulatedConnectionError(httpx.ConnectError):
    def __init__(self) -> None:
        super().__init__("simulated connection error (fault injection)")


class FaultInjectingAdapter:
    """Wraps a real adapter and injects faults per (engine, operation)."""

    def __init__(self, engine: str, inner) -> None:
        self._engine = engine
        self._inner = inner

    # -- injection plumbing ---------------------------------------------------

    def _inject_fail(self, operation: str) -> None:
        spec = controller.spec(self._engine, operation)
        if spec is not None and spec.mode == "fail":
            spec.injected += 1
            spec.consume()
            logger.warning("fault injected engine=%s op=%s mode=fail", self._engine, operation)
            raise _SimulatedConnectionError()

    def _inject_loss(self, operation: str) -> None:
        spec = controller.spec(self._engine, operation)
        if spec is None or spec.mode != "loss":
            return
        # The real call succeeded (caller is about to return); hide it.
        spec.injected += 1
        spec.consume()
        logger.warning(
            "fault injected engine=%s op=%s mode=loss (real engine call succeeded, response hidden)",
            self._engine,
            operation,
        )
        raise _SimulatedConnectionError()

    # -- lifecycle ------------------------------------------------------------

    def deploy_process(self, bpmn_xml: str, process_key: str, name: str | None = None):
        self._inject_fail("deploy_process")
        result = self._inner.deploy_process(bpmn_xml, process_key, name)
        self._inject_loss("deploy_process")
        return result

    def start_process(
        self, process_key: str, business_key: str | None = None, variables=None, version: int | None = None
    ):
        self._inject_fail("start_process")
        result = self._inner.start_process(process_key, business_key, variables, version)
        self._inject_loss("start_process")
        return result

    def cancel_process(self, process_instance_id: str, reason: str | None = None) -> None:
        self._inject_fail("cancel_process")
        self._inner.cancel_process(process_instance_id, reason)
        self._inject_loss("cancel_process")

    def complete_human_task(self, external_task_id: str, variables=None) -> None:
        self._inject_fail("complete_human_task")
        self._inner.complete_human_task(external_task_id, variables)
        self._inject_loss("complete_human_task")

    # -- queries (never fault-injected: they are read-only reconciliation) ----

    def get_process_instance(self, process_instance_id):
        return self._inner.get_process_instance(process_instance_id)

    def get_active_human_tasks(self, process_instance_id: str | None = None):
        return self._inner.get_active_human_tasks(process_instance_id)

    def get_human_task(self, external_task_id: str):
        return self._inner.get_human_task(external_task_id)

    def find_process_instance_by_business_key(self, process_key: str, business_key: str):
        return self._inner.find_process_instance_by_business_key(process_key, business_key)

    def get_process_history(self, process_instance_id: str):
        return self._inner.get_process_history(process_instance_id)

    def get_failed_jobs(self, process_instance_id: str | None = None):
        return self._inner.get_failed_jobs(process_instance_id)

    def get_process_definitions(self, process_key: str):
        return self._inner.get_process_definitions(process_key)

    def get_instance_definition_version(self, process_instance_id: str):
        return self._inner.get_instance_definition_version(process_instance_id)

    def get_active_activity_ids(self, process_instance_id: str):
        return self._inner.get_active_activity_ids(process_instance_id)

    def get_running_instances(self, process_key: str):
        return self._inner.get_running_instances(process_key)

    def delete_deployment(self, deployment_id: str) -> None:
        self._inner.delete_deployment(deployment_id)

    def fetch_external_jobs(self, *args, **kwargs):
        return self._inner.fetch_external_jobs(*args, **kwargs)

    def complete_external_job(self, *args, **kwargs):
        return self._inner.complete_external_job(*args, **kwargs)

    def fail_external_job(self, *args, **kwargs):
        return self._inner.fail_external_job(*args, **kwargs)

    def fire_timer(self, process_instance_id: str) -> int:
        return self._inner.fire_timer(process_instance_id)

    def close(self) -> None:
        self._inner.close()


def maybe_wrap(engine: str, adapter) -> "FaultInjectingAdapter":
    """Wrap an adapter when a fault is armed for its engine, else return as-is."""
    if controller.snapshot().get(engine):
        return FaultInjectingAdapter(engine, adapter)
    return adapter
