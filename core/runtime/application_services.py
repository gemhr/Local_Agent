#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Application-scoped runtime dependencies and bounded lifecycle contracts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
import inspect
import re
import threading
from typing import Callable

from core.runtime.activity import RuntimeActivityTracker
from core.runtime.context import RunContext
from core.runtime.event_channel import RuntimeEventChannel
from core.runtime.state import AgentState


SAFE_RUNTIME_ASSEMBLY_VERSION = "1"
_COMPONENT_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_PER_RUN_TYPES = (AgentState, RunContext, RuntimeEventChannel, RuntimeActivityTracker)


class RuntimeLifecycleState(str, Enum):
    STARTING = "STARTING"
    READY = "READY"
    SHUTTING_DOWN = "SHUTTING_DOWN"
    CLOSED = "CLOSED"


class RuntimeInitializationError(RuntimeError):
    """Path-free startup failure projected to a fixed safe error code."""

    error_code = "RUNTIME_INITIALIZATION_FAILED"

    def __init__(self, component: str) -> None:
        _validate_component_name(component)
        self.component = component
        super().__init__(
            "runtime component initialization failed "
            f"(error_code={self.error_code}, component={component})"
        )


@dataclass(frozen=True, slots=True)
class RuntimeLifecycleIssue:
    component: str
    error_code: str


@dataclass(frozen=True, slots=True)
class RuntimeLifecycleReport:
    action: str
    completed: bool
    issues: tuple[RuntimeLifecycleIssue, ...] = ()

    @property
    def error_codes(self) -> tuple[str, ...]:
        return tuple(issue.error_code for issue in self.issues)


@dataclass(slots=True)
class _LifecycleControl:
    state: RuntimeLifecycleState = RuntimeLifecycleState.READY
    close_report: RuntimeLifecycleReport | None = None
    close_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    state_lock: threading.Lock = field(default_factory=threading.Lock)


def _validate_timeout(timeout: object) -> float:
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or float(timeout) < 0
    ):
        raise ValueError("timeout must be a non-negative number")
    return float(timeout)


def _validate_component_name(component: str) -> None:
    if not isinstance(component, str) or _COMPONENT_NAME.fullmatch(component) is None:
        raise ValueError("component must be a safe lowercase identifier")


def _invoke_arguments(method, timeout: float, operation: str) -> tuple[tuple, dict]:
    """Select a bounded-call spelling without exposing the target in errors."""
    try:
        parameters = inspect.signature(method).parameters
    except (TypeError, ValueError):
        parameters = {}
    if operation == "shutdown":
        return (), {"wait": True, "timeout": timeout}
    if "timeout" in parameters:
        return (), {"timeout": timeout}
    if "timeout_seconds" in parameters:
        return (), {"timeout_seconds": timeout}
    return (), {}


async def _invoke_bounded(
    target: object,
    operation: str,
    timeout: float,
) -> bool:
    method = getattr(target, operation, None)
    if not callable(method):
        return True
    args, kwargs = _invoke_arguments(method, timeout, operation)
    if inspect.iscoroutinefunction(method):
        result = await asyncio.wait_for(method(*args, **kwargs), timeout=timeout)
    else:
        result = await asyncio.wait_for(
            asyncio.to_thread(method, *args, **kwargs),
            timeout=timeout,
        )
        if inspect.isawaitable(result):
            result = await asyncio.wait_for(result, timeout=timeout)
    return result is not False


@dataclass(frozen=True, slots=True)
class ApplicationRuntimeServices:
    """Immutable dependency references with one controlled close owner.

    The container deliberately has no field capable of retaining the current
    request's context, state, event channel, or activity tracker.
    """

    event_journal: object
    observability_dispatcher: object
    structured_logger: object
    runtime_metrics_recorder: object
    span_recorder: object
    snapshot_store: object | None
    recovery_validator: object | None
    model_invocation_router: object
    tool_execution_service: object
    retrieval_execution_service: object | None
    blocking_executors: tuple[object, ...]
    worker_trackers: tuple[object, ...]
    run_registry: object
    snapshot_enabled: bool = False
    recovery_enabled: bool = False
    activity_tracker_factory: Callable[[str], RuntimeActivityTracker] = (
        RuntimeActivityTracker
    )
    extra_closeables: tuple[tuple[str, object], ...] = ()
    _lifecycle: _LifecycleControl = field(
        default_factory=_LifecycleControl,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if self.snapshot_enabled != (self.snapshot_store is not None):
            raise ValueError("snapshot_enabled must match snapshot_store availability")
        if self.recovery_enabled != (self.recovery_validator is not None):
            raise ValueError(
                "recovery_enabled must match recovery_validator availability"
            )
        if self.recovery_enabled and not self.snapshot_enabled:
            raise ValueError("recovery requires snapshot capability")
        if not callable(self.activity_tracker_factory):
            raise TypeError("activity_tracker_factory must be callable")
        for value in (
            self.event_journal,
            self.observability_dispatcher,
            self.structured_logger,
            self.runtime_metrics_recorder,
            self.span_recorder,
            self.snapshot_store,
            self.recovery_validator,
            self.model_invocation_router,
            self.tool_execution_service,
            self.retrieval_execution_service,
            self.run_registry,
            *self.blocking_executors,
            *self.worker_trackers,
        ):
            if isinstance(value, _PER_RUN_TYPES):
                raise ValueError("application services cannot retain per-run objects")
        for component, resource in self.extra_closeables:
            _validate_component_name(component)
            if isinstance(resource, _PER_RUN_TYPES):
                raise ValueError("application services cannot retain per-run objects")

    @property
    def lifecycle_state(self) -> RuntimeLifecycleState:
        with self._lifecycle.state_lock:
            return self._lifecycle.state

    def new_activity_tracker(self, run_id: str):
        tracker = self.activity_tracker_factory(run_id)
        if getattr(tracker, "run_id", None) != run_id:
            raise TypeError("activity_tracker_factory returned an invalid tracker")
        return tracker

    def _targets(self) -> tuple[tuple[str, object, str], ...]:
        candidates: list[tuple[str, object | None, str]] = [
            ("observability_dispatcher", self.observability_dispatcher, "close"),
            ("span_recorder", self.span_recorder, "close"),
            ("snapshot_store", self.snapshot_store, "close"),
            ("event_journal", self.event_journal, "close"),
        ]
        candidates.extend(
            (f"blocking_executor_{index}", value, "shutdown")
            for index, value in enumerate(self.blocking_executors)
        )
        candidates.extend(
            (f"worker_tracker_{index}", value, "close")
            for index, value in enumerate(self.worker_trackers)
        )
        candidates.extend(
            (component, resource, "close")
            for component, resource in self.extra_closeables
        )
        unique: list[tuple[str, object, str]] = []
        identities: set[int] = set()
        for component, resource, operation in candidates:
            if resource is None or id(resource) in identities:
                continue
            identities.add(id(resource))
            unique.append((component, resource, operation))
        return tuple(unique)

    async def flush(self, timeout: float) -> RuntimeLifecycleReport:
        active_timeout = _validate_timeout(timeout)
        issues: list[RuntimeLifecycleIssue] = []
        for component, target, _close_operation in self._targets():
            if not callable(getattr(target, "flush", None)):
                continue
            try:
                completed = await _invoke_bounded(target, "flush", active_timeout)
            except TimeoutError:
                issues.append(
                    RuntimeLifecycleIssue(component, "RUNTIME_COMPONENT_FLUSH_TIMEOUT")
                )
            except Exception:
                issues.append(
                    RuntimeLifecycleIssue(component, "RUNTIME_COMPONENT_FLUSH_FAILED")
                )
            else:
                if not completed:
                    issues.append(
                        RuntimeLifecycleIssue(
                            component, "RUNTIME_COMPONENT_FLUSH_TIMEOUT"
                        )
                    )
        return RuntimeLifecycleReport("flush", not issues, tuple(issues))

    async def close(self, timeout: float) -> RuntimeLifecycleReport:
        """Close every owned resource at most once and continue after failures."""
        active_timeout = _validate_timeout(timeout)
        async with self._lifecycle.close_lock:
            if self._lifecycle.close_report is not None:
                return self._lifecycle.close_report
            with self._lifecycle.state_lock:
                self._lifecycle.state = RuntimeLifecycleState.SHUTTING_DOWN
            issues: list[RuntimeLifecycleIssue] = []
            for component, target, operation in self._targets():
                try:
                    completed = await _invoke_bounded(
                        target, operation, active_timeout
                    )
                except TimeoutError:
                    issues.append(
                        RuntimeLifecycleIssue(
                            component, "RUNTIME_COMPONENT_CLOSE_TIMEOUT"
                        )
                    )
                except Exception:
                    issues.append(
                        RuntimeLifecycleIssue(
                            component, "RUNTIME_COMPONENT_CLOSE_FAILED"
                        )
                    )
                else:
                    if not completed:
                        issues.append(
                            RuntimeLifecycleIssue(
                                component, "RUNTIME_COMPONENT_CLOSE_TIMEOUT"
                            )
                        )
            report = RuntimeLifecycleReport("close", not issues, tuple(issues))
            with self._lifecycle.state_lock:
                self._lifecycle.state = RuntimeLifecycleState.CLOSED
                self._lifecycle.close_report = report
            return report

    def __repr__(self) -> str:
        component_count = 12 + len(self.blocking_executors) + len(
            self.worker_trackers
        )
        return (
            "ApplicationRuntimeServices("
            "component='application_runtime_services', "
            f"lifecycle_state={self.lifecycle_state.value!r}, "
            f"snapshot_enabled={self.snapshot_enabled!r}, "
            f"recovery_enabled={self.recovery_enabled!r}, "
            f"safe_version={SAFE_RUNTIME_ASSEMBLY_VERSION!r}, "
            f"component_count={component_count})"
        )


class RuntimeInitializationStack:
    """Reverse-order cleanup for resources created before assembly succeeds."""

    def __init__(self) -> None:
        self._resources: list[tuple[str, object, str]] = []
        self._released = False
        self._closed = False

    def track(
        self,
        component: str,
        resource: object,
        *,
        close_operation: str = "close",
    ) -> object:
        if self._released or self._closed:
            raise RuntimeError("initialization stack is no longer accepting resources")
        _validate_component_name(component)
        if close_operation not in {"close", "shutdown"}:
            raise ValueError("unsupported close operation")
        self._resources.append((component, resource, close_operation))
        return resource

    async def create(
        self,
        component: str,
        factory,
        *,
        close_operation: str = "close",
        timeout: float = 5.0,
    ) -> object:
        """Create and track one resource; clean prior resources if it fails."""
        if not callable(factory):
            raise TypeError("factory must be callable")
        try:
            resource = factory()
            if inspect.isawaitable(resource):
                resource = await resource
        except asyncio.CancelledError:
            await self.close(timeout)
            raise
        except Exception:
            await self.close(timeout)
            raise RuntimeInitializationError(component) from None
        return self.track(
            component,
            resource,
            close_operation=close_operation,
        )

    async def run(
        self,
        operation,
        *,
        component: str = "runtime_assembly",
        timeout: float = 5.0,
    ):
        """Run a non-resource initialization step with the same rollback rule."""
        if not callable(operation):
            raise TypeError("operation must be callable")
        _validate_component_name(component)
        try:
            result = operation()
            if inspect.isawaitable(result):
                result = await result
            return result
        except asyncio.CancelledError:
            await self.close(timeout)
            raise
        except Exception:
            await self.close(timeout)
            raise RuntimeInitializationError(component) from None

    async def fail(self, error: BaseException, *, timeout: float = 5.0) -> None:
        """Rollback first, then raise a caller-supplied safe startup error."""
        if not isinstance(error, BaseException):
            raise TypeError("error must be an exception")
        await self.close(timeout)
        raise error

    def release(self) -> None:
        self._released = True
        self._resources.clear()

    async def close(self, timeout: float) -> RuntimeLifecycleReport:
        active_timeout = _validate_timeout(timeout)
        if self._closed or self._released:
            return RuntimeLifecycleReport("initialization_cleanup", True)
        self._closed = True
        issues: list[RuntimeLifecycleIssue] = []
        identities: set[int] = set()
        for component, target, operation in reversed(self._resources):
            if id(target) in identities:
                continue
            identities.add(id(target))
            try:
                completed = await _invoke_bounded(target, operation, active_timeout)
            except TimeoutError:
                issues.append(
                    RuntimeLifecycleIssue(
                        component, "RUNTIME_INITIALIZATION_CLEANUP_TIMEOUT"
                    )
                )
            except Exception:
                issues.append(
                    RuntimeLifecycleIssue(
                        component, "RUNTIME_INITIALIZATION_CLEANUP_FAILED"
                    )
                )
            else:
                if not completed:
                    issues.append(
                        RuntimeLifecycleIssue(
                            component, "RUNTIME_INITIALIZATION_CLEANUP_TIMEOUT"
                        )
                    )
        self._resources.clear()
        return RuntimeLifecycleReport(
            "initialization_cleanup", not issues, tuple(issues)
        )


__all__ = [
    "ApplicationRuntimeServices",
    "RuntimeInitializationError",
    "RuntimeInitializationStack",
    "RuntimeLifecycleIssue",
    "RuntimeLifecycleReport",
    "RuntimeLifecycleState",
    "SAFE_RUNTIME_ASSEMBLY_VERSION",
]
