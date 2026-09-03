"""Stage5-Phase7-WP2 approval stream projection / journal compatibility tests.

覆盖 WP2 冻结 Contract §7/§13/§28/§29：

- TOOL_APPROVAL_REQUESTED / TOOL_APPROVAL_DECIDED 进入既有 ``[[ORCH]]`` control
  projection，且只暴露 explicit allowlist 字段；
- 公共 projection 绝不包含 raw invocation_id、arguments_digest、
  idempotency_key_digest、actor identity 等 internal 字段；
- risk_level 缺省时不投影该键；
- decided projection 覆盖 APPROVED / REJECTED / INVALIDATED_CANCELLED /
  INVALIDATED_TIMEOUT 四种 status；
- 未 allowlist 的 RuntimeEvent 仍被忽略（不自动透传）；
- Journal：新写入包含 invocation_binding_digest；旧 WP1 记录（无该字段）在
  读取端按 legacy optional 继续接受（fail closed 只针对未知字段）。
"""

from __future__ import annotations

import json

import pytest

from core.runtime import (
    RuntimeEventType,
    ToolApprovalDecidedPayload,
    ToolApprovalRequestedPayload,
)
from core.runtime.events import (
    MemoryFormationCompletedPayload,
    validate_journal_payload,
)
from core.runtime.stream_adapter import (
    ChatStreamChunkKind,
    ChatStreamCompatibilityAdapter,
)
from tests._recovery_fixtures import runtime_event

BINDING_DIGEST = "b" * 64
ACTOR_DIGEST = "d" * 64


def _requested(
    *,
    risk_level: str | None = "HIGH",
    binding_digest: str = BINDING_DIGEST,
) -> ToolApprovalRequestedPayload:
    return ToolApprovalRequestedPayload(
        approval_id="approval-1",
        tool_name="complex_workflow_simulator",
        invocation_identity_digest="a" * 64,
        arguments_digest="c" * 64,
        invocation_binding_digest=binding_digest,
        idempotency_key_digest="e" * 64,
        risk_level=risk_level,
        risk_facts="LOCAL_STATE_MUTATION|NON_IDEMPOTENT",
    )


def _decided(
    decision_status: str = "APPROVED",
    *,
    actor_id_digest: str | None = ACTOR_DIGEST,
) -> ToolApprovalDecidedPayload:
    return ToolApprovalDecidedPayload(
        approval_id="approval-1",
        invocation_identity_digest="a" * 64,
        invocation_binding_digest=BINDING_DIGEST,
        decision_status=decision_status,
        actor_id_digest=actor_id_digest,
    )


def _adapt_json(adapter: ChatStreamCompatibilityAdapter, event) -> dict:
    chunk = adapter.adapt(event)
    assert chunk is not None
    assert chunk.kind is ChatStreamChunkKind.CONTROL
    assert chunk.text.startswith("[[ORCH]]")
    assert chunk.text.endswith("\n")
    return json.loads(chunk.text[len("[[ORCH]]") :])


def test_requested_projection_exposes_only_public_allowlist():
    adapter = ChatStreamCompatibilityAdapter()
    event = runtime_event(
        7,
        RuntimeEventType.TOOL_APPROVAL_REQUESTED,
        _requested(),
        run_id="run-1",
        step_id="step-1",
        step_sequence=1,
    )
    projection = _adapt_json(adapter, event)
    assert projection["run_id"] == "run-1"
    assert projection["sequence"] == 7
    assert projection["event_type"] == "TOOL_APPROVAL_REQUESTED"
    assert projection["step_id"] == "step-1"
    payload = projection["payload"]
    assert payload["approval_id"] == "approval-1"
    assert payload["tool_name"] == "complex_workflow_simulator"
    assert payload["invocation_binding_digest"] == BINDING_DIGEST
    assert payload["risk_level"] == "HIGH"
    assert payload["risk_facts"] == "LOCAL_STATE_MUTATION|NON_IDEMPOTENT"
    # Public allowlist 是封闭集合：internal digest 字段绝不外泄。
    assert set(payload) == {
        "approval_id",
        "tool_name",
        "invocation_binding_digest",
        "risk_level",
        "risk_facts",
    }


def test_requested_projection_omits_absent_risk_level():
    adapter = ChatStreamCompatibilityAdapter()
    event = runtime_event(
        1,
        RuntimeEventType.TOOL_APPROVAL_REQUESTED,
        _requested(risk_level=None),
    )
    payload = _adapt_json(adapter, event)["payload"]
    assert "risk_level" not in payload


def test_requested_projection_never_contains_internal_fields():
    adapter = ChatStreamCompatibilityAdapter()
    event = runtime_event(
        1,
        RuntimeEventType.TOOL_APPROVAL_REQUESTED,
        _requested(),
        step_id="step",
        step_sequence=1,
    )
    chunk = adapter.adapt(event)
    assert chunk is not None
    text = chunk.text
    assert "invocation_identity_digest" not in text
    assert "arguments_digest" not in text
    assert "idempotency_key_digest" not in text
    assert "invocation_id" not in text.replace("invocation_binding_digest", "")
    assert "operation_id" not in text


@pytest.mark.parametrize(
    "decision_status",
    ["APPROVED", "REJECTED", "INVALIDATED_CANCELLED", "INVALIDATED_TIMEOUT"],
)
def test_decided_projection_covers_all_statuses_without_actor_identity(
    decision_status: str,
):
    adapter = ChatStreamCompatibilityAdapter()
    event = runtime_event(
        9,
        RuntimeEventType.TOOL_APPROVAL_DECIDED,
        _decided(decision_status),
        run_id="run-1",
        step_id="step-1",
        step_sequence=2,
    )
    projection = _adapt_json(adapter, event)
    assert projection["event_type"] == "TOOL_APPROVAL_DECIDED"
    payload = projection["payload"]
    # Decided payload 固定为三个字段；actor_id_digest 对 UI 无用，不扩大 surface。
    assert set(payload) == {
        "approval_id",
        "invocation_binding_digest",
        "decision_status",
    }
    assert payload["decision_status"] == decision_status
    assert payload["invocation_binding_digest"] == BINDING_DIGEST
    assert ACTOR_DIGEST not in projection


def test_non_allowlisted_event_stays_ignored():
    """WP2 只加两个 allowlist 条目；不把所有 RuntimeEvent 自动透传。"""
    adapter = ChatStreamCompatibilityAdapter()
    event = runtime_event(
        3,
        RuntimeEventType.MEMORY_FORMATION_COMPLETED,
        MemoryFormationCompletedPayload(
            exchange_id="exchange-1",
            agent_id="core_router",
            memory_scope="direct",
            formation_method="HYBRID",
            status="SUCCEEDED",
            schema_version=1,
            proposed_count=0,
            accepted_count=0,
            ignored_count=0,
            persisted_count=0,
            reused_count=0,
            failed_count=0,
            formation_total_duration_ms=0,
            model_extraction_duration_ms=0,
            persistence_duration_ms=0,
        ),
    )
    assert adapter.adapt(event) is None


def test_adapter_rejects_non_runtime_event_input():
    """Adapter 对非法输入保持 fail-closed（RUNTIME_STREAM_ENCODING_FAILED）。"""
    from core.runtime.stream_adapter import ChatStreamProtocolError

    adapter = ChatStreamCompatibilityAdapter()
    with pytest.raises(ChatStreamProtocolError):
        adapter.adapt(object())  # type: ignore[arg-type]


def test_journal_accepts_new_rows_with_binding_digest():
    from core.runtime.event_journal_store import InMemoryRunEventJournal

    journal = InMemoryRunEventJournal()
    journal.append(
        runtime_event(
            1, RuntimeEventType.TOOL_APPROVAL_REQUESTED, _requested(), step_id="step", step_sequence=1
        )
    )
    journal.append(
        runtime_event(
            2, RuntimeEventType.TOOL_APPROVAL_DECIDED, _decided(), step_id="step", step_sequence=2
        )
    )
    records = journal.read_after("run", 0, 10)
    assert records[0].safe_payload["invocation_binding_digest"] == BINDING_DIGEST
    assert records[1].safe_payload["invocation_binding_digest"] == BINDING_DIGEST
    validate_journal_payload(
        RuntimeEventType.TOOL_APPROVAL_REQUESTED, dict(records[0].safe_payload)
    )
    validate_journal_payload(
        RuntimeEventType.TOOL_APPROVAL_DECIDED, dict(records[1].safe_payload)
    )


def test_journal_reader_accepts_legacy_rows_without_binding_digest():
    """WP1 时代 Journal 记录没有 invocation_binding_digest，读取端必须继续接受。"""
    legacy_requested = {
        "approval_id": "approval-1",
        "tool_name": "complex_workflow_simulator",
        "invocation_identity_digest": "a" * 64,
        "arguments_digest": "c" * 64,
        "idempotency_key_digest": "e" * 64,
        "risk_level": "HIGH",
        "risk_facts": "NONE",
    }
    legacy_decided = {
        "approval_id": "approval-1",
        "invocation_identity_digest": "a" * 64,
        "decision_status": "APPROVED",
        "actor_id_digest": ACTOR_DIGEST,
    }
    validate_journal_payload(
        RuntimeEventType.TOOL_APPROVAL_REQUESTED, dict(legacy_requested)
    )
    validate_journal_payload(
        RuntimeEventType.TOOL_APPROVAL_DECIDED, dict(legacy_decided)
    )
    # 新字段可选地出现（新写入行）；未知字段仍然 fail closed。
    with_binding = dict(legacy_requested, invocation_binding_digest=BINDING_DIGEST)
    validate_journal_payload(
        RuntimeEventType.TOOL_APPROVAL_REQUESTED, with_binding
    )
    with pytest.raises(ValueError):
        validate_journal_payload(
            RuntimeEventType.TOOL_APPROVAL_REQUESTED,
            dict(with_binding, unexpected_field="x"),
        )
