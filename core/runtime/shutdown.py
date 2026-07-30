"""Bounded graceful shutdown coordination for application runtime services."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import math
import time

from core.runtime.admission import RuntimeAdmissionGate, RuntimeAdmissionState
from core.runtime.application_services import (
    ApplicationRuntimeServices,
    RuntimeComponentResult,
)
from core.runtime.cancellation import CancellationReason


def _finite_timeout(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise ValueError(f"{name} must be a finite non-negative number")
    return float(value)


@dataclass(frozen=True, slots=True)
class ShutdownReport:
    state: RuntimeAdmissionState
    active_run_count: int
    cancelled_run_count: int
    forced_run_count: int
    remaining_run_count: int
    detached_worker_count: int
    components: tuple[RuntimeComponentResult, ...]

    @property
    def completed(self) -> bool:
        return self.state is RuntimeAdmissionState.CLOSED

    @property
    def error_codes(self) -> tuple[str, ...]:
        return tuple(
            item.error_code
            for item in self.components
            if item.error_code is not None
        )


class GracefulShutdownCoordinator:
    """The sole owner of the application shutdown sequence."""

    def __init__(
        self,
        services: ApplicationRuntimeServices,
        *,
        shutdown_grace_seconds: float,
        component_timeout_seconds: float,
    ) -> None:
        if not isinstance(services, ApplicationRuntimeServices):
            raise TypeError("services must be ApplicationRuntimeServices")
        self._services = services
        self._gate: RuntimeAdmissionGate = services.admission_gate
        self._shutdown_grace_seconds = _finite_timeout(
            shutdown_grace_seconds, "shutdown_grace_seconds"
        )
        self._component_timeout_seconds = _finite_timeout(
            component_timeout_seconds, "component_timeout_seconds"
        )
        self._lock = asyncio.Lock()
        self._report: ShutdownReport | None = None

    async def shutdown(self) -> ShutdownReport:
        async with self._lock:
            if self._report is not None:
                return self._report

            components: list[RuntimeComponentResult] = []
            self._gate.close_admission()
            await self._record_call(
                components,
                "runtime_admission",
                lambda: asyncio.to_thread(
                    self._gate.wait_until_settled,
                    self._component_timeout_seconds,
                ),
                timeout=self._component_timeout_seconds,
                false_error="RUNTIME_ADMISSION_SETTLE_TIMEOUT",
            )

            handles = self._services.run_registry.active_handles()
            cancelled_count = sum(
                handle.request_cancel(CancellationReason.SERVER_SHUTDOWN)
                for handle in handles
            )

            await self._record_call(
                components,
                "active_run_drain",
                lambda: asyncio.to_thread(
                    self._services.run_registry.wait_until_empty,
                    self._shutdown_grace_seconds,
                ),
                timeout=self._shutdown_grace_seconds + 0.05,
                result_ok=lambda remaining: not remaining,
                false_error="RUNTIME_RUN_DRAIN_TIMEOUT",
            )

            remaining_handles = self._services.run_registry.active_handles()
            forced_count = 0
            for handle in remaining_handles:
                try:
                    await asyncio.wait_for(
                        handle.force_abort(CancellationReason.SERVER_SHUTDOWN),
                        timeout=self._component_timeout_seconds,
                    )
                    forced_count += 1
                except Exception:
                    # Registry cleanup below remains best effort and bounded.
                    pass
                finally:
                    if (
                        self._services.run_registry.get(handle.run_id)
                        is handle
                    ):
                        self._services.run_registry.unregister(handle.run_id)

            admission_report = await self._services.close_worker_admission(
                self._component_timeout_seconds
            )
            components.extend(admission_report.components)
            worker_report = await self._services.wait_workers(
                self._component_timeout_seconds
            )
            components.extend(worker_report.components)

            flush_report = await self._services.flush(
                self._component_timeout_seconds
            )
            components.extend(flush_report.components)
            close_models = worker_report.completed
            if not close_models:
                components.extend(
                    RuntimeComponentResult(
                        component=component,
                        status="DEFERRED",
                        duration_seconds=0.0,
                        error_code=(
                            "RUNTIME_MODEL_CLOSE_DEFERRED_ACTIVE_WORKER"
                        ),
                    )
                    for component, _resource
                    in self._services.extra_closeables
                    if component.startswith("model_")
                )
            close_report = await self._services.close(
                self._component_timeout_seconds,
                close_models=close_models,
            )
            components.extend(close_report.components)

            self._gate.mark_closed()
            remaining_count = len(
                self._services.run_registry.active_handles()
            )
            detached_worker_count = sum(
                self._detached_count(tracker)
                for tracker in (
                    *self._services.worker_trackers,
                    *self._services.blocking_executors,
                )
            )
            self._report = ShutdownReport(
                state=self._gate.state,
                active_run_count=len(handles),
                cancelled_run_count=cancelled_count,
                forced_run_count=forced_count,
                remaining_run_count=remaining_count,
                detached_worker_count=detached_worker_count,
                components=tuple(components),
            )
            return self._report

    async def _record_call(
        self,
        components: list[RuntimeComponentResult],
        component: str,
        operation,
        *,
        timeout: float,
        false_error: str,
        result_ok=lambda result: bool(result),
    ) -> None:
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(operation(), timeout=timeout)
            error_code = None if result_ok(result) else false_error
        except TimeoutError:
            error_code = false_error
        except Exception:
            error_code = f"{component.upper()}_FAILED"
        components.append(
            RuntimeComponentResult(
                component=component,
                status="COMPLETED" if error_code is None else "FAILED",
                duration_seconds=max(0.0, time.monotonic() - started),
                error_code=error_code,
            )
        )

    @staticmethod
    def _detached_count(tracker: object) -> int:
        try:
            snapshot = getattr(tracker, "snapshot", None)
            if callable(snapshot):
                return max(0, int(snapshot().detached_count))
            value = getattr(tracker, "detached_worker_count", 0)
            return max(0, int(value() if callable(value) else value))
        except Exception:
            return 0


__all__ = ["GracefulShutdownCoordinator", "ShutdownReport"]
