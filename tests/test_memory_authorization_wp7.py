"""WP7-B Agent-private Memory requester/owner authorization contracts。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from core.advanced_memory import (
    AdvancedMemoryStore,
    MemoryOrigin,
    MemoryStatus,
    MemoryType,
    SemanticMemoryRecord,
)
from core.memory_manager import MemoryManager
from core.runtime import (
    ContextBuildRequest,
    ContextBuilder,
    MemoryAccessAuthorizer,
    MemoryAccessPrincipal,
    MemoryAuthorizationDecision,
    MemoryAuthorizationErrorCode,
    MemoryAuthorizationReason,
    MemoryRetrievalService,
    FormationExtractionModel,
    SemanticMemoryFormation,
)
from core.runtime.final_memory_writer import CommittedExchangeReceipt
from core.runtime.episodic_memory_formation import EpisodeEvidenceInput
from core.runtime.episodic_memory_formation import EpisodicMemoryFormation
from core.runtime.state import AgentState, RunStatus, StopReason


def _store(tmp_path) -> AdvancedMemoryStore:
    path = str(tmp_path / "wp7-memory.db")
    MemoryManager(db_path=path)
    return AdvancedMemoryStore(path)


def _record(memory_id: str = "mem-a", *, agent_id: str = "agent_A") -> SemanticMemoryRecord:
    now = datetime.now(UTC)
    return SemanticMemoryRecord(
        memory_id=memory_id,
        agent_id=agent_id,
        memory_scope="direct",
        canonical_text="Agent A 使用 SQLite",
        payload={"value": "SQLite"},
        origin=MemoryOrigin(
            "TEST", "run-a", "exchange-a", agent_id, "direct", "WP7_TEST"
        ),
        memory_type=MemoryType.SEMANTIC,
        status=MemoryStatus.ACTIVE,
        logical_key="project.database",
        created_at=now,
        updated_at=now,
    )


def test_private_policy_is_typed_owner_requester_and_fail_closed() -> None:
    policy = MemoryAccessAuthorizer()
    owner = MemoryAccessPrincipal("agent_A")
    allowed = policy.authorize_private_read(owner, "agent_A", "direct")
    assert allowed.decision is MemoryAuthorizationDecision.ALLOW
    assert allowed.reason_code is MemoryAuthorizationReason.OWNER_MATCH

    foreign = policy.authorize_private_read(
        MemoryAccessPrincipal("agent_B"), "agent_A", "direct"
    )
    assert foreign.decision is MemoryAuthorizationDecision.DENY
    assert foreign.error_code == MemoryAuthorizationErrorCode.PRIVATE_MEMORY_ACCESS_DENIED

    missing = policy.authorize_private_read(None, "agent_A", "direct")
    assert missing.decision is MemoryAuthorizationDecision.DENY
    assert missing.reason_code is MemoryAuthorizationReason.UNKNOWN_REQUESTER
    assert missing.error_code == MemoryAuthorizationErrorCode.MEMORY_REQUESTER_MISSING

    mismatch = policy.authorize_private_read(
        owner, "agent_A", "direct", requested_memory_scope="other"
    )
    assert mismatch.decision is MemoryAuthorizationDecision.DENY
    assert mismatch.reason_code is MemoryAuthorizationReason.SCOPE_MISMATCH
    assert mismatch.error_code == MemoryAuthorizationErrorCode.PRIVATE_MEMORY_SCOPE_MISMATCH

    project = policy.authorize_private_read(
        owner, "agent_A", "direct", visibility="PROJECT"
    )
    assert project.decision is MemoryAuthorizationDecision.DENY
    assert project.error_code == MemoryAuthorizationErrorCode.UNSUPPORTED_MEMORY_VISIBILITY

    unsupported = policy.authorize("DELETE", owner, "agent_A", "direct")
    assert unsupported.decision is MemoryAuthorizationDecision.DENY
    assert unsupported.reason_code is MemoryAuthorizationReason.UNSUPPORTED_OPERATION


class _CountingStore(AdvancedMemoryStore):
    def __init__(self, db_path: str) -> None:
        super().__init__(db_path)
        self.semantic_reads = 0
        self.episodic_reads = 0

    def list_active_semantic_for_scope(self, *args, **kwargs):
        self.semantic_reads += 1
        return super().list_active_semantic_for_scope(*args, **kwargs)

    def list_active_episodic_for_scope(self, *args, **kwargs):
        self.episodic_reads += 1
        return super().list_active_episodic_for_scope(*args, **kwargs)


def test_foreign_private_read_is_denied_before_retrieval_and_injection(tmp_path) -> None:
    store = _store(tmp_path)
    store.create(_record())
    counted = _CountingStore(store.db_path)
    bundle = MemoryRetrievalService(counted).retrieve(
        requester=MemoryAccessPrincipal("agent_B"),
        target_owner_agent_id="agent_A",
        memory_scope="direct",
        query="SQLite",
    )
    assert bundle.authorization is not None
    assert bundle.authorization.decision is MemoryAuthorizationDecision.DENY
    assert bundle.authorization.reason_code is MemoryAuthorizationReason.FOREIGN_PRIVATE_OWNER
    assert bundle.record_count == bundle.selected_count == 0
    assert counted.semantic_reads == counted.episodic_reads == 0

    missing = MemoryRetrievalService(counted).retrieve(
        agent_id="agent_A",
        memory_scope="direct",
        query="SQLite",
    )
    assert missing.authorization is not None
    assert missing.authorization.reason_code is MemoryAuthorizationReason.UNKNOWN_REQUESTER
    assert missing.record_count == 0
    assert counted.semantic_reads == counted.episodic_reads == 0

    result = ContextBuilder().build(
        ContextBuildRequest("run-b", "agent_B", bundle.all_records, 4096, 512)
    )
    assert result.included_items == ()
    assert result.stats.has_memory is False


class _ExtractionModel(FormationExtractionModel):
    def extract(self, user_query: str, final_answer: str) -> str:
        return json.dumps(
            {
                "schema_version": 1,
                "candidates": [
                    {
                        "disposition": "REMEMBER",
                        "category": "ENGINEERING_CONSTRAINT",
                        "canonical_text": "以后统一使用 SQLite",
                        "value": "SQLite",
                        "source_excerpt": "以后统一使用 SQLite",
                        "predicate_resolution": "OPEN",
                        "proposed_predicate_id": None,
                    }
                ],
            },
            ensure_ascii=False,
        )


def _receipt(agent_id: str = "agent_A") -> CommittedExchangeReceipt:
    return CommittedExchangeReceipt(
        run_id="run-a", exchange_id="exchange-a", entry_agent_id=agent_id, memory_scope="direct"
    )


@pytest.mark.asyncio
async def test_foreign_semantic_update_is_denied_before_model_and_store_mutation(tmp_path) -> None:
    store = _store(tmp_path)
    original = _record()
    store.create(original)
    model = _ExtractionModel()
    formation = SemanticMemoryFormation(
        entry_agent_id="agent_A",
        requester=MemoryAccessPrincipal("agent_B"),
        user_request="以后统一使用 SQLite",
        memory_store=store,
        extraction_model=model,
        run_id="run-a",
    )
    result = await formation.run_formation(
        receipt=_receipt(), final_step_id="final", store=object()
    )
    assert result.safe_error_code == "PRIVATE_MEMORY_ACCESS_DENIED"
    assert result.accepted_count == result.persisted_count == 0
    assert store.get_by_memory_id(original.memory_id).status is MemoryStatus.ACTIVE


@pytest.mark.asyncio
async def test_foreign_semantic_forget_is_denied_with_zero_affected(tmp_path) -> None:
    store = _store(tmp_path)
    original = _record()
    store.create(original)
    formation = SemanticMemoryFormation(
        entry_agent_id="agent_A",
        requester=MemoryAccessPrincipal("agent_B"),
        user_request="请忘记 project.database",
        memory_store=store,
        extraction_model=_ExtractionModel(),
        run_id="run-a",
    )
    result = await formation.run_formation(
        receipt=_receipt(), final_step_id="final", store=object()
    )
    assert result.safe_error_code == "PRIVATE_MEMORY_ACCESS_DENIED"
    assert result.lifecycle_affected_count == 0
    assert store.get_by_memory_id(original.memory_id).status is MemoryStatus.ACTIVE


def _episode_source() -> EpisodeEvidenceInput:
    state = AgentState.for_run_context("run-episode")
    state.mark_running()
    state.add_step("step-1", "执行安全检查")
    state.start_step("step-1")
    state.succeed_step("step-1")
    return EpisodeEvidenceInput(
        run_id="run-episode",
        agent_id="agent_A",
        memory_scope="direct",
        user_request="修复数据库迁移失败",
        plan_goal="安全完成迁移",
        agent_state=state,
        terminal_status=RunStatus.SUCCEEDED,
        stop_reason=StopReason.COMPLETED,
        delivery_status="DELIVERED",
    )


@pytest.mark.asyncio
async def test_foreign_episodic_create_is_denied_without_persistence(tmp_path) -> None:
    store = _store(tmp_path)
    result = await EpisodicMemoryFormation(
        store, requester=MemoryAccessPrincipal("agent_B")
    ).run_formation(_episode_source())
    assert result.outcome.value == "FAILED"
    assert result.safe_reason == "FOREIGN_PRIVATE_OWNER"
    assert store.list_by_agent("agent_A", memory_scope="direct", active_only=False) == []
