from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.runtime.metrics import InMemoryMetricsRecorder
from core.runtime.recovery_coordinator import RecoveryCoordinator
from core.runtime.run_control import RunControlConflict
from core.runtime.structured_logging import (
    InMemoryStructuredRuntimeLogger,
    emit_reconciliation_log,
)


@pytest.mark.asyncio
async def test_reconciliation_metrics_are_recorded_from_handler_outcomes() -> None:
    metrics = InMemoryMetricsRecorder()
    logger = InMemoryStructuredRuntimeLogger()
    coordinator = RecoveryCoordinator.__new__(RecoveryCoordinator)
    coordinator.metrics_recorder = metrics
    coordinator.structured_logger = logger

    async def committed(_candidate):
        return SimpleNamespace(
            state="COMMITTED",
            reconcile_attempt_count=1,
            last_safe_error_code=None,
            manual_required=False,
        )

    coordinator._reconciliation_handler = committed
    candidate = SimpleNamespace(
        run_id="run-safe",
        step_id="step-safe",
        invocation_id="inv-safe",
        tool_name="safe_tool",
    )
    await coordinator._supervise_reconciliation(candidate)

    snapshot = metrics.snapshot()
    assert snapshot.counter("runtime_reconciliation_claimed_total") == 1
    assert snapshot.counter("runtime_reconciliation_converged_committed_total") == 1
    assert snapshot.histogram("runtime_reconciliation_latency_seconds")
    record = logger.records[-1]
    assert record.invocation_id == "inv-safe"
    assert record.tool_name == "safe_tool"
    assert record.outcome == "COMMITTED"


@pytest.mark.asyncio
async def test_reconciliation_claim_loss_is_not_counted_as_claim_or_failure() -> None:
    metrics = InMemoryMetricsRecorder()
    logger = InMemoryStructuredRuntimeLogger()
    coordinator = RecoveryCoordinator.__new__(RecoveryCoordinator)
    coordinator.metrics_recorder = metrics
    coordinator.structured_logger = logger

    async def claim_lost(_candidate):
        raise RunControlConflict("lost")

    coordinator._reconciliation_handler = claim_lost
    candidate = SimpleNamespace(
        run_id="run-safe",
        step_id="step-safe",
        invocation_id="inv-safe",
        tool_name="safe_tool",
    )
    await coordinator._supervise_reconciliation(candidate)

    snapshot = metrics.snapshot()
    assert snapshot.counter("runtime_reconciliation_claimed_total") == 0
    assert snapshot.counter("runtime_reconciliation_claim_lost_total") == 1
    assert snapshot.counter("runtime_reconciliation_failed_total") == 0
    assert logger.records[-1].outcome == "CLAIM_LOST"


def test_reconciliation_logging_does_not_accept_raw_payload_fields() -> None:
    logger = InMemoryStructuredRuntimeLogger()
    emit_reconciliation_log(
        logger,
        run_id="run-safe",
        step_id="step-safe",
        invocation_id="inv-safe",
        tool_name="safe_tool",
        provider_identity="provider-safe",
        attempt=2,
        outcome="UNKNOWN",
        safe_error_code="LOOKUP_TIMEOUT",
        manual_actor_id="operator-1",
    )
    encoded = logger.records[-1].to_json_dict()
    assert encoded["provider_identity"] == "provider-safe"
    assert encoded["attempt"] == 2
    assert "args" not in encoded
    assert "payload" not in encoded
    assert "api_key" not in encoded
    assert "jwt" not in encoded
