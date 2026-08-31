"""WP6-E isolated Episodic Layer1 evaluation harness contracts.

These tests prove the TEST_ONLY / ISOLATED / EXPLICITLY_ENABLED / FAIL_CLOSED
evaluation harness capabilities (54 Gate) against real Coordinator, real
Formation, real Retrieval and the isolated v3 evaluation-execute path.  No real
model is ever invoked.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import server
from core.advanced_memory import (
    AdvancedMemoryStore,
    EpisodeGoal,
    EpisodeGoalAuthority,
    EpisodeObservation,
    EpisodeResult,
    EpisodeSituation,
    EpisodicMemoryRecord,
    MemoryOrigin,
    render_episode_canonical_text,
)
from core.chat_service import ChatService
from core.memory_manager import MemoryManager
from core.runtime import (
    ChatRuntimeMode,
    ChatRuntimeSelector,
    CoordinatedRuntimeFactory,
    RunStatus,
    StopReason,
)
from core.runtime.episodic_evaluation import (
    EpisodicCaptureCollector,
    EpisodicEvaluationCapability,
    EpisodicEvaluationControl,
    EpisodicEvaluationError,
    EpisodicEvidenceRetainer,
    EpisodicFixtureInstaller,
    EpisodicFixtureObservation,
    EpisodicFixtureResult,
    EpisodicFixtureSpec,
    EpisodicReplayRunner,
    deterministic_failed_run_controller,
)
from core.runtime.memory_retrieval import MemoryRetrievalService
from core.runtime.memory_authorization import MemoryAccessPrincipal
from core.runtime.model_context import (
    ContextBuildRequest,
    ContextBuilder,
    ContextSourceType,
    ContextTrustLevel,
)
from tests._runtime_assembly_fixtures import make_services
from tests.test_wp3_history_boundary import FakeModel, make_real_router
from tests._wp3_fixtures import direct_json


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


class EpisodicEvalFakeModel(FakeModel):
    """Deterministic model for planner/agents + safe empty semantic formation."""

    def generate(self, messages, **kwargs):
        self.calls += 1
        self.all_messages.append(list(messages))
        system = "\n".join(
            message["content"]
            for message in messages
            if message["role"] == "system"
        )
        if "长期记忆候选提取器" in system:
            yield '{"schema_version":1,"candidates":[]}'
        elif "遗忘目标提取器" in system:
            yield '{"schema_version":1,"logical_key":null,"source_excerpt":"","safe_reason":"EXPLICIT_FORGET"}'
        else:
            yield from super().generate(messages, **kwargs)


def _memory(tmp_path):
    path = tmp_path / "memory.db"
    return MemoryManager(db_path=str(path)), AdvancedMemoryStore(str(path))


def _episode(memory_id: str, run_id: str, text: str, *, scope: str = "direct", status: str = "SUCCEEDED") -> EpisodicMemoryRecord:
    created = datetime.now(UTC)
    return EpisodicMemoryRecord(
        memory_id=memory_id,
        agent_id="core_router",
        memory_scope=scope,
        origin_run_id=run_id,
        situation=EpisodeSituation(text),
        goal=EpisodeGoal("完成当前任务", EpisodeGoalAuthority.USER_PROVIDED),
        observations=(EpisodeObservation("STEP", "work", status),),
        result=EpisodeResult(status, "COMPLETED", "DELIVERED"),
        origin=MemoryOrigin(
            "runtime_terminal", run_id, f"exchange-{run_id}", "core_router", scope, "EPISODIC_V1"
        ),
        created_at=created,
        updated_at=created,
    )


def _harness_factory(router, services):
    return CoordinatedRuntimeFactory(router, services, event_channel_capacity=32)


async def _run_scope(factory, agent_id: str, query: str, *, controller=None, observer=None):
    scope = await factory.create_run_scope(
        agent_id,
        query,
        fault_controller=controller,
        episodic_evaluation_observer=observer,
    )
    try:
        result = await scope.execute()
    finally:
        await scope.close()
    return result


# ---------------------------------------------------------------------------
# Deterministic FAILED Run seam
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deterministic_failure_produces_real_failed_step_and_terminal(tmp_path) -> None:
    services = make_services(snapshot_enabled=False)
    memory, store = _memory(tmp_path)
    router = make_real_router(memory, model=EpisodicEvalFakeModel(direct_json()))
    retainer = EpisodicEvidenceRetainer()
    factory = _harness_factory(router, services)

    result = await _run_scope(
        factory,
        "core_router",
        "修复 Excel 日志解析失败后恢复",
        controller=deterministic_failed_run_controller(),
        observer=retainer,
    )

    assert result.status is RunStatus.FAILED
    assert result.failed_step_ids, "deterministic failure must produce a real failed Step"
    # Real coordinator terminal is FAILED and the normal Formation observer ran.
    record = store.get_episode_by_origin_run_id(result.run_id, "core_router", "direct")
    assert record.result.terminal_status == "FAILED"
    assert record.result.delivery_status == "NOT_DELIVERED"
    first = retainer.first_formation_receipt()
    assert first is not None
    assert first.outcome == "CREATED"
    assert first.memory_id == record.memory_id
    receipt = retainer.runtime_receipt()
    assert receipt is not None
    assert receipt.terminal_status == "FAILED"
    assert receipt.stop_reason == "UNHANDLED_ERROR"
    assert receipt.formation_outcome == "CREATED"
    assert receipt.canonical_text_sha256 is not None
    assert len(receipt.step_names) >= 1
    assert "FAILED" in receipt.step_statuses


@pytest.mark.asyncio
async def test_episodic_formation_does_not_rewrite_terminal(tmp_path) -> None:
    services = make_services(snapshot_enabled=False)
    memory, _ = _memory(tmp_path)
    router = make_real_router(memory, model=EpisodicEvalFakeModel(direct_json()))
    retainer = EpisodicEvidenceRetainer()
    factory = _harness_factory(router, services)

    result = await _run_scope(
        factory,
        "core_router",
        "修复数据库迁移失败",
        controller=deterministic_failed_run_controller(),
        observer=retainer,
    )

    assert result.status is RunStatus.FAILED
    # Formation observed the real FAILED terminal; terminal was not rewritten.
    receipt = retainer.runtime_receipt()
    assert receipt.terminal_status == "FAILED"
    assert receipt.formation_outcome in {"CREATED", "REUSED"}
    assert result.error_code is not None


# ---------------------------------------------------------------------------
# Formation replay seam
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_first_created_second_reused_single_row(tmp_path) -> None:
    services = make_services(snapshot_enabled=False)
    memory, store = _memory(tmp_path)
    model = EpisodicEvalFakeModel(direct_json())
    router = make_real_router(memory, model=model)
    retainer = EpisodicEvidenceRetainer()
    factory = _harness_factory(router, services)

    result = await _run_scope(factory, "core_router", "修复 Excel 日志解析失败后恢复", observer=retainer)
    model_calls_after_run = model.calls

    first = retainer.first_formation_receipt()
    assert first is not None
    assert first.outcome == "CREATED"
    assert first.memory_id is not None

    runner = EpisodicReplayRunner(store)
    replay = await runner.replay(run_id=result.run_id, retainer=retainer)

    assert replay.outcome == "REUSED"
    assert replay.memory_id == first.memory_id
    # One EPISODIC row; no second Run and no re-execution.
    rows = store.list_active_episodic_for_scope("core_router", "direct", candidate_limit=64)
    assert len(rows.records) == 1
    assert model.calls == model_calls_after_run


@pytest.mark.asyncio
async def test_replay_unknown_run_id_rejected(tmp_path) -> None:
    memory, store = _memory(tmp_path)
    runner = EpisodicReplayRunner(store)
    retainer = EpisodicEvidenceRetainer()
    with pytest.raises(EpisodicEvaluationError) as exc_info:
        await runner.replay(run_id=uuid.uuid4().hex, retainer=retainer)
    assert exc_info.value.error_code == "EPISODIC_EVALUATION_REPLAY_UNKNOWN_RUN_ID"


# ---------------------------------------------------------------------------
# Typed fixture installer
# ---------------------------------------------------------------------------


def _fixture_spec(*, scope: str = "orchestration") -> EpisodicFixtureSpec:
    return EpisodicFixtureSpec(
        fixture_ref="e09-orchestration-fixture",
        agent_id="core_router",
        memory_scope=scope,
        origin_run_id="fixture-origin-1",
        situation="E09 初始 fixture 场景描述",
        goal="完成 E09 fixture 任务",
        observations=(
            EpisodicFixtureObservation(
                observation_type="STEP", name="work", status="SUCCEEDED"
            ),
        ),
        result=EpisodicFixtureResult(
            terminal_status="SUCCEEDED",
            stop_reason="COMPLETED",
            delivery_status="DELIVERED",
        ),
    )


def test_typed_fixture_installs_with_renderer_owned_canonical_text(tmp_path) -> None:
    memory, store = _memory(tmp_path)
    installer = EpisodicFixtureInstaller(store)
    receipt = installer.install(_fixture_spec())

    assert receipt.origin_kind == "DATASET_CONTROLLED_INITIAL_FIXTURE"
    assert receipt.memory_scope == "orchestration"
    record = store.get_episode(receipt.memory_id, "core_router", "orchestration")
    # canonical_text is always renderer-owned, never caller-supplied.
    assert record.canonical_text == render_episode_canonical_text(record)
    assert record.canonical_text.startswith("Situation: ")
    assert "Lesson:" not in record.canonical_text
    assert record.origin.origin_type == "DATASET_CONTROLLED_INITIAL_FIXTURE"
    # Re-install with same origin is idempotent (origin-run identity).
    second = installer.install(_fixture_spec())
    assert second.memory_id == receipt.memory_id


def test_fixture_wrong_scope_stays_foreign_and_not_in_direct(tmp_path) -> None:
    memory, store = _memory(tmp_path)
    installer = EpisodicFixtureInstaller(store)
    receipt = installer.install(_fixture_spec(scope="orchestration"))
    assert receipt.memory_scope == "orchestration"
    # Not visible in the direct episodic narrow read used by production.
    direct = store.list_active_episodic_for_scope("core_router", "direct", candidate_limit=64)
    assert direct.records == ()


def test_caller_canonical_text_fixture_is_impossible(tmp_path) -> None:
    with pytest.raises(TypeError):
        EpisodicFixtureSpec(
            fixture_ref="x", agent_id="a", memory_scope="direct",
            origin_run_id="o", situation="s", goal="g",
            observations=(EpisodicFixtureObservation("STEP", "w", "SUCCEEDED"),),
            result=EpisodicFixtureResult("SUCCEEDED", "COMPLETED", "DELIVERED"),
            canonical_text="caller-owned",  # type: ignore[call-arg]
        )


def test_fixture_rejects_arbitrary_payload_dict(tmp_path) -> None:
    with pytest.raises(EpisodicEvaluationError):
        EpisodicFixtureSpec(
            fixture_ref="x", agent_id="a", memory_scope="direct",
            origin_run_id="o", situation="s", goal="g",
            observations=({"arbitrary": "payload"},),  # type: ignore[arg-type]
            result=EpisodicFixtureResult("SUCCEEDED", "COMPLETED", "DELIVERED"),
        )


# ---------------------------------------------------------------------------
# Layer1 capture (observation only)
# ---------------------------------------------------------------------------


def _capture_bundle(tmp_path, *, episode_text: str, query: str):
    memory, store = _memory(tmp_path)
    store.create_or_get_episode(_episode("episode-a", "run-a", episode_text))
    bundle = MemoryRetrievalService(store).retrieve(
        requester=MemoryAccessPrincipal("core_router"),
        target_owner_agent_id="core_router",
        memory_scope="direct",
        query=query,
    )
    return store, bundle


def test_capture_projects_selected_supplied_injected_without_content(tmp_path) -> None:
    _, bundle = _capture_bundle(
        tmp_path, episode_text="修复 Excel 日志解析失败后恢复", query="Excel 日志解析失败"
    )
    assert bundle.episodic_records, "positive lexical retrieval must select the episode"
    run_b = uuid.uuid4().hex
    collector = EpisodicCaptureCollector(run_b)
    collector.observe_retrieval(run_id=run_b, bundle=bundle)

    item = bundle.episodic_records[0].to_context_item()
    context_result = ContextBuilder().build(
        ContextBuildRequest(run_b, "core_router", [item], 4096, 512)
    )
    collector.observe_injection(target="PLANNING", context_result=context_result)
    collector.observe_injection(target="DIRECT_ENTRY", context_result=context_result)

    artifact = collector.envelope()
    assert artifact is not None
    assert artifact.capture_outcome == "COMPLETE"
    selected = artifact.selection.selected
    assert len(selected) == 1
    assert selected[0].selected is True
    assert selected[0].lexical_match_score > 0
    memory_id = selected[0].memory_id
    assert artifact.supplied.episodic_memory_ids == (memory_id,)
    assert artifact.supplied.record_count == 1
    assert len(artifact.injected) == 2
    for entry in artifact.injected:
        assert entry.episodic_memory_ids == (memory_id,)
        assert entry.context_record_count == 1
        assert entry.source_type == ContextSourceType.EPISODIC_MEMORY_RETRIEVAL.value
        assert entry.trust_level == ContextTrustLevel.USER_CONTENT.value
    wire = json.dumps(artifact.to_wire_dict(), ensure_ascii=False)
    # PRIVATE_EVALUATION_ARTIFACT must never contain episode body content.
    for forbidden in ("canonical", "Situation", "Goal:", "Lesson", "user_request", "api_key"):
        assert forbidden not in wire


def test_zero_score_is_captured_as_unselected(tmp_path) -> None:
    _, bundle = _capture_bundle(
        tmp_path, episode_text="修复 Excel 日志解析失败后恢复", query="今天天气怎么样"
    )
    assert not bundle.episodic_records, "unrelated query must select nothing"
    run_b = uuid.uuid4().hex
    collector = EpisodicCaptureCollector(run_b)
    collector.observe_retrieval(run_id=run_b, bundle=bundle)
    artifact = collector.envelope()
    assert artifact is not None
    assert artifact.selection.candidate_count >= 1
    item = artifact.selection.selected[0]
    assert item.lexical_match_score == 0
    assert item.selected is False
    assert item.drop_reason == "NO_LEXICAL_MATCH"


def test_capture_collector_never_mutates_and_rejects_run_id_mismatch(tmp_path) -> None:
    _, bundle = _capture_bundle(
        tmp_path, episode_text="修复 Excel 日志解析失败后恢复", query="Excel 日志解析失败"
    )
    run_b = uuid.uuid4().hex
    collector = EpisodicCaptureCollector(run_b)
    before = bundle.episodic_records
    collector.observe_retrieval(run_id=uuid.uuid4().hex, bundle=bundle)
    assert bundle.episodic_records == before
    artifact = collector.envelope()
    assert artifact is None  # mismatched run observed nothing


# ---------------------------------------------------------------------------
# Security negative tests
# ---------------------------------------------------------------------------


def test_normal_chat_request_cannot_carry_evaluation_control() -> None:
    # ChatRequest ignores unknown fields: the field is structurally never read.
    request = server.ChatRequest(
        agent_id="core_router",
        query="test",
        run_id=uuid.uuid4().hex,
        evaluation_control={"capabilities": ["DETERMINISTIC_FAILED_RUN"]},
    )
    assert not hasattr(request, "evaluation_control")


def test_runtime_execute_request_rejects_evaluation_control() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        server.RuntimeExecuteRequest(
            agent_id="a",
            query="q",
            run_id=uuid.uuid4().hex,
            timeout_seconds=30.0,
            evaluation_control={"capabilities": ["DETERMINISTIC_FAILED_RUN"]},
        )


def test_unknown_evaluation_control_rejected() -> None:
    with pytest.raises(ValidationError):
        server.EpisodicEvaluationControlRequest(
            capabilities=["EXECUTE_PYTHON"],
        )


def test_arbitrary_fixture_payload_rejected() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        server.EpisodicFixtureSpecRequest(
            fixture_ref="x",
            agent_id="a",
            memory_scope="direct",
            origin_run_id="o",
            situation="s",
            goal="g",
            observations=[],
            result=server.EpisodicFixtureResultRequest(
                terminal_status="SUCCEEDED", stop_reason="COMPLETED", delivery_status="DELIVERED"
            ),
            arbitrary_payload={"x": 1},
        )


def test_caller_canonical_text_field_rejected() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        server.EpisodicFixtureSpecRequest(
            fixture_ref="x",
            agent_id="a",
            memory_scope="direct",
            origin_run_id="o",
            situation="s",
            goal="g",
            observations=[],
            result=server.EpisodicFixtureResultRequest(
                terminal_status="SUCCEEDED", stop_reason="COMPLETED", delivery_status="DELIVERED"
            ),
            canonical_text="caller-owned",
        )


def test_illegal_capability_composition_rejected() -> None:
    with pytest.raises(EpisodicEvaluationError):
        EpisodicEvaluationControl(
            capabilities=frozenset(
                {
                    EpisodicEvaluationCapability.DETERMINISTIC_FAILED_RUN,
                    EpisodicEvaluationCapability.REPLAY_EPISODIC_FORMATION_OBSERVER,
                }
            ),
            replay_run_id=uuid.uuid4().hex,
        )


def test_install_fixture_without_typed_fixture_rejected() -> None:
    with pytest.raises(EpisodicEvaluationError):
        EpisodicEvaluationControl(
            capabilities=frozenset({EpisodicEvaluationCapability.INSTALL_EPISODIC_FIXTURE})
        )


def test_replay_without_replay_run_id_rejected() -> None:
    with pytest.raises(EpisodicEvaluationError):
        EpisodicEvaluationControl(
            capabilities=frozenset(
                {EpisodicEvaluationCapability.REPLAY_EPISODIC_FORMATION_OBSERVER}
            )
        )


@pytest.mark.asyncio
async def test_replay_run_id_mismatch_rejected_by_endpoint(tmp_path, monkeypatch) -> None:
    services = make_services(snapshot_enabled=False)
    memory, _ = _memory(tmp_path)
    router = make_real_router(memory, model=EpisodicEvalFakeModel(direct_json()))
    factory = _harness_factory(router, services)
    service = ChatService(
        router,
        coordinated_runtime_factory=factory,
        run_registry=services.run_registry,
    )
    monkeypatch.setattr(server, "chat_service", service)
    run_id = uuid.uuid4().hex
    payload = server.RuntimeEvaluationExecuteV3Request(
        agent_id="core_router",
        query="test",
        run_id=run_id,
        timeout_seconds=30.0,
        evaluation_control=server.EpisodicEvaluationControlRequest(
            capabilities=["REPLAY_EPISODIC_FORMATION_OBSERVER"],
            replay_run_id=uuid.uuid4().hex,
        ),
    )
    with pytest.raises(HTTPException) as exc:
        await server.runtime_evaluation_execute_v3_endpoint(payload)
    assert exc.value.status_code == 422
    assert exc.value.detail == "EPISODIC_EVALUATION_REPLAY_RUN_ID_MISMATCH"


# ---------------------------------------------------------------------------
# Isolated v3 endpoint integration
# ---------------------------------------------------------------------------


def _v3_payload(*, run_id: str, query: str, control: dict | None) -> server.RuntimeEvaluationExecuteV3Request:
    return server.RuntimeEvaluationExecuteV3Request(
        agent_id="core_router",
        query=query,
        run_id=run_id,
        timeout_seconds=30.0,
        evaluation_control=(
            server.EpisodicEvaluationControlRequest(**control)
            if control is not None
            else None
        ),
    )


@pytest.mark.asyncio
async def test_v3_failed_run_forms_failed_episode_and_returns_receipts(tmp_path, monkeypatch) -> None:
    services = make_services(snapshot_enabled=False)
    memory, store = _memory(tmp_path)
    router = make_real_router(memory, model=EpisodicEvalFakeModel(direct_json()))
    factory = _harness_factory(router, services)
    service = ChatService(
        router,
        coordinated_runtime_factory=factory,
        run_registry=services.run_registry,
    )
    monkeypatch.setattr(server, "chat_service", service)
    run_id = uuid.uuid4().hex
    response = await server.runtime_evaluation_execute_v3_endpoint(
        _v3_payload(
            run_id=run_id,
            query="修复 Excel 日志解析失败后恢复",
            control={
                "capabilities": ["DETERMINISTIC_FAILED_RUN", "CAPTURE_EPISODIC_PIPELINE"]
            },
        )
    )
    body = json.loads(response.body)
    assert body["protocol_version"] == "localagent-episodic-evaluation-execute.v1"
    assert body["status"] == "FAILED"
    assert body["evaluation_control_status"] == "EXECUTED"
    assert body["evaluation_error_code"] is None
    assert len(body["formation_receipts"]) == 1
    assert body["formation_receipts"][0]["outcome"] == "CREATED"
    assert body["runtime_receipt"]["terminal_status"] == "FAILED"
    record = store.get_episode_by_origin_run_id(run_id, "core_router", "direct")
    assert record.result.terminal_status == "FAILED"
    # Journal must not contain episode body content for this run.
    journal_records = services.event_journal.read_after(run_id, 0, 1000)
    for event in journal_records:
        safe = json.dumps(event.safe_payload, ensure_ascii=False)
        assert "Situation" not in safe
        assert "Lesson" not in safe


@pytest.mark.asyncio
async def test_v3_replay_returns_created_then_reused(tmp_path, monkeypatch) -> None:
    services = make_services(snapshot_enabled=False)
    memory, store = _memory(tmp_path)
    router = make_real_router(memory, model=EpisodicEvalFakeModel(direct_json()))
    factory = _harness_factory(router, services)
    service = ChatService(
        router,
        coordinated_runtime_factory=factory,
        run_registry=services.run_registry,
    )
    monkeypatch.setattr(server, "chat_service", service)
    run_id = uuid.uuid4().hex
    response = await server.runtime_evaluation_execute_v3_endpoint(
        _v3_payload(
            run_id=run_id,
            query="修复 Excel 日志解析失败后恢复",
            control={
                "capabilities": ["REPLAY_EPISODIC_FORMATION_OBSERVER"],
                "replay_run_id": run_id,
            },
        )
    )
    body = json.loads(response.body)
    assert body["status"] == "SUCCEEDED"
    assert len(body["formation_receipts"]) == 1
    assert body["formation_receipts"][0]["outcome"] == "CREATED"
    assert len(body["replay_receipts"]) == 1
    assert body["replay_receipts"][0]["outcome"] == "REUSED"
    assert body["replay_receipts"][0]["memory_id"] == body["formation_receipts"][0]["memory_id"]
    rows = store.list_active_episodic_for_scope("core_router", "direct", candidate_limit=64)
    assert len(rows.records) == 1


@pytest.mark.asyncio
async def test_v3_fixture_install_receipt(tmp_path, monkeypatch) -> None:
    services = make_services(snapshot_enabled=False)
    memory, store = _memory(tmp_path)
    router = make_real_router(memory, model=EpisodicEvalFakeModel(direct_json()))
    factory = _harness_factory(router, services)
    service = ChatService(
        router,
        coordinated_runtime_factory=factory,
        run_registry=services.run_registry,
    )
    monkeypatch.setattr(server, "chat_service", service)
    run_id = uuid.uuid4().hex
    response = await server.runtime_evaluation_execute_v3_endpoint(
        _v3_payload(
            run_id=run_id,
            query="普通查询",
            control={
                "capabilities": ["INSTALL_EPISODIC_FIXTURE"],
                "fixture": {
                    "fixture_ref": "e09-orchestration-fixture",
                    "agent_id": "core_router",
                    "memory_scope": "orchestration",
                    "origin_run_id": "fixture-origin-1",
                    "situation": "E09 fixture 场景",
                    "goal": "完成 fixture 任务",
                    "observations": [
                        {"observation_type": "STEP", "name": "work", "status": "SUCCEEDED"}
                    ],
                    "result": {
                        "terminal_status": "SUCCEEDED",
                        "stop_reason": "COMPLETED",
                        "delivery_status": "DELIVERED",
                    },
                },
            },
        )
    )
    body = json.loads(response.body)
    assert body["evaluation_control_status"] == "EXECUTED"
    assert len(body["fixture_receipts"]) == 1
    fixture = body["fixture_receipts"][0]
    assert fixture["origin_kind"] == "DATASET_CONTROLLED_INITIAL_FIXTURE"
    assert fixture["memory_scope"] == "orchestration"
    record = store.get_episode(fixture["memory_id"], "core_router", "orchestration")
    assert record.canonical_text.startswith("Situation: ")


@pytest.mark.asyncio
async def test_v3_capture_integration_run_b(tmp_path, monkeypatch) -> None:
    """evaluation target -> deterministic fake execution -> real coordinator ->
    real formation -> next run retrieval -> capture selected/supplied/injected."""
    services = make_services(snapshot_enabled=False)
    memory, store = _memory(tmp_path)
    router = make_real_router(memory, model=EpisodicEvalFakeModel(direct_json()))
    factory = _harness_factory(router, services)
    # Run A: real coordinator forms a real Episode.
    run_a = uuid.uuid4().hex
    result_a = await _run_scope(factory, "core_router", "修复 Excel 日志解析失败后恢复")
    assert result_a.status is RunStatus.SUCCEEDED
    formed = store.get_episode_by_origin_run_id(result_a.run_id, "core_router", "direct")
    assert formed.result.terminal_status == "SUCCEEDED"

    service = ChatService(
        router,
        coordinated_runtime_factory=factory,
        run_registry=services.run_registry,
    )
    monkeypatch.setattr(server, "chat_service", service)
    run_b = uuid.uuid4().hex
    response = await server.runtime_evaluation_execute_v3_endpoint(
        _v3_payload(
            run_id=run_b,
            query="Excel 日志解析失败",
            control={"capabilities": ["CAPTURE_EPISODIC_PIPELINE"]},
        )
    )
    body = json.loads(response.body)
    assert body["status"] == "SUCCEEDED"
    capture = body["episodic_capture"]
    assert capture is not None
    assert capture["capture_outcome"] == "COMPLETE"
    selection = capture["selection"]
    assert selection["candidate_count"] >= 1
    selected = [item for item in selection["selected"] if item["selected"]]
    assert len(selected) == 1
    memory_id = selected[0]["memory_id"]
    assert memory_id == formed.memory_id
    assert selected[0]["lexical_match_score"] > 0
    supplied = capture["supplied"]
    assert supplied["record_count"] == 1
    assert supplied["episodic_memory_ids"] == [memory_id]
    injected = capture["injected"]
    assert injected, "episodic injection must be observed from real ContextBuilder results"
    targets = {entry["target"] for entry in injected}
    assert "PLANNING" in targets
    for entry in injected:
        assert entry["episodic_memory_ids"] == [memory_id]
        assert entry["source_type"] == ContextSourceType.EPISODIC_MEMORY_RETRIEVAL.value
        assert entry["trust_level"] == ContextTrustLevel.USER_CONTENT.value
    # PRIVATE artifact must not leak body content.
    wire = json.dumps(capture, ensure_ascii=False)
    assert "Situation" not in wire
    assert "Lesson" not in wire
    assert "canonical" not in wire


@pytest.mark.asyncio
async def test_e08_profile_forms_then_captures_actual_zero_score(tmp_path, monkeypatch) -> None:
    """Frozen E08 vocabulary: target-owned Run A profile, real Run B retrieval."""
    services = make_services(snapshot_enabled=False)
    memory, store = _memory(tmp_path)
    router = make_real_router(memory, model=EpisodicEvalFakeModel(direct_json()))
    service = ChatService(router, coordinated_runtime_factory=_harness_factory(router, services), run_registry=services.run_registry)
    monkeypatch.setattr(server, "chat_service", service)
    run_a = uuid.uuid4().hex
    response_a = await server.runtime_evaluation_execute_v3_endpoint(_v3_payload(
        run_id=run_a,
        query="请整理项目生产环境的发布清单并记录部署方式与回滚步骤",
        control={"capabilities": ["DETERMINISTIC_EPISODIC_SUCCESS_RUN"]},
    ))
    body_a = json.loads(response_a.body)
    assert body_a["status"] == "SUCCEEDED"
    assert body_a["formation_receipts"][0]["outcome"] == "CREATED"
    formed = store.get_episode_by_origin_run_id(run_a, "core_router", "direct")
    assert formed.result.terminal_status == "SUCCEEDED"
    run_b = uuid.uuid4().hex
    response_b = await server.runtime_evaluation_execute_v3_endpoint(_v3_payload(
        run_id=run_b,
        query="请检查数据库连接串的加密配置是否启用",
        control={"capabilities": ["CAPTURE_EPISODIC_PIPELINE"]},
    ))
    capture = json.loads(response_b.body)["episodic_capture"]
    selected = next(item for item in capture["selection"]["selected"] if item["memory_id"] == formed.memory_id)
    assert selected["lexical_match_score"] == 0
    assert selected["selected"] is False
    assert selected["drop_reason"] == "NO_LEXICAL_MATCH"
    assert capture["supplied"]["episodic_memory_ids"] == []
    assert capture["injected"] == []
