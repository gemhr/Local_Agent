#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Health / Readiness 的纯只读诊断投影。

本模块是 Diagnostic Projection Owner，**不是** Runtime Authority。

它只读取已经存在的真实事实：
- ApplicationRuntimeServices.lifecycle_state（lifecycle Authority）
- ApplicationRuntimeServices.admission_gate.state（Admission Authority）
- ApplicationRuntimeServices.startup_dependency_snapshot（Startup Dependency Snapshot）

并投影为不可变的 ApplicationDiagnosticSnapshot。它不写回 lifecycle、
admission、dependency 或任何 Runtime 状态，不缓存，不持有 FastAPI/Client/Run
对象，不触发 recovery/retry。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from core.runtime.admission import RuntimeAdmissionState
from core.runtime.application_services import (
    ApplicationRuntimeServices,
    RuntimeLifecycleState,
    StartupDependencySnapshot,
)


class DiagnosticStatus(str, Enum):
    """Derived diagnostic status；不是 writable lifecycle。

    第一版正式值固定为以下 6 个；不得新增。
    """

    STARTING = "STARTING"
    READY = "READY"
    READY_DEGRADED = "READY_DEGRADED"
    DRAINING = "DRAINING"
    CLOSED = "CLOSED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class ApplicationDiagnosticSnapshot:
    """frozen、typed、low-cardinality、safe 的 diagnostic response。

    HTTP body 只允许这四个字段；不得增加 metadata / 计数 / 时间戳 / error。
    """

    status: DiagnosticStatus
    lifecycle: RuntimeLifecycleState | str
    admission: RuntimeAdmissionState | str
    degraded: bool

    def to_safe_dict(self) -> dict[str, Any]:
        """稳定、纯序列化；不依赖 vars/__dict__/raw Enum repr。

        输出字段顺序固定，避免未来无意增加字段。
        """
        return {
            "status": self.status.value,
            "lifecycle": (
                self.lifecycle.value
                if isinstance(self.lifecycle, Enum)
                else str(self.lifecycle)
            ),
            "admission": (
                self.admission.value
                if isinstance(self.admission, Enum)
                else str(self.admission)
            ),
            "degraded": self.degraded,
        }


def _lifecycle_value(lifecycle: RuntimeLifecycleState | str) -> str:
    return lifecycle.value if isinstance(lifecycle, Enum) else str(lifecycle)


def _admission_value(admission: RuntimeAdmissionState | str) -> str:
    return admission.value if isinstance(admission, Enum) else str(admission)


def _unavailable_snapshot() -> ApplicationDiagnosticSnapshot:
    """fail closed：无法安全确认时统一投影为 UNAVAILABLE。"""
    return ApplicationDiagnosticSnapshot(
        status=DiagnosticStatus.UNAVAILABLE,
        lifecycle="UNAVAILABLE",
        admission="UNAVAILABLE",
        degraded=False,
    )


def resolve_application_diagnostic(
    services: ApplicationRuntimeServices | None,
    *,
    fallback_lifecycle: RuntimeLifecycleState | str | None = None,
) -> ApplicationDiagnosticSnapshot:
    """单一 diagnostic resolver。

    Args:
        services: ApplicationRuntimeServices handle；不存在时为 None。
        fallback_lifecycle: services 尚不存在时，仅允许使用
            app.state.runtime_lifecycle_state 作为有限 fallback。

    Returns:
        不可变的 ApplicationDiagnosticSnapshot。
    """
    if services is not None:
        return _resolve_from_services(services)

    # services 尚不存在：仅允许有限 fallback（pre-services STARTING 或
    # 纯投影测试所需的 shutdown/closed view）。
    if fallback_lifecycle is None:
        return _unavailable_snapshot()

    lifecycle = _lifecycle_value(fallback_lifecycle)
    if lifecycle == RuntimeLifecycleState.STARTING.value:
        return ApplicationDiagnosticSnapshot(
            status=DiagnosticStatus.STARTING,
            lifecycle=RuntimeLifecycleState.STARTING,
            # RuntimeAdmissionState 只有 ACCEPTING/DRAINING/CLOSED；
            # 与 _unavailable_snapshot 一致，使用安全字符串 UNAVAILABLE。
            admission="UNAVAILABLE",
            degraded=False,
        )
    if lifecycle == RuntimeLifecycleState.SHUTTING_DOWN.value:
        return ApplicationDiagnosticSnapshot(
            status=DiagnosticStatus.DRAINING,
            lifecycle=RuntimeLifecycleState.SHUTTING_DOWN,
            admission=RuntimeAdmissionState.DRAINING,
            degraded=False,
        )
    if lifecycle == RuntimeLifecycleState.CLOSED.value:
        return ApplicationDiagnosticSnapshot(
            status=DiagnosticStatus.CLOSED,
            lifecycle=RuntimeLifecycleState.CLOSED,
            admission=RuntimeAdmissionState.CLOSED,
            degraded=False,
        )
    # 其他未知/不可用 fallback 一律 fail closed。
    return _unavailable_snapshot()


def _resolve_from_services(
    services: ApplicationRuntimeServices,
) -> ApplicationDiagnosticSnapshot:
    """从真实 Authority 投影；只读，不写回。"""
    lifecycle = services.lifecycle_state
    admission = services.admission_gate.state
    degraded = services.startup_dependency_snapshot.knowledge_base_degraded

    if lifecycle is RuntimeLifecycleState.READY:
        if admission is RuntimeAdmissionState.ACCEPTING:
            status = (
                DiagnosticStatus.READY_DEGRADED
                if degraded
                else DiagnosticStatus.READY
            )
            return ApplicationDiagnosticSnapshot(
                status=status,
                lifecycle=RuntimeLifecycleState.READY,
                admission=RuntimeAdmissionState.ACCEPTING,
                degraded=degraded,
            )
        if admission is RuntimeAdmissionState.DRAINING:
            return ApplicationDiagnosticSnapshot(
                status=DiagnosticStatus.DRAINING,
                lifecycle=RuntimeLifecycleState.READY,
                admission=RuntimeAdmissionState.DRAINING,
                degraded=degraded,
            )
        # READY + CLOSED 是异常组合；fail closed。
        return _unavailable_snapshot()

    if lifecycle is RuntimeLifecycleState.SHUTTING_DOWN:
        if admission is RuntimeAdmissionState.DRAINING:
            return ApplicationDiagnosticSnapshot(
                status=DiagnosticStatus.DRAINING,
                lifecycle=RuntimeLifecycleState.SHUTTING_DOWN,
                admission=RuntimeAdmissionState.DRAINING,
                degraded=degraded,
            )
        # SHUTTING_DOWN + 非 DRAINING 是异常组合；fail closed。
        return _unavailable_snapshot()

    if lifecycle is RuntimeLifecycleState.CLOSED:
        if admission is RuntimeAdmissionState.CLOSED:
            return ApplicationDiagnosticSnapshot(
                status=DiagnosticStatus.CLOSED,
                lifecycle=RuntimeLifecycleState.CLOSED,
                admission=RuntimeAdmissionState.CLOSED,
                degraded=degraded,
            )
        # CLOSED + 非 CLOSED 是异常组合；fail closed。
        return _unavailable_snapshot()

    if lifecycle is RuntimeLifecycleState.STARTING:
        # services 已存在但 lifecycle 仍 STARTING：属于异常/未完成装配；
        # 不承诺可接受新 Run，fail closed 为 UNAVAILABLE。
        return _unavailable_snapshot()

    # 未知 lifecycle 值：fail closed。
    return _unavailable_snapshot()


def health_http_status(snapshot: ApplicationDiagnosticSnapshot) -> int:
    """Health 的 HTTP status 映射。

    Health 只证明 application 尚未进入 terminal CLOSED / fatal unavailable。
    DRAINING 期间仍为 200（足以完成有界关闭）。
    """
    if snapshot.status in (
        DiagnosticStatus.STARTING,
        DiagnosticStatus.READY,
        DiagnosticStatus.READY_DEGRADED,
        DiagnosticStatus.DRAINING,
    ):
        return 200
    return 503


def readiness_http_status(snapshot: ApplicationDiagnosticSnapshot) -> int:
    """Readiness 的 HTTP status 映射。

    Readiness 证明可以安全尝试接受一个新的 Run：
    services + lifecycle READY + admission ACCEPTING（KB degraded 仍 ready）。
    """
    if snapshot.status in (
        DiagnosticStatus.READY,
        DiagnosticStatus.READY_DEGRADED,
    ):
        return 200
    return 503


__all__ = [
    "ApplicationDiagnosticSnapshot",
    "DiagnosticStatus",
    "health_http_status",
    "readiness_http_status",
    "resolve_application_diagnostic",
]
