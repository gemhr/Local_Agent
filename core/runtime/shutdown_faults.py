"""Operation-scoped helpers for deterministic shutdown fault seams."""

from __future__ import annotations

import asyncio
import hashlib

from core.runtime.fault_injection import FaultInjectionController
from core.runtime.fault_injection_contract import (
    FaultAction,
    FaultMatchContext,
    FaultPoint,
)


class ShutdownFaultTimeoutError(TimeoutError):
    """A bounded shutdown seam exhausted its operation timeout."""

    error_code = "RUNTIME_SHUTDOWN_FAULT_TIMEOUT"


def shutdown_run_digest(run_id: str) -> str:
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id must be a non-empty string")
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()


async def execute_shutdown_fault(
    controller: FaultInjectionController | None,
    point: FaultPoint,
    *,
    timeout: float,
    component: str,
    operation_kind: str,
    shutdown_component: str | None = None,
    run_id_digest: str | None = None,
    runtime_mode: str | None = None,
) -> None:
    if controller is None:
        return
    if not isinstance(controller, FaultInjectionController):
        raise TypeError("fault_controller must be FaultInjectionController or None")
    try:
        await asyncio.wait_for(
            controller.execute_if_matched(
                FaultMatchContext(
                    fault_point=point,
                    component=component,
                    operation_kind=operation_kind,
                    shutdown_component=shutdown_component,
                    run_id_digest=run_id_digest,
                    runtime_mode=runtime_mode,
                ),
                allowed_actions={
                    FaultAction.RAISE_TYPED_ERROR,
                    FaultAction.DELAY,
                    FaultAction.BLOCK_UNTIL_RELEASED,
                },
            ),
            timeout=timeout,
        )
    except TimeoutError:
        raise ShutdownFaultTimeoutError from None


__all__ = [
    "ShutdownFaultTimeoutError",
    "execute_shutdown_fault",
    "shutdown_run_digest",
]
