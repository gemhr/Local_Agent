"""WP2-B canonical Formation hook deterministic tests。

证明 canonical ordering（OutputGate DELIVERED → conversation commit receipt →
independent Formation → existing Step completion）与 delivery / terminal
isolation：Formation 的任何 outcome 都不改变 delivered output、final Step
status、Run terminal 或触发 re-delivery；以及 production wiring 经
CoordinatedRuntimeFactory 真正接入 Formation component。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import List, Optional

import pytest

from core.advanced_memory import AdvancedMemoryStore
from core.memory_manager import MemoryManager
from core.runtime import (
    AgentState,
    AgentStateMachine,
    CancellationSource,
    CommittedExchangeReceipt,
    DeliveryStatus,
    FaultPoint,
    InMemoryRunEventJournal,
    ResultContentType,
    RunEventEmitter,
    RunEventType,
    RunStatus,
    RuntimeEventChannel,
    RuntimeEventType,
    StepClaim,
    StepResult,
    StepResultCommitter,
    StepResultStore,
    StepStateEvent,
    OutputGate,
    SemanticFormationResult,
    SemanticFormationStatus,
    create_run_context,
)
from core.runtime.planning import (
    ExecutionKind,
    OutputPolicy,
    Plan,
    PlanSource,
    PlanStep,
)
from core.runtime.semantic_memory_formation import (
    FormationCandidateOutcome,
    FormationCandidateOutcomeCode,
)
from tests._event_fault_fixtures import event_controller
from tests._wp3_fixtures import make_wp3_services
from tests.test_step_completion import build_shape2_plan, make_state, claim_for
from tests.test_wp3_history_boundary import FakeModel, make_real_router

ORDER: List[str] = []


def formation_result(
    status: SemanticFormationStatus = SemanticFormationStatus.SUCCEEDED,
    error_code: Optional[str] = None,
) -> SemanticFormationResult:
    return SemanticFormationResult(
        run_id="run-1",
        exchange_id="run-1",
        agent_id="core_router",
        memory_scope="direct",
        formation_method="HYBRID",
        status=status,
        schema_version=1,
        proposed_count=1,
        accepted_count=1,
        ignored_count=0,
        persisted_count=1,
        reused_count=0,
        failed_count=0,
        candidate_outcomes=(
            FormationCandidateOutcome(
                0, FormationCandidateOutcomeCode.PERSISTED, "OK", "mem-x"
            ),
        ),
        formation_total_duration_ms=5,
        model_extraction_duration_ms=3,
        persistence_duration_ms=2,
        safe_error_code=error_code,
    )


class RecordingGate(OutputGate):
    async def attempt_publish(self, *, claim, result):
        ORDER.append("delivery")
        return await super().attempt_publish(claim=claim, result=result)


class RecordingWriter:
    def __init__(
        self,
        receipt: Optional[CommittedExchangeReceipt] = None,
        error: Optional[Exception] = None,
    ) -> None:
        self.receipt = receipt
        self.error = error
        self.calls = 0

    def write_delivered(self, *, final_step_id: str, store: StepResultStore):
        ORDER.append("conversation_commit")
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.receipt


class RecordingFormation:
    def __init__(
        self,
        result: Optional[SemanticFormationResult] = None,
        error: Optional[Exception] = None,
    ) -> None:
        self.result = result or formation_result()
        self.error = error
        self.calls = 0
        self.receipts: List[CommittedExchangeReceipt] = []

    async def run_formation(
        self, *, receipt: CommittedExchangeReceipt, final_step_id: str, store
    ) -> SemanticFormationResult:
        ORDER.append("formation")
        self.calls += 1
        self.receipts.append(receipt)
        if self.error is not None:
            raise self.error
        return self.result


async def make_hook_emitter(*, controller=None):
    context, source = create_run_context(entry_agent_id="test")
    journal = InMemoryRunEventJournal()
    channel = RuntimeEventChannel(
        8,
        run_id=context.run_id,
        cancellation_token=source.token,
        journal=journal,
        fault_controller=controller,
    )
    emitter = RunEventEmitter(
        run_id=context.run_id, trace_id=context.trace_id, channel=channel
    )
    return emitter, channel


def build_committer(
    plan,
    emitter,
    *,
    writer: Optional[RecordingWriter] = None,
    formation: Optional[RecordingFormation] = None,
    controller=None,
    running: tuple = ("synthesis",),
):
    store = StepResultStore(plan, run_id="run-1")
    state, machine = make_state(plan, running=running)
    gate = RecordingGate(
        plan=plan,
        store=store,
        event_emitter=emitter,
        state_getter=lambda: state,
        run_active=lambda: state.status
        in {RunStatus.CREATED, RunStatus.RUNNING},
        fault_controller=controller,
    )
    committer = StepResultCommitter(
        store=store,
        state_machine=machine,
        event_emitter=emitter,
        plan=plan,
        output_gate=gate,
        final_memory_writer=writer,
        semantic_memory_formation=formation,
    )
    return store, state, committer, gate


def committed_receipt() -> CommittedExchangeReceipt:
    return CommittedExchangeReceipt(
        run_id="run-1",
        exchange_id="run-1",
        entry_agent_id="core_router",
        memory_scope="direct",
    )


FINAL_RESULT = StepResult(
    "synthesis", "synthesis_agent", ResultContentType.TEXT, "FINAL"
)


# ---------------------------------------------------------------------------
# Hook 触发条件
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_final_step_never_triggers_formation() -> None:
    ORDER.clear()
    plan = build_shape2_plan()
    emitter, channel = await make_hook_emitter()
    try:
        writer = RecordingWriter(receipt=committed_receipt())
        formation = RecordingFormation()
        store, state, committer, gate = build_committer(
            plan,
            emitter,
            writer=writer,
            formation=formation,
            running=("task-code",),
        )
        # INTERNAL step：不经过 gate / writer / formation。
        result = StepResult(
            "task-code", "code_expert", ResultContentType.TEXT, "ok"
        )
        completion = await committer.commit(
            claim_for(plan, "task-code"), result, state
        )
        assert completion.succeeded is True
        assert completion.formation_status is None
        assert writer.calls == 0
        assert formation.calls == 0
        assert ORDER == []
    finally:
        await channel.abort()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault_point", "delivery_code"),
    [
        (FaultPoint.EVENT_BEFORE_JOURNAL_APPEND, "FAILED"),
        (FaultPoint.EVENT_AFTER_JOURNAL_APPEND, "OUTCOME_UNKNOWN"),
    ],
)
async def test_failed_or_unknown_delivery_never_triggers_formation(
    fault_point, delivery_code
) -> None:
    ORDER.clear()
    plan = build_shape2_plan()
    controller = event_controller(
        fault_point, event_type=RuntimeEventType.OUTPUT_DELTA
    )
    emitter, channel = await make_hook_emitter(controller=controller)
    try:
        writer = RecordingWriter(receipt=committed_receipt())
        formation = RecordingFormation()
        store, state, committer, gate = build_committer(
            plan, emitter, writer=writer, formation=formation, controller=controller
        )
        completion = await committer.commit(
            claim_for(plan, "synthesis"), FINAL_RESULT, state
        )
        assert completion.succeeded is False
        assert completion.delivery_status.value == delivery_code
        assert writer.calls == 0
        assert formation.calls == 0
        assert "conversation_commit" not in ORDER
        assert "formation" not in ORDER
    finally:
        await channel.abort()


@pytest.mark.asyncio
async def test_delivered_but_conversation_commit_failed_skips_formation() -> None:
    ORDER.clear()
    plan = build_shape2_plan()
    emitter, channel = await make_hook_emitter()
    try:
        writer = RecordingWriter(error=RuntimeError("memory down"))
        formation = RecordingFormation()
        store, state, committer, gate = build_committer(
            plan, emitter, writer=writer, formation=formation
        )
        completion = await committer.commit(
            claim_for(plan, "synthesis"), FINAL_RESULT, state
        )
        assert completion.succeeded is False
        assert completion.error_code == "FINAL_OUTPUT_MEMORY_COMMIT_FAILED"
        assert completion.delivery_status is DeliveryStatus.DELIVERED
        assert writer.calls == 1
        assert formation.calls == 0
        assert ORDER == ["delivery", "conversation_commit"]
    finally:
        await channel.abort()


@pytest.mark.asyncio
async def test_delivered_with_committed_exchange_triggers_formation_in_order() -> None:
    ORDER.clear()
    plan = build_shape2_plan()
    emitter, channel = await make_hook_emitter()
    try:
        writer = RecordingWriter(receipt=committed_receipt())
        formation = RecordingFormation()
        store, state, committer, gate = build_committer(
            plan, emitter, writer=writer, formation=formation
        )
        completion = await committer.commit(
            claim_for(plan, "synthesis"), FINAL_RESULT, state
        )
        assert completion.succeeded is True
        assert completion.error_code is None
        assert completion.delivery_status is DeliveryStatus.DELIVERED
        assert completion.formation_status == "SUCCEEDED"
        assert completion.formation_error_code is None
        assert writer.calls == 1
        assert formation.calls == 1
        assert formation.receipts[0] is not None
        assert formation.receipts[0].exchange_id == "run-1"
        # canonical ordering 可证明。
        assert ORDER == ["delivery", "conversation_commit", "formation"]
    finally:
        await channel.abort()


@pytest.mark.asyncio
async def test_persist_disabled_receipt_none_skips_formation() -> None:
    ORDER.clear()
    plan = build_shape2_plan()
    emitter, channel = await make_hook_emitter()
    try:
        writer = RecordingWriter(receipt=None)  # persist=False → 无 receipt
        formation = RecordingFormation()
        store, state, committer, gate = build_committer(
            plan, emitter, writer=writer, formation=formation
        )
        completion = await committer.commit(
            claim_for(plan, "synthesis"), FINAL_RESULT, state
        )
        assert completion.succeeded is True
        assert writer.calls == 1
        assert formation.calls == 0
        assert completion.formation_status is None
    finally:
        await channel.abort()


@pytest.mark.asyncio
async def test_no_writer_receipt_means_no_formation() -> None:
    ORDER.clear()
    plan = build_shape2_plan()
    emitter, channel = await make_hook_emitter()
    try:
        formation = RecordingFormation()
        store, state, committer, gate = build_committer(
            plan, emitter, formation=formation
        )
        completion = await committer.commit(
            claim_for(plan, "synthesis"), FINAL_RESULT, state
        )
        assert completion.succeeded is True
        assert formation.calls == 0
    finally:
        await channel.abort()


# ---------------------------------------------------------------------------
# Delivery / terminal isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_code"),
    [
        (SemanticFormationStatus.FAILED, "FORMATION_MODEL_FAILED"),
        (SemanticFormationStatus.PARTIAL, "FORMATION_PERSISTENCE_FAILED"),
        (SemanticFormationStatus.CANCELLED, "FORMATION_CANCELLED"),
        (SemanticFormationStatus.TIMED_OUT, "FORMATION_TIMED_OUT"),
    ],
)
async def test_formation_failure_never_changes_delivery_or_step(
    status, error_code
) -> None:
    ORDER.clear()
    plan = build_shape2_plan()
    emitter, channel = await make_hook_emitter()
    try:
        writer = RecordingWriter(receipt=committed_receipt())
        formation = RecordingFormation(
            result=formation_result(status=status, error_code=error_code)
        )
        store, state, committer, gate = build_committer(
            plan, emitter, writer=writer, formation=formation
        )
        completion = await committer.commit(
            claim_for(plan, "synthesis"), FINAL_RESULT, state
        )
        # delivered output / final Step / completion 语义完全不受 Formation 影响。
        assert completion.succeeded is True
        assert completion.error_code is None
        assert completion.delivery_status is DeliveryStatus.DELIVERED
        assert completion.delivery_error_code is None
        assert completion.formation_status == status.value
        assert completion.formation_error_code == error_code
        assert state.steps["synthesis"].status.value == "SUCCEEDED"
        # at-most-once：gate 只尝试一次，无 re-delivery / second terminal。
        assert gate.attempted is True
        assert gate.last_attempt.delivery_status is DeliveryStatus.DELIVERED
    finally:
        await channel.abort()


@pytest.mark.asyncio
async def test_formation_raising_exception_is_still_isolated() -> None:
    ORDER.clear()
    plan = build_shape2_plan()
    emitter, channel = await make_hook_emitter()
    try:
        writer = RecordingWriter(receipt=committed_receipt())
        formation = RecordingFormation(error=RuntimeError("bug"))
        store, state, committer, gate = build_committer(
            plan, emitter, writer=writer, formation=formation
        )
        completion = await committer.commit(
            claim_for(plan, "synthesis"), FINAL_RESULT, state
        )
        assert completion.succeeded is True
        assert completion.delivery_status is DeliveryStatus.DELIVERED
        assert completion.formation_status == "FAILED"
        assert completion.formation_error_code == "FORMATION_INTERNAL_ERROR"
    finally:
        await channel.abort()


@pytest.mark.asyncio
async def test_formation_cancelled_error_is_still_isolated() -> None:
    ORDER.clear()
    plan = build_shape2_plan()
    emitter, channel = await make_hook_emitter()
    try:
        writer = RecordingWriter(receipt=committed_receipt())
        formation = RecordingFormation(error=asyncio.CancelledError())
        store, state, committer, gate = build_committer(
            plan, emitter, writer=writer, formation=formation
        )
        completion = await committer.commit(
            claim_for(plan, "synthesis"), FINAL_RESULT, state
        )
        assert completion.succeeded is True
        assert completion.error_code is None
        assert completion.delivery_status is DeliveryStatus.DELIVERED
        assert completion.formation_status == "CANCELLED"
        assert completion.formation_error_code == "FORMATION_CANCELLED"
        assert state.steps["synthesis"].status.value == "SUCCEEDED"
    finally:
        await channel.abort()


@pytest.mark.asyncio
async def test_safe_completion_result_never_carries_formation_content() -> None:
    plan = build_shape2_plan()
    emitter, channel = await make_hook_emitter()
    try:
        writer = RecordingWriter(receipt=committed_receipt())
        formation = RecordingFormation()
        store, state, committer, gate = build_committer(
            plan, emitter, writer=writer, formation=formation
        )
        final = StepResult(
            "synthesis",
            "synthesis_agent",
            ResultContentType.TEXT,
            "SECRET_FINAL_TEXT_DO_NOT_PERSIST",
        )
        completion = await committer.commit(
            claim_for(plan, "synthesis"), final, state
        )
        rendered = repr(completion)
        assert "SECRET_FINAL_TEXT_DO_NOT_PERSIST" not in rendered
    finally:
        await channel.abort()


# ---------------------------------------------------------------------------
# Production wiring（CoordinatedRuntimeFactory canonical path）
# ---------------------------------------------------------------------------


class FormationAwareFakeModel(FakeModel):
    """FakeModel + Formation extraction 分支。"""

    def __init__(self, *args, formation_json: str, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.formation_json = formation_json
        self.formation_calls = 0
        self.formation_messages: List[list] = []

    def generate(self, messages, **kwargs):
        system = "\n".join(
            message["content"]
            for message in messages
            if message["role"] == "system"
        )
        if "长期记忆候选提取器" in system:
            self.formation_calls += 1
            self.formation_messages.append(list(messages))
            yield self.formation_json
        else:
            yield from super().generate(messages, **kwargs)


def formation_candidates_json(*candidates: dict) -> str:
    return json.dumps(
        {"schema_version": 1, "candidates": list(candidates)}, ensure_ascii=False
    )


@pytest.mark.asyncio
async def test_production_wiring_forms_memory_and_emits_event(tmp_path) -> None:
    from core.runtime import CoordinatedRuntimeFactory
    from tests._wp3_fixtures import direct_json

    services = make_wp3_services()
    memory = MemoryManager(str(tmp_path / "memory.db"))
    user_request = "以后这个项目统一用 uv 管包。"
    model = FormationAwareFakeModel(
        planning_json=direct_json(),
        formation_json=formation_candidates_json(
            {
                "disposition": "REMEMBER",
                "category": "STABLE_USER_PREFERENCE",
                "canonical_text": "项目统一使用 uv 管理依赖。",
                "value": "uv",
                "logical_key": "project.package_manager",
                "source_excerpt": "统一用 uv",
                "reason_code": "EXPLICIT_PREFERENCE",
            }
        ),
    )
    router = make_real_router(memory, model=model)
    scope = await CoordinatedRuntimeFactory(router, services).create_run_scope(
        "core_router", user_request
    )
    try:
        result = await scope.execute()
        assert result.status is RunStatus.SUCCEEDED
        # Formation extraction 真实发生在 canonical production path 上。
        assert model.formation_calls == 1
        advanced = AdvancedMemoryStore(str(tmp_path / "memory.db"))
        records = advanced.list_by_agent("core_router")
        assert len(records) == 1
        record = records[0]
        assert record.payload == {"value": "uv"}
        assert record.logical_key == "project.package_manager"
        assert record.origin.formation_method == "HYBRID"
        assert record.origin.origin_type == "DELIVERED_EXCHANGE"
        assert record.origin.origin_run_id == result.run_id
        assert record.origin.origin_agent_id == "core_router"
        assert record.origin.origin_memory_scope == "direct"
        # journal-first typed Formation observation event。
        journal_records = services.event_journal.read_after(result.run_id, 0, 1000)
        formation_events = [
            item
            for item in journal_records
            if item.event_type.value == "MEMORY_FORMATION_COMPLETED"
        ]
        assert len(formation_events) == 1
        payload = formation_events[0].safe_payload
        assert payload["status"] == "SUCCEEDED"
        assert payload["persisted_count"] == 1
        assert record.memory_id in payload["candidate_outcomes"]
        assert user_request not in str(payload)
    finally:
        await scope.close()


@pytest.mark.asyncio
async def test_production_formation_failure_never_fails_run(tmp_path) -> None:
    from core.runtime import CoordinatedRuntimeFactory
    from tests._wp3_fixtures import direct_json

    services = make_wp3_services()
    memory = MemoryManager(str(tmp_path / "memory.db"))
    model = FormationAwareFakeModel(
        planning_json=direct_json(),
        formation_json="{malformed",  # strict parser → typed FAILED
    )
    router = make_real_router(memory, model=model)
    scope = await CoordinatedRuntimeFactory(router, services).create_run_scope(
        "core_router", "随便问点东西"
    )
    try:
        result = await scope.execute()
        # Formation FAILED：delivery / Run terminal 完全不受影响。
        assert result.status is RunStatus.SUCCEEDED
        assert result.error_code is None
        assert model.formation_calls == 1
        advanced = AdvancedMemoryStore(str(tmp_path / "memory.db"))
        assert advanced.list_by_agent("core_router") == []
        journal_records = services.event_journal.read_after(result.run_id, 0, 1000)
        formation_events = [
            item
            for item in journal_records
            if item.event_type.value == "MEMORY_FORMATION_COMPLETED"
        ]
        assert len(formation_events) == 1
        assert formation_events[0].safe_payload["status"] == "FAILED"
        assert (
            formation_events[0].safe_payload["safe_error_code"]
            == "FORMATION_OUTPUT_INVALID"
        )
        # conversation exchange 已提交且不因 Formation 失败回滚。
        assert memory.count_messages("core_router") == 2
    finally:
        await scope.close()
