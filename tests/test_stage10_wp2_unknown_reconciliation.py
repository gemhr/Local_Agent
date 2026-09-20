from __future__ import annotations

import uuid

import pytest

from core.runtime.run_control import DurableRunControlService
from core.runtime.tool_contract import ToolInvocation
from core.runtime.tool_idempotency import (
    DurableToolInvocationService,
    ProviderReconciliationEvidence,
    ProviderReconciliationResult,
    ToolInvocationState,
)


def _invocation() -> ToolInvocation:
    return ToolInvocation.create(
        tool_name="reconcile_test_tool",
        invocation_id=uuid.uuid4().hex,
        idempotency_key=uuid.uuid4().hex,
        arguments={"operation_id": "reconcile-op"},
    )


@pytest.mark.asyncio
async def test_started_durable_record_lookup_converges_committed(clean_database):
    control = DurableRunControlService(clean_database)
    lease = await control.claim(uuid.uuid4().hex, "wp2-owner")
    service = DurableToolInvocationService(clean_database)
    invocation = _invocation()
    await service.prepare(lease=lease, step_id="step", invocation=invocation, tool_name=invocation.tool_name)
    await service.start(lease=lease, invocation_id=invocation.invocation_id)

    class Provider:
        def reconcile_durable(self, record):
            assert record.state is ToolInvocationState.UNKNOWN
            return ProviderReconciliationEvidence(
                outcome=ProviderReconciliationResult.COMMITTED,
                result={
                    "invocation_id": record.invocation_id,
                    "attempt_id": "reconcile-attempt",
                    "tool_name": record.tool_name,
                    "status": "SUCCEEDED",
                    "output": {
                        "content_type": "application/json",
                        "content": "{\"reconciled\":true}",
                        "original_size_bytes": 19,
                        "returned_size_bytes": 19,
                        "truncated": False,
                        "digest": "reconciled-output",
                    },
                    "safe_summary": "provider committed",
                    "side_effect_state": "COMMITTED",
                    "idempotency_replayed": False,
                    "retry_disposition": "SAFE_WITH_IDEMPOTENCY_KEY",
                    "resource_key_digest": None,
                    "started_at": "2026-01-01T00:00:00+00:00",
                    "completed_at": "2026-01-01T00:00:00+00:00",
                    "duration_ms": 0,
                    "retry_index": 0,
                    "worker_terminated": True,
                    "execution_detached": False,
                    "resource_release_pending": False,
                },
            )

    result = await service.reconcile_durable_record(
        lease=lease, provider=Provider(), invocation_id=invocation.invocation_id
    )
    assert result.state is ToolInvocationState.COMMITTED


@pytest.mark.asyncio
async def test_still_unknown_backoff_and_max_attempt_requires_manual(clean_database):
    control = DurableRunControlService(clean_database)
    lease = await control.claim(uuid.uuid4().hex, "wp2-owner")
    service = DurableToolInvocationService(clean_database)
    invocation = _invocation()
    await service.prepare(lease=lease, step_id="step", invocation=invocation, tool_name=invocation.tool_name)
    await service.start(lease=lease, invocation_id=invocation.invocation_id)
    for _ in range(2):
        result = await service.record_reconciliation_unknown(
            lease=lease, invocation_id=invocation.invocation_id,
            error_code="LOOKUP_TIMEOUT", max_attempts=2,
            initial_seconds=1, max_seconds=2,
        )
    assert result.state is ToolInvocationState.UNKNOWN
    assert result.manual_required is True
    assert result.next_reconcile_at is None


@pytest.mark.asyncio
async def test_unsupported_provider_is_manual_required_and_typed_resolution_is_fenced(clean_database):
    control = DurableRunControlService(clean_database)
    lease = await control.claim(uuid.uuid4().hex, "wp2-owner")
    service = DurableToolInvocationService(clean_database)
    invocation = _invocation()
    await service.prepare(lease=lease, step_id="step", invocation=invocation, tool_name=invocation.tool_name)
    await service.start(lease=lease, invocation_id=invocation.invocation_id)
    result = await service.reconcile_durable_record(
        lease=lease, provider=object(), invocation_id=invocation.invocation_id
    )
    assert result.state is ToolInvocationState.UNKNOWN
    assert result.manual_required is True
    resolved = await service.resolve_unknown_not_committed(
        lease=lease, invocation_id=invocation.invocation_id
    )
    assert resolved.state is ToolInvocationState.NOT_COMMITTED
    assert resolved.manual_required is False
