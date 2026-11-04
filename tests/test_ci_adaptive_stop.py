"""原 CI 调查、真实隔离 PG 与原 Loop 的五组动态停止验证。"""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest
from sqlalchemy import select

from core.agent_platform.ci_diagnosis import definition, validate_report
from core.agent_platform.ci_diagnosis_tools import register_tools, TOOL_NAMES
from core.agent_platform.ci_evidence import observed_sources, minimum_coverage, canonical_sources
from core.agent_platform.contracts import AgentRegistration
from core.agent_platform.registry import AgentRegistrationBundle
from core.auth import ObjectAuthorizationService
from core.persistence.memory import PostgresMemoryManager, PostgresMemoryManagerBridge
from core.persistence.models import RuntimeLoopActionRow
from core.persistence.sync_bridge import SyncPersistenceBridge
from core.runtime.agent_loop.contracts import LoopSuspended, HANDOFF_FUNCTION
from core.runtime.agent_loop.adaptive_stop import enforce_final_only
from core.runtime.memory_scope import MemoryAccessContext
from core.runtime.model_invocation import ModelAdapterResponse, NativeToolCall
from core.runtime.model_selection import ModelProfile, ModelProfileId, ModelCostProfile
from core.runtime.tool_registry import ToolRegistry
from tests.test_agent_loop_runtime import assembly
from tests.test_ci_incident_resolution import setup_incident


def calls(*items):
    batch = tuple(NativeToolCall(identity, name, json.dumps(arguments)) for identity, name, arguments in items)
    return ModelAdapterResponse("", native_tool_calls=batch, tool_call_count=len(batch), finish_reason="tool_calls",
        assistant_message={"role": "assistant", "content": None, "tool_calls": [{"id": c.provider_tool_call_id,
            "type": "function", "function": {"name": c.tool_name, "arguments": c.arguments_json}} for c in batch]})


class InvestigationProvider:
    def __init__(self, execution, incident_id, *, limited=False, illegal=None):
        self.execution, self.incident_id, self.limited, self.illegal = execution, incident_id, limited, illegal
        self.requests = []

    def supports_native_tool_calling(self):
        return True

    def report(self, insufficient=False):
        evidence = [e for e in self.execution.evidence_manifest if e["availability"] == "AVAILABLE"
            and e["kind"] in ({"ENVIRONMENT_RECORD"} if insufficient else {"PARSER_LOG", "ENVIRONMENT_RECORD", "CASE_REQUIREMENTS"})]
        return dict(schema_version="ci-diagnosis.v1", kind="REPORT", incident_id=self.incident_id,
            diagnosis_status="INSUFFICIENT_EVIDENCE" if insufficient else "SUPPORTED", failure_stage="PARSER",
            root_cause_hypothesis="字段版本超出解析能力", supporting_evidence=[dict(evidence_id=e["evidence_id"], digest=e["digest"]) for e in evidence],
            uncertainties=["缺少解析原始日志，不能确认根因"] if insufficient else [], recommended_checks=["核对解析能力"], recommended_owner_role="TOOL_MAINTAINER")

    def invoke_request(self, messages, *, generation_options, run_context, max_tokens):
        self.requests.append(dict(messages=deepcopy(messages), options=deepcopy(generation_options), iteration=run_context.loop_iteration_no))
        n = len(self.requests)
        by_kind = {e["kind"]: e["evidence_id"] for e in self.execution.evidence_manifest}
        if generation_options.get("tool_choice") == "none":
            if self.illegal == "TOOL_BATCH":
                return calls(("forbidden-tool", TOOL_NAMES[0], {}))
            if self.illegal == "HANDOFF":
                return calls(("forbidden-child", HANDOFF_FUNCTION, dict(target_agent_id="ci_reader", task="继续调查")))
            return ModelAdapterResponse(json.dumps(self.report(self.limited)), finish_reason="stop", tool_call_count=0)
        if self.limited:
            if n == 1:
                return calls(("limited-env", TOOL_NAMES[1], dict(evidence_id=by_kind["ENVIRONMENT_RECORD"])))
            raise AssertionError("预算预留后不应继续调查")
        if n == 1:
            return calls(("context", TOOL_NAMES[0], {}))
        if n == 2:
            return calls(("parser", TOOL_NAMES[1], dict(evidence_id=by_kind["PARSER_LOG"])),
                ("env", TOOL_NAMES[1], dict(evidence_id=by_kind["ENVIRONMENT_RECORD"])))
        if n == 3:
            return calls(("case", TOOL_NAMES[1], dict(evidence_id=by_kind["CASE_REQUIREMENTS"])))
        if n == 4:
            return calls(("extra-comparison", TOOL_NAMES[1], dict(evidence_id=by_kind["COMPARISON_LOG"])))
        return ModelAdapterResponse(json.dumps(self.report()), finish_reason="stop", tool_call_count=0)


async def build(database, tmp_path, *, adaptive=True, limited=False, illegal=None, handoff=False, loop_limits=None, reuse=None):
    if reuse is None:
        incidents, principal, instance, execution, incident = await setup_incident(database, "limited_diagnostics" if limited else "report_pipeline_failure")
    else:
        incidents, principal, instance, execution, incident = reuse.incidents, reuse.principal, reuse.instance, reuse.execution, reuse.incident
    row, _ = await incidents.admit(principal, incident.incident_id, str(uuid4()))
    bridge = SyncPersistenceBridge(asyncio.get_running_loop())
    registry = ToolRegistry()
    register_tools(registry, incidents, bridge)
    agent = definition()
    options = {**agent.business_options, "skills": (), "loop_policy": {**agent.business_options["loop_policy"], "total_tokens": 131072, **(loop_limits or {})}}
    if not adaptive:
        options.pop("adaptive_stop")
    if handoff:
        options["delegation_allowlist"] = ["ci_reader"]
    agent = replace(agent, allowed_tools=frozenset(TOOL_NAMES), business_options=options)
    registrations = [AgentRegistration(agent)]
    if handoff:
        child = replace(agent, agent_id="ci_reader", entry_allowed=False, delegation_allowed=True,
            business_options={**options, "delegation_allowlist": []})
        registrations.append(AgentRegistration(child))
    bundle = AgentRegistrationBundle(registrations=tuple(registrations),
        tool_grants={n: frozenset(r.definition.agent_id for r in registrations) for n in TOOL_NAMES})
    model = InvestigationProvider(execution, incident.incident_id, limited=limited, illegal=illegal)
    profile = ModelProfile(ModelProfileId.LOCAL_FAST, 65536, 512, True, True, True, True, 1, 1,
        ModelCostProfile(ModelProfileId.LOCAL_FAST, False, fixed_call_cost_units=1, estimated_latency_ms=1), False, "ci-stop-test",
        supports_native_tool_calling=True, provider_kind="synthetic", model_identity="ci-stop-model",
        request_counting_mode="BOUNDED_ESTIMATE", request_counting_version="utf8-byte-upper-bound.v1", request_framing_bound=64,
        request_message_framing_bound=16, output_accounting_contract="bounded-visible-output.v1")
    factory, repo, _, _ = await assembly(database, tmp_path, [], context_model=model, business_bundle=bundle,
        model_profile=profile, max_tokens=512, tool_registrations=registry.startup_registrations,
        memory_manager=PostgresMemoryManagerBridge(PostgresMemoryManager(database), bridge))
    from core.runtime.tool_snapshot_store import PostgresToolResolutionSnapshotStore
    factory._router.tool_snapshot_store = PostgresToolResolutionSnapshotStore(database)
    authorization = ObjectAuthorizationService(database)
    access = MemoryAccessContext.from_principal(principal, agent.agent_id, row.session_id)
    await authorization.bind_new(principal, "RUN", row.run_id)
    await authorization.bind_new(principal, "CONVERSATION", access.conversation_id)
    return NS(incidents=incidents, row=row, incident=incident, execution=execution, principal=principal, instance=instance,
        factory=factory, repo=repo, model=model, access=access)


async def scope_for(q, *, timeout=180):
    return await q.factory.create_run_scope(q.row.agent_id, "调查授权 CI 执行失败", run_id=q.row.run_id,
        session_id=q.row.session_id, memory_access_context=q.access, timeout_seconds=timeout, persist=True)


async def run(q, scope=None):
    scope = scope or await scope_for(q)
    try:
        result = await scope.execute()
    finally:
        await scope.close(abort=True)
    return result, await q.repo.load(q.row.run_id)


async def actions(q):
    async with q.incidents.database.session() as session:
        return tuple((await session.scalars(select(RuntimeLoopActionRow).where(RuntimeLoopActionRow.run_id == q.row.run_id)
            .order_by(RuntimeLoopActionRow.iteration_no, RuntimeLoopActionRow.ordinal))).all())


def final_content(image):
    return image.steps[0].typed_result_payload["content"]


@pytest.mark.asyncio
async def test_continue_then_coverage_stops_extra_investigation(clean_database, tmp_path):
    ordinary = await build(clean_database, tmp_path, adaptive=False)
    adaptive = await build(clean_database, tmp_path, reuse=ordinary)
    # 两个 Scope 都先取得相同会话的空历史快照，再启动执行，避免 A 的报告进入 B Seed。
    scope_a, scope_b = await scope_for(ordinary), await scope_for(adaptive)
    (result_a, image_a), (result_b, image_b) = await asyncio.gather(run(ordinary, scope_a), run(adaptive, scope_b))
    assert result_a.status.value == result_b.status.value == "SUCCEEDED", (result_a.error_code, result_b.error_code)
    assert len(image_a.tool_invocations) == 5 and len(image_b.tool_invocations) == 4
    assert image_a.loop_frames[0].adaptive_decision is None
    assert image_a.loop_frames[0].context_seed["messages"] == image_b.loop_frames[0].context_seed["messages"]
    frame = image_b.loop_frames[0]
    assert frame.adaptive_decision["reason"] == "COVERAGE_SUFFICIENT" and frame.adaptive_decision["effective_iteration"] == 4
    assert frame.final_delivery_receipt["status"] == "DELIVERED"
    snap = image_b.context_snapshots[-1]
    assert snap.payload["options"] == {"tools": [], "tool_choice": "none"}
    assert snap.payload["adaptive_decision"] == frame.adaptive_decision
    for q, image in ((ordinary, image_a), (adaptive, image_b)):
        report = validate_report(final_content(image), q.row, q.incident, q.execution, image, await actions(q))
        assert report["diagnosis_status"] == "SUPPORTED"
    assert json.loads(final_content(image_a)) == json.loads(final_content(image_b))
    trace = dict(run_id=image_b.root.run_id, committed_tools=len(image_b.tool_invocations),
        observed_cursors=[i.observation_cursor for i in image_b.loop_iterations if i.state == "OBSERVED"],
        decision=frame.adaptive_decision, snapshot_id=snap.snapshot_id, snapshot_digest=snap.snapshot_digest,
        final_decision_id=frame.result_decision_id, delivery_status=frame.final_delivery_receipt["status"],
        runtime_status=image_b.root.status, business_status=json.loads(final_content(image_b))["diagnosis_status"])
    (tmp_path / "adaptive_execution_trace.json").write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.mark.asyncio
async def test_driver_and_transaction_reject_tool_and_handoff(clean_database, tmp_path, monkeypatch):
    from core.runtime.agent_loop import driver_v2
    for bypass_driver in (False, True):
        for illegal in ("TOOL_BATCH", "HANDOFF"):
            q = await build(clean_database, tmp_path, illegal=illegal, handoff=True)
            with monkeypatch.context() as patch:
                if bypass_driver:
                    patch.setattr(driver_v2, "enforce_final_only", lambda *args: None)
                result, image = await run(q)
            assert result.status.value == "FAILED" and result.error_code == "LOOP_DECISION_INVALID", result.error_code
            assert len(image.tool_invocations) == 4 and len(await actions(q)) == 4
            assert len(q.model.requests) == 4 and image.loop_iterations[-1].decision_id is None
            assert image.loop_frames[0].adaptive_decision is not None


@pytest.mark.asyncio
async def test_insufficient_report_and_no_extra_budget(clean_database, tmp_path):
    q = await build(clean_database, tmp_path, limited=True, loop_limits={"max_tool_calls": 1})
    result, image = await run(q)
    assert result.status.value == "SUCCEEDED", result.error_code
    assert image.loop_frames[0].adaptive_decision["insufficient"] is True
    report = validate_report(final_content(image), q.row, q.incident, q.execution, image, await actions(q))
    assert report["diagnosis_status"] == "INSUFFICIENT_EVIDENCE" and report["uncertainties"]
    with pytest.raises(Exception, match="CI_REPORT_SOURCE_INVALID"):
        validate_report(final_content(image), q.row, q.incident, q.execution, image, [])
    exhausted = await build(clean_database, tmp_path, limited=True, loop_limits={"max_model_calls": 1})
    failed, image = await run(exhausted)
    assert failed.status.value == "FAILED" and len(exhausted.model.requests) == 1
    assert image.root.budget_consumed["model_calls"] == 1
    zero = await build(clean_database, tmp_path, loop_limits={"max_model_calls": 2})
    failed, image = await run(zero)
    assert failed.status.value == "FAILED" and failed.error_code == "LOOP_EVIDENCE_INSUFFICIENT"
    assert len(zero.model.requests) == 1 and image.loop_frames[0].adaptive_decision is None


@pytest.mark.asyncio
async def test_final_only_recovery_preserves_decision_snapshot_and_tools(clean_database, tmp_path, monkeypatch):
    q = await build(clean_database, tmp_path)
    original = q.repo.accept_context_and_open_iteration
    async def pause(*args, **kwargs):
        value = await original(*args, **kwargs)
        if kwargs["candidate"].get("adaptive_decision"):
            raise LoopSuspended("TEST_FINAL_ONLY_SNAPSHOT")
        return value
    scope = await scope_for(q)
    with monkeypatch.context() as patch:
        patch.setattr(q.repo, "accept_context_and_open_iteration", pause)
        try:
            with pytest.raises(LoopSuspended):
                await scope.execute()
        finally:
            await scope.close(abort=True)
    before = await q.repo.load(q.row.run_id)
    restriction, snapshot = before.loop_frames[0].adaptive_decision, before.context_snapshots[-1]
    assert restriction and snapshot.payload["adaptive_decision"] == restriction
    lease = await q.factory.services.durable_run_control.claim(q.row.run_id, "adaptive-recovery")
    recovered = await q.repo.prepare_recovery(lease)
    restored_frame = recovered.loop_frames[0]
    assert await q.repo.consider_loop_final_only(lease, restored_frame.frame_id,
        resume_generation=restored_frame.resume_generation, row_version=restored_frame.row_version, reserve=False) == restriction
    scope = await q.factory.create_rehydrated_run_scope(recovered, lease=lease)
    def no_reconsider(*args, **kwargs):
        raise AssertionError("不得重新判断持久 FINAL_ONLY")
    with monkeypatch.context() as patch:
        patch.setattr(q.repo, "consider_loop_final_only", no_reconsider)
        try:
            assert scope.run_context.data.deadline_at == before.root.absolute_deadline
            from core.runtime.agent_loop.context import pinned_loop_config
            from core.runtime.execution_aggregate import canonical_payload_digest
            old = deepcopy(restored_frame.pinned_config)
            old.pop("adaptive_stop")
            old["agent"]["business_options"].pop("adaptive_stop")
            assert canonical_payload_digest(pinned_loop_config(q.factory._router, q.row.agent_id,
                scope.run_context, frozen_config=old)) == canonical_payload_digest(old)
            result = await scope.execute()
        finally:
            await scope.close(abort=True)
    after = await q.repo.load(q.row.run_id)
    assert result.status.value == "SUCCEEDED", result.error_code
    assert after.loop_frames[0].adaptive_decision == restriction
    assert after.context_snapshots[-1].snapshot_id == snapshot.snapshot_id
    assert len(after.tool_invocations) == len(before.tool_invocations) == 4
    assert len(q.model.requests) == 4 and q.model.requests[-1]["options"]["tool_choice"] == "none"


@pytest.mark.asyncio
async def test_deadline_and_invalid_sources_do_not_become_success(clean_database, tmp_path, monkeypatch):
    q = await build(clean_database, tmp_path)
    original = q.repo.consider_loop_final_only
    async def pause(*args, **kwargs):
        value = await original(*args, **kwargs)
        if value:
            raise LoopSuspended("TEST_FINAL_ONLY_PERSISTED")
        return value
    scope = await scope_for(q, timeout=30)
    with monkeypatch.context() as patch:
        patch.setattr(q.repo, "consider_loop_final_only", pause)
        try:
            with pytest.raises(LoopSuspended):
                await scope.execute()
        finally:
            await scope.close(abort=True)
    image = await q.repo.load(q.row.run_id)
    source_actions = await actions(q)
    frame = image.loop_frames[0]
    seen = observed_sources(image, source_actions, frame, q.incident.incident_id, q.execution.execution_id)
    assert minimum_coverage(seen)
    bad_actions = deepcopy(source_actions)
    for action in bad_actions:
        action.source_digest = "0" * 64
    assert not observed_sources(image, bad_actions, frame, q.incident.incident_id, q.execution.execution_id)
    assert not canonical_sources({k: {**v, "execution_id": str(uuid4())} for k, v in seen.items()}, q.incident.incident_id, q.execution)
    assert not minimum_coverage({k: v for k, v in seen.items() if v["kind"] != "CASE_REQUIREMENTS"})
    await asyncio.sleep(max(0, (image.root.absolute_deadline - datetime.now(timezone.utc)).total_seconds()) + 0.05)
    lease = await q.factory.services.durable_run_control.claim(q.row.run_id, "expired-final-only")
    recovered = await q.repo.prepare_recovery(lease)
    scope = await q.factory.create_rehydrated_run_scope(recovered, lease=lease)
    try:
        result = await scope.execute()
    finally:
        await scope.close(abort=True)
    assert result.status.value == "FAILED" and result.error_code == "DEADLINE_EXCEEDED"
    assert len(q.model.requests) == 3
