"""Tool Approval event / Journal evidence safety tests (Stage5-Phase7-WP1).

验证：
- Requested / Decided payload 不含 raw arguments / path / prompt / approver id。
- Journal allowlist 与 payload 字段一致，可 append 并可 verify 读回。
- Journal-tail reducer 接受新 approval 事件（观测类，不进入 recovery）。
- Requested < Decided(APPROVED) < TOOL_STARTED 排序可用。
"""

from __future__ import annotations

import pytest

from core.runtime import (
    RuntimeEvent,
    RuntimeEventType,
    ToolApprovalDecidedPayload,
    ToolApprovalRequestedPayload,
)
from core.runtime.events import validate_journal_payload
from core.runtime.event_journal_store import InMemoryRunEventJournal
from tests._recovery_fixtures import runtime_event


def _requested(
    approval_id: str = "approval-1",
    invocation_digest: str = "a" * 64,
    arguments_digest: str = "b" * 64,
    idem_digest: str = "c" * 64,
    binding_digest: str = "e" * 64,
) -> ToolApprovalRequestedPayload:
    return ToolApprovalRequestedPayload(
        approval_id=approval_id,
        tool_name="complex_workflow_simulator",
        invocation_identity_digest=invocation_digest,
        arguments_digest=arguments_digest,
        invocation_binding_digest=binding_digest,
        idempotency_key_digest=idem_digest,
        risk_level="HIGH",
        risk_facts="LOCAL_STATE_MUTATION|NON_IDEMPOTENT",
    )


def _decided(
    approval_id: str = "approval-1",
    invocation_digest: str = "a" * 64,
    decision_status: str = "APPROVED",
    actor_digest: str | None = None,
    binding_digest: str = "e" * 64,
) -> ToolApprovalDecidedPayload:
    return ToolApprovalDecidedPayload(
        approval_id=approval_id,
        invocation_identity_digest=invocation_digest,
        invocation_binding_digest=binding_digest,
        decision_status=decision_status,
        actor_id_digest=actor_digest,
    )


def test_requested_payload_rejects_raw_and_bad_digests():
    # 无效 digest -> 拒绝。
    with pytest.raises(ValueError):
        _requested(invocation_digest="not-a-digest")
    with pytest.raises(ValueError):
        _requested(arguments_digest="not-a-digest")
    with pytest.raises(ValueError):
        _requested(idem_digest="short")
    with pytest.raises(ValueError):
        _requested(binding_digest="not-a-digest")
    # risk_facts 必须稳定字符串表示。
    with pytest.raises(TypeError):
        ToolApprovalRequestedPayload(
            approval_id="approval-1",
            tool_name="t",
            invocation_identity_digest="a" * 64,
            arguments_digest="b" * 64,
            invocation_binding_digest="e" * 64,
            risk_facts=("A", "B"),  # type: ignore[arg-type]
        )
    # 不支持 status。
    with pytest.raises(ValueError):
        _decided(decision_status="PENDING")
    # actor digest 校验。
    with pytest.raises(ValueError):
        _decided(actor_digest="not-a-digest")


def test_requested_journal_payload_safety_and_allowlist():
    payload = _requested()
    journal = InMemoryRunEventJournal()
    journal.append(runtime_event(1, RuntimeEventType.TOOL_APPROVAL_REQUESTED, payload, step_id="step", step_sequence=1))
    record = journal.read_after("run", 0, 10)[0]
    safe = record.safe_payload
    assert safe["approval_id"] == "approval-1"
    assert safe["tool_name"] == "complex_workflow_simulator"
    assert safe["invocation_identity_digest"] == "a" * 64
    assert safe["arguments_digest"] == "b" * 64
    assert safe["invocation_binding_digest"] == "e" * 64
    assert safe["idempotency_key_digest"] == "c" * 64
    assert safe["risk_level"] == "HIGH"
    assert safe["risk_facts"] == "LOCAL_STATE_MUTATION|NON_IDEMPOTENT"
    # 不存在 raw 字段。
    assert "arguments" not in safe
    assert "path" not in safe
    assert "prompt" not in repr(record)
    # allowlist 严格一致（可写可读）。
    validate_journal_payload(RuntimeEventType.TOOL_APPROVAL_REQUESTED, dict(safe))


def test_decided_journal_payload_safety_and_allowlist():
    actor_digest = "d" * 64
    payload = _decided(actor_digest=actor_digest)
    journal = InMemoryRunEventJournal()
    journal.append(runtime_event(2, RuntimeEventType.TOOL_APPROVAL_DECIDED, payload, step_id="step", step_sequence=2))
    record = journal.read_after("run", 0, 10)[0]
    safe = record.safe_payload
    assert safe["approval_id"] == "approval-1"
    assert safe["invocation_identity_digest"] == "a" * 64
    assert safe["invocation_binding_digest"] == "e" * 64
    assert safe["decision_status"] == "APPROVED"
    assert safe["actor_id_digest"] == actor_digest
    # 不包含 raw approver identity 与 raw invocation。
    assert "alice" not in repr(record)
    assert "arguments" not in safe
    validate_journal_payload(RuntimeEventType.TOOL_APPROVAL_DECIDED, dict(safe))
    # Decided 支持四种 status。
    for status in (
        "APPROVED",
        "REJECTED",
        "INVALIDATED_CANCELLED",
        "INVALIDATED_TIMEOUT",
    ):
        journal2 = InMemoryRunEventJournal()
        journal2.append(runtime_event(1, RuntimeEventType.TOOL_APPROVAL_DECIDED, _decided(decision_status=status)))
        record2 = journal2.read_after("run", 0, 10)[0]
        assert record2.safe_payload["decision_status"] == status


def test_journal_tail_reducer_accepts_approval_evidence():
    """Approval 事件是观测/审计证据，不进入 recovery projection。"""
    from core.runtime import RunStartedPayload
    from core.runtime.journal_tail_reducer import (
        JournalTailValidator,
        LimitedJournalTailReducer,
    )
    from tests._recovery_fixtures import recovery_snapshot

    journal = InMemoryRunEventJournal()
    journal.append(
        runtime_event(1, RuntimeEventType.RUN_STARTED, RunStartedPayload("RUNNING"))
    )
    journal.append(
        runtime_event(2, RuntimeEventType.TOOL_APPROVAL_REQUESTED, _requested())
    )
    journal.append(
        runtime_event(3, RuntimeEventType.TOOL_APPROVAL_DECIDED, _decided())
    )
    records = journal.read_after("run", 0, 1000)
    snapshot = recovery_snapshot(sequence=0)
    validated = JournalTailValidator.validate(
        run_id="run", snapshot_sequence=0, records=records
    )
    assert validated.last_sequence == 3
    assert validated.terminal_event_seen is False
    reduced = LimitedJournalTailReducer.reduce(snapshot, records)
    # approval evidence 不进入 tool_evidence 列表。
    assert reduced.tool_evidence == ()
