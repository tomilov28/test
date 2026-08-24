"""Fault injection scaffolding for later benchmark phases.

Benchmark scenarios need to observe how each engine behaves when external
dependencies fail or when calls time out. These helpers wrap a real adapter
with controllable failure modes. Intentionally minimal.
"""

import logging
import random
from typing import Callable

logger = logging.getLogger(__name__)


class FaultConfig:
    def __init__(
        self,
        fail_probability: float = 0.0,
        timeout_seconds: float = 0.0,
        fail_methods: set[str] | None = None,
    ) -> None:
        self.fail_probability = fail_probability
        self.timeout_seconds = timeout_seconds
        self.fail_methods = fail_methods or set()

    @classmethod
    def disabled(cls) -> "FaultConfig":
        return cls()


class FaultyAdapterProxy:
    """Wraps an adapter and injects failures/timeouts per call."""

    def __init__(self, delegate, config: FaultConfig | None = None) -> None:
        self._delegate = delegate
        self._config = config or FaultConfig.disabled()
        self.injected_failures = 0

    def _maybe_fail(self, method: str) -> None:
        if self._config.fail_methods and method not in self._config.fail_methods:
            return
        if random.random() < self._config.fail_probability:
            self.injected_failures += 1
            raise TimeoutError(f"fault injection: {method} simulated timeout")

    def _guard(self, method: str, call: Callable):
        self._maybe_fail(method)
        return call()

    def __getattr__(self, name: str):
        delegate = getattr(self._delegate, name)

        def wrapper(*args, **kwargs):
            return self._guard(name, lambda: delegate(*args, **kwargs))

        return wrapper

    def close(self):
        self._delegate.close()
