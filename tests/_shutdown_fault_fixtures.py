from __future__ import annotations

from datetime import UTC, datetime
import hashlib

from core.runtime import (
    FaultAction,
    FaultBlocker,
    FaultInjectionController,
    FaultPlan,
    FaultPoint,
    FaultRule,
    FaultScope,
    FaultTrigger,
    InjectedFaultCode,
)


NOW = datetime(2026, 1, 24, tzinfo=UTC)


def run_digest(run_id: str) -> str:
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()


def shutdown_rule(
    point: FaultPoint,
    *,
    rule_id: str | None = None,
    shutdown_component: str | None = None,
    run_id: str | None = None,
    action: FaultAction = FaultAction.RAISE_TYPED_ERROR,
    max_hits: int = 1,
    delay_seconds: float = 1.0,
    enabled: bool = True,
) -> FaultRule:
    return FaultRule(
        rule_id=rule_id or point.value.lower(),
        fault_point=point,
        action=action,
        trigger=FaultTrigger.ALWAYS,
        scope=FaultScope.COMPONENT_SCOPE,
        max_hits=max_hits,
        component=(
            "observability_dispatcher"
            if point is FaultPoint.OBSERVABILITY_BEFORE_FLUSH
            else "trace_recorder"
            if point is FaultPoint.TRACE_BEFORE_FLUSH
            else "graceful_shutdown"
        ),
        shutdown_component=shutdown_component,
        run_id_digest=run_digest(run_id) if run_id is not None else None,
        safe_fault_code=(
            InjectedFaultCode.INJECTED_PERMANENT_FAILURE
            if action is FaultAction.RAISE_TYPED_ERROR
            else None
        ),
        delay_seconds=delay_seconds if action is FaultAction.DELAY else None,
        enabled=enabled,
    )


def shutdown_controller(
    *rules: FaultRule,
    blockers: dict[str, FaultBlocker] | None = None,
    enabled: bool = True,
) -> FaultInjectionController:
    return FaultInjectionController(
        FaultPlan("shutdown-plan", tuple(rules), created_at=NOW),
        enabled=enabled,
        blockers=blockers,
    )


class RecordingResource:
    def __init__(
        self,
        name: str,
        calls: list[str],
        *,
        fail_close: bool = False,
    ) -> None:
        self.name = name
        self.calls = calls
        self.fail_close = fail_close
        self.close_calls = 0

    def close(self) -> bool:
        self.close_calls += 1
        self.calls.append(f"{self.name}.close")
        if self.fail_close:
            raise RuntimeError("provider-secret-error")
        return True


class RecordingWorker:
    def __init__(
        self,
        calls: list[str],
        *,
        active: int = 0,
        detached: int = 0,
        idle_result: bool = True,
    ) -> None:
        self.calls = calls
        self.active = active
        self.detached = detached
        self.idle_result = idle_result
        self.wait_calls = 0
        self.close_calls = 0

    def close_admission(self) -> None:
        self.calls.append("worker.admission")

    def wait_until_idle(self, timeout: float) -> bool:
        self.wait_calls += 1
        self.calls.append("worker.drain")
        if self.idle_result:
            self.active = 0
            self.detached = 0
        return self.idle_result

    @property
    def active_worker_count(self) -> int:
        return self.active

    @property
    def detached_worker_count(self) -> int:
        return self.detached

    def shutdown(self, *, wait: bool = True, timeout: float = 1.0) -> bool:
        self.close_calls += 1
        self.calls.append("worker.close")
        return self.active == 0


__all__ = [
    "RecordingResource",
    "RecordingWorker",
    "run_digest",
    "shutdown_controller",
    "shutdown_rule",
]
