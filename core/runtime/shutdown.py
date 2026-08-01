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
    RuntimeLifecycleState,
)
from core.runtime.cancellation import CancellationReason
from core.runtime.fault_injection import FaultInjectionController
from core.runtime.fault_injection_contract import FaultPoint, InjectedFaultError
from core.runtime.shutdown_faults import (
    ShutdownFaultTimeoutError,
    execute_shutdown_fault,
    shutdown_run_digest,
)


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
    lifecycle_state: RuntimeLifecycleState
    active_run_count: int
    cancel_requested_count: int
    cancelled_run_count: int
    cancel_failed_count: int
    gracefully_drained_count: int
    forced_run_count: int
    remaining_run_count: int
    worker_drain_status: str
    active_worker_count: int
    detached_worker_count: int
    unknown_worker_count: int
    observability_flush_status: str
    trace_flush_status: str
    duration_seconds: float
    components: tuple[RuntimeComponentResult, ...]

    @property
    def completed(self) -> bool:
        return (
            self.state is RuntimeAdmissionState.CLOSED
            and self.lifecycle_state is RuntimeLifecycleState.CLOSED
        )

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

    async def shutdown(
        self,
        fault_controller: FaultInjectionController | None = None,
    ) -> ShutdownReport:
        async with self._lock:
            if self._report is not None:
                return self._report

            started = time.monotonic()
            components: list[RuntimeComponentResult] = []
            self._services.begin_shutdown()
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
            cancel_requested_count = 0
            cancelled_count = 0
            cancel_failed_count = 0
            for handle in handles:
                if handle.is_completed:
                    continue
                cancel_started = time.monotonic()
                error_code: str | None = None
                try:
                    await execute_shutdown_fault(
                        fault_controller,
                        FaultPoint.SHUTDOWN_BEFORE_RUN_CANCEL,
                        timeout=self._component_timeout_seconds,
                        component="graceful_shutdown",
                        operation_kind="RUN_CANCEL",
                        run_id_digest=shutdown_run_digest(handle.run_id),
                        runtime_mode=_safe_runtime_mode(handle.runtime_mode),
                    )
                    cancel_requested_count += 1
                    if handle.request_cancel(
                        CancellationReason.SERVER_SHUTDOWN
                    ):
                        cancelled_count += 1
                except ShutdownFaultTimeoutError:
                    cancel_failed_count += 1
                    error_code = "RUNTIME_RUN_CANCEL_INJECTED_TIMEOUT"
                except InjectedFaultError:
                    cancel_failed_count += 1
                    error_code = "RUNTIME_RUN_CANCEL_INJECTED_FAILURE"
                except Exception:
                    cancel_failed_count += 1
                    error_code = "RUNTIME_RUN_CANCEL_FAILED"
                components.append(
                    RuntimeComponentResult(
                        component="run_cancel",
                        status=(
                            "COMPLETED" if error_code is None else "FAILED"
                        ),
                        duration_seconds=max(
                            0.0, time.monotonic() - cancel_started
                        ),
                        error_code=error_code,
                    )
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
            gracefully_drained_count = max(
                0, len(handles) - len(remaining_handles)
            )
            forced_count = 0
            for handle in remaining_handles:
                force_started = time.monotonic()
                force_error: str | None = None
                try:
                    await asyncio.wait_for(
                        handle.force_abort(CancellationReason.SERVER_SHUTDOWN),
                        timeout=self._component_timeout_seconds,
                    )
                    forced_count += 1
                except TimeoutError:
                    force_error = "RUNTIME_RUN_FORCE_ABORT_TIMEOUT"
                except Exception:
                    force_error = "RUNTIME_RUN_FORCE_ABORT_FAILED"
                    # Registry cleanup below remains best effort and bounded.
                    pass
                finally:
                    if (
                        self._services.run_registry.get(handle.run_id)
                        is handle
                    ):
                        self._services.run_registry.unregister(handle.run_id)
                components.append(
                    RuntimeComponentResult(
                        component="run_force_abort",
                        status=(
                            "COMPLETED" if force_error is None else "FAILED"
                        ),
                        duration_seconds=max(
                            0.0, time.monotonic() - force_started
                        ),
                        error_code=force_error,
                    )
                )

            admission_report = await self._services.close_worker_admission(
                self._component_timeout_seconds
            )
            components.extend(admission_report.components)
            worker_drain_completed = True
            worker_drain_faulted = False
            if self._services.has_worker_drain_targets():
                worker_started = time.monotonic()
                try:
                    await execute_shutdown_fault(
                        fault_controller,
                        FaultPoint.SHUTDOWN_BEFORE_WORKER_DRAIN,
                        timeout=self._component_timeout_seconds,
                        component="graceful_shutdown",
                        operation_kind="WORKER_DRAIN",
                        shutdown_component="worker_drain",
                    )
                except ShutdownFaultTimeoutError:
                    worker_drain_completed = False
                    worker_drain_faulted = True
                    components.append(
                        RuntimeComponentResult(
                            "worker_drain",
                            "FAILED",
                            max(0.0, time.monotonic() - worker_started),
                            "RUNTIME_WORKER_DRAIN_INJECTED_TIMEOUT",
                        )
                    )
                except InjectedFaultError:
                    worker_drain_completed = False
                    worker_drain_faulted = True
                    components.append(
                        RuntimeComponentResult(
                            "worker_drain",
                            "FAILED",
                            max(0.0, time.monotonic() - worker_started),
                            "RUNTIME_WORKER_DRAIN_INJECTED_FAILURE",
                        )
                    )
                else:
                    worker_report = await self._services.wait_workers(
                        self._component_timeout_seconds
                    )
                    components.extend(worker_report.components)
                    worker_drain_completed = worker_report.completed

            active_workers, detached_workers, unknown_workers = (
                self._worker_counts(
                    assume_idle=worker_drain_completed,
                )
            )

            flush_report = await self._services.flush(
                self._component_timeout_seconds,
                fault_controller=fault_controller,
            )
            components.extend(flush_report.components)
            observability_flush_status = _component_status(
                flush_report.components, "observability_dispatcher"
            )
            trace_flush_status = _component_status(
                flush_report.components, "span_recorder"
            )
            close_models = (
                worker_drain_completed
                and not worker_drain_faulted
                and active_workers == 0
                and detached_workers == 0
                and unknown_workers == 0
            )
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
                    for component in self._services.model_close_components()
                )
            close_report = await self._services.close(
                self._component_timeout_seconds,
                close_models=close_models,
                fault_controller=fault_controller,
            )
            components.extend(close_report.components)

            self._gate.mark_closed()
            remaining_count = len(
                self._services.run_registry.active_handles()
            )
            active_workers, detached_workers, unknown_workers = (
                self._worker_counts(assume_idle=worker_drain_completed)
            )
            self._report = ShutdownReport(
                state=self._gate.state,
                lifecycle_state=self._services.lifecycle_state,
                active_run_count=len(handles),
                cancel_requested_count=cancel_requested_count,
                cancelled_run_count=cancelled_count,
                cancel_failed_count=cancel_failed_count,
                gracefully_drained_count=gracefully_drained_count,
                forced_run_count=forced_count,
                remaining_run_count=remaining_count,
                worker_drain_status=(
                    "IDLE"
                    if worker_drain_completed
                    and active_workers == 0
                    and detached_workers == 0
                    and unknown_workers == 0
                    else "FAILED"
                    if worker_drain_faulted
                    else "NOT_IDLE"
                ),
                active_worker_count=active_workers,
                detached_worker_count=detached_workers,
                unknown_worker_count=unknown_workers,
                observability_flush_status=observability_flush_status,
                trace_flush_status=trace_flush_status,
                duration_seconds=max(0.0, time.monotonic() - started),
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

    def _worker_counts(self, *, assume_idle: bool) -> tuple[int, int, int]:
        active = detached = unknown = 0
        identities: set[int] = set()
        for tracker in (
            *self._services.worker_trackers,
            *self._services.blocking_executors,
        ):
            if id(tracker) in identities:
                continue
            identities.add(id(tracker))
            try:
                snapshot_method = getattr(tracker, "snapshot", None)
                if callable(snapshot_method):
                    snapshot = snapshot_method()
                    active += max(0, int(getattr(snapshot, "active_count")))
                    detached += max(
                        0, int(getattr(snapshot, "detached_count"))
                    )
                    continue
                worker_snapshot = getattr(tracker, "worker_snapshot", None)
                if callable(worker_snapshot):
                    snapshot = worker_snapshot()
                    active += max(0, int(snapshot["active_worker_count"]))
                    detached += max(
                        0, int(snapshot["detached_worker_count"])
                    )
                    continue
                active_value = getattr(tracker, "active_worker_count")
                detached_value = getattr(tracker, "detached_worker_count")
                active += max(
                    0,
                    int(
                        active_value()
                        if callable(active_value)
                        else active_value
                    ),
                )
                detached += max(
                    0,
                    int(
                        detached_value()
                        if callable(detached_value)
                        else detached_value
                    ),
                )
            except Exception:
                if not assume_idle:
                    unknown += 1
        return active, detached, unknown


def _safe_runtime_mode(value: object) -> str:
    return (
        value
        if value in {"COORDINATED", "LEGACY", "LEGACY_COMPAT"}
        else "UNKNOWN"
    )


def _component_status(
    components: tuple[RuntimeComponentResult, ...], component: str
) -> str:
    for result in components:
        if result.component == component:
            return result.status
    return "NOT_APPLICABLE"


__all__ = ["GracefulShutdownCoordinator", "ShutdownReport"]
