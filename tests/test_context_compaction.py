"""仅四组 Context 验收，合成内容经真实 PG/Runtime/Model Owner。"""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.runtime.agent_loop.context import ContextPolicyV1, ContextSnapshotV1, snapshot_candidate
from core.runtime.agent_loop.compaction import parse_summary, validate_candidate
from core.runtime.agent_loop.contracts import LoopStopped
from core.runtime.agent_loop.context_evidence import capture_context_evidence, build_context_evidence
from core.runtime.execution_aggregate import canonical_payload_digest
from core.runtime.model_invocation import ModelAdapterResponse, ModelAdapterInvocationError
from core.runtime.model_routing import ModelFailureCategory
from core.runtime.tool_adapters import ToolAdapterResponse
from tests.test_agent_loop_runtime import assembly, execute, native, ObservationTool


class LargeObservationTool(ObservationTool):
    def __init__(self, name):
        super().__init__(name)
        self.spec = replace(self.spec, max_output_bytes=32768)

    def invoke_once(self, invocation, context):
        first = not self.calls
        self.calls.append(invocation.invocation_id)
        return ToolAdapterResponse(json.dumps({"public": "public old read-only fact. " * (750 if self.spec.tool_name == "loop_a" and first else 10)}), "application/json", "public result")


class ContextProvider:
    def __init__(self, directory=None, *, rejection=False, invalid=False):
        self.requests = []
        self.directory, self.rejection, self.invalid = directory, rejection, invalid

    def supports_native_tool_calling(self):
        return True

    def invoke_request(self, messages, *, max_tokens, generation_options, run_context):
        purpose = run_context.loop_model_purpose
        record = {"purpose": purpose, "iteration": run_context.loop_iteration_no,
            "messages": messages, "options": generation_options}
        self.requests.append(record)
        if self.directory:
            with (self.directory / "context_calls.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"purpose": purpose, "iteration": run_context.loop_iteration_no}) + "\n")
        if purpose == "COMPACTION":
            data = json.loads(messages[-1]["content"])
            body = {"version": 1, "groups": [{"source_ref": d["source_ref"], "facts": ["public retained fact"],
                "decisions": [], "open_items": []} for d in data]}
            if self.invalid:
                body["groups"][0]["source_ref"] = "source:999"
            return ModelAdapterResponse(json.dumps(body), finish_reason="stop", tool_call_count=0)
        iteration = run_context.loop_iteration_no
        if self.rejection and iteration == 4:
            raise ModelAdapterInvocationError(ModelFailureCategory.CONTEXT_LIMIT_EXCEEDED,
                provider_responded=True, confirmed_context_rejection=True)
        if iteration <= 3:
            return native(["loop_a", "loop_b", "loop_a"][iteration - 1], f"context-call-{iteration}")
        return ModelAdapterResponse("public context final", finish_reason="stop", tool_call_count=0)


async def context_assembly(database, directory, *, provider=None, repository=None, control=None, trigger=80):
    return await assembly(database, directory, [], tools=[LargeObservationTool(n) for n in ("loop_a", "loop_b")],
        context_model=provider or ContextProvider(), repository=repository, control=control,
        context_policy=ContextPolicyV1(trigger_percent=trigger).to_dict(), context_window=29000)


@pytest.mark.asyncio
async def test_long_context_compacts_and_finishes(clean_database, tmp_path):
    factory, repo, model, tools = await context_assembly(clean_database, tmp_path)
    result, run_id = await execute(factory)
    image = await repo.load(run_id)
    assert result.status.value == "SUCCEEDED", (result.error_code, image.loop_frames[0].stop_code, [(r["purpose"], r["iteration"]) for r in model.requests], [i.decision_kind for i in image.loop_iterations], image.loop_frames[0].context_seed["protocol_mode"])
    assert len(image.context_operations) == 1
    operation = image.context_operations[0]
    assert operation.state == "ACCEPTED" and operation.attempt_count == 1
    assert len(image.models) == image.root.budget_consumed["model_calls"] == 5
    summary_request = next(r for r in model.requests if r["purpose"] == "COMPACTION")
    assert "additionalProperties" in summary_request["messages"][0]["content"]
    assert "response_format" not in summary_request["options"]
    assert image.root.budget_consumed["cost_units"] == 5
    assert [len(t.calls) for t in tools] == [2, 1]
    assert len(image.loop_iterations) == 4 and image.loop_frames[0].final_delivery_receipt
    final = model.requests[-1]
    assert final["purpose"] == "DECISION"
    assert [m["tool_call_id"] for m in final["messages"] if m["role"] == "tool"] == ["context-call-2", "context-call-3"]
    assert any(m["role"] == "user" and "MODEL_DERIVED_UNTRUSTED_DATA" in m["content"] for m in final["messages"])
    assert all(m.request_assessment["admissible"] for m in image.models)
    assert all(m.request_assessment["options_bound"] > 0 and m.request_assessment["framing_bound"] > 0 for m in image.models)
    evidence = await capture_context_evidence(repo, run_id)
    assert evidence["body"]["coverage"] == "COMPLETE", evidence["body"]["gaps"]
    assert await capture_context_evidence(repo, run_id) == evidence
    assert "public retained fact" not in json.dumps(evidence)
    partial = build_context_evidence(replace(image, context_snapshots=()))
    assert partial["coverage"] == "PARTIAL" and "CONTEXT_SNAPSHOT_MISSING" in partial["gaps"]


def test_mandatory_source_pairs_and_invalid_summary():
    frame = SimpleNamespace(context_policy=ContextPolicyV1().to_dict(), context_seed={"messages": [{"role": "system", "content": "frozen safety"}, {"role": "user", "content": "task"}]},
        current_context_snapshot_id=None, current_context_snapshot_digest=None, current_context_snapshot_version=None,
        seed_digest="a" * 64, config_digest="b" * 64, committed_observation_cursor=3)
    groups = [{"ref": f"source:{i}", "mandatory": i > 1, "pair_digest": canonical_payload_digest([i]),
        "messages": [{"role": "assistant", "tool_calls": [{"id": f"c{i}", "function": {"name": "public", "arguments": "{}"}}]},
                     {"role": "tool", "tool_call_id": f"c{i}", "content": "result"}]} for i in range(1, 4)]
    summary = {"version": 1, "groups": [{"source_ref": "source:1", "facts": ["fact"], "decisions": [], "open_items": []}]}
    response = lambda body: ModelAdapterResponse(body, finish_reason="stop", tool_call_count=0)
    assert parse_summary(response(json.dumps(summary)), groups) == summary
    artifact = snapshot_candidate(frame, groups, {}, 128, summary=summary)
    assert artifact.payload["messages"][-4:] == groups[1]["messages"] + groups[2]["messages"]
    assert artifact.payload["messages"][1] == frame.context_seed["messages"][1]
    altered = artifact.payload
    altered["messages"][-1]["content"] = "tampered mandatory"
    with pytest.raises(LoopStopped, match="CONTEXT_VALIDATION_FAILED"):
        validate_candidate(frame, groups, ContextSnapshotV1.create(altered), [], [])
    summary["groups"][0]["source_ref"] = "source:3"
    with pytest.raises(LoopStopped):
        parse_summary(response(json.dumps(summary)), groups)
    with pytest.raises(LoopStopped):
        parse_summary(response('{"version":1,"version":1,"groups":[]}'), groups)


@pytest.mark.asyncio
async def test_context_budget_rejection_and_partial(clean_database, tmp_path):
    from core.runtime.model_context import assess_request
    from core.runtime.model_selection import ModelProfile, ModelProfileId
    profile = ModelProfile(ModelProfileId.LOCAL_FAST, 10, 8, True, True, True, True, 1, 1)
    assert not assess_request([{"role": "user", "content": "large" * 30}], {}, 8, profile).admissible
    assert assess_request([], {}, 1, profile).mode == "HEURISTIC"
    # 关闭软触发，Provider 明确拒绝后只在原 iteration 内绑定一次。
    factory, repo, model, _ = await context_assembly(clean_database, tmp_path, provider=ContextProvider(rejection=True), trigger=100)
    result, run_id = await execute(factory)
    image = await repo.load(run_id)
    assert result.status.value == "FAILED"
    assert image.loop_frames[0].stop_code == "LOOP_CONTEXT_EXHAUSTED"
    assert len(image.loop_iterations) == 4
    assert image.loop_iterations[-1].context_rebind_count == 1
    assert sum(m.state == "REJECTED" for m in image.models) == 2
    assert len(image.context_operations) == 1 and image.context_operations[0].attempt_count == 1
    evidence = await capture_context_evidence(repo, run_id)
    assert evidence["body"]["coverage"] == "COMPLETE", evidence["body"]["gaps"]
    assert build_context_evidence(replace(image, models=tuple(replace_model_without_assessment(m) for m in image.models)))["coverage"] == "PARTIAL"


def replace_model_without_assessment(model):
    return SimpleNamespace(**{column.name: (None if column.name == "request_assessment" else getattr(model, column.name)) for column in model.__table__.columns})


async def context_crash_worker(phase, dsn, run_id, output_directory):
    import os
    from core.persistence.database import Database, DatabaseConfig
    from core.persistence.repositories.execution import DurableExecutionRepository
    from core.runtime.run_control import DurableRunControlService
    from core.runtime.recovery_coordinator import RecoveryCoordinator

    class ContextBoundaryRepository(DurableExecutionRepository):
        async def finish_model_attempt(self, lease, **kwargs):
            await super().finish_model_attempt(lease, **kwargs)
            image = await self.load(lease.run_id)
            row = next(m for m in image.models if m.model_attempt_number == kwargs["model_attempt"])
            if phase == "context-result" and row.purpose == "COMPACTION" and row.state == "COMPLETED":
                os._exit(81)

        async def accept_context_and_open_iteration(self, lease, frame_id, **kwargs):
            iteration = await super().accept_context_and_open_iteration(lease, frame_id, **kwargs)
            if phase == "context-accepted" and kwargs["candidate"]["kind"] == "COMPACTED":
                os._exit(82)
            return iteration

    directory = Path(output_directory)
    database = Database(DatabaseConfig(url=dsn, use_null_pool=True))
    control = DurableRunControlService(database, lease_seconds=1)
    repo = ContextBoundaryRepository(database, control)
    factory, _, _, _ = await context_assembly(database, directory, provider=ContextProvider(directory), repository=repo, control=control)
    if phase == "context-result":
        await execute(factory, run_id=run_id)
        raise AssertionError("missing completed-compaction crash")

    async def recover(rid, lease, image):
        scope = await factory.create_rehydrated_run_scope(image, lease=lease, run_id=rid, persist=False)
        try:
            result = await scope.execute()
            assert result.status.value == "SUCCEEDED", (result.error_code, result.safe_message)
        finally:
            await scope.close(abort=True)

    recovery = RecoveryCoordinator(repo, control, recover, instance_id="context-recovery")
    assert await recovery.scan_once() == 1
    await asyncio.gather(*tuple(recovery._local_tasks))
    image = await repo.load(run_id)
    assert image.root.status == "SUCCEEDED", image.root.last_recovery_error
    await database.dispose()


@pytest.mark.asyncio
async def test_context_crash_recovery_fencing_and_budget(clean_database, request, tmp_path):
    from core.persistence.repositories.execution import DurableExecutionRepository
    from core.runtime.run_control import DurableRunControlService
    from core.runtime.agent_loop.context import context_sources
    from core.runtime.model_invocation import restore_loop_model_response
    from tests.test_agent_loop_runtime import run_recovery_window_worker
    run_id = "context-crash-" + uuid4().hex
    control = DurableRunControlService(clean_database, lease_seconds=1)
    repo = DurableExecutionRepository(clean_database, control)
    await run_recovery_window_worker(request, tmp_path, "context-result", run_id, 81)
    before = await repo.load(run_id)
    assert before.context_operations[0].state == "PREPARED"
    assert before.models[-1].purpose == "COMPACTION" and before.models[-1].state == "COMPLETED"
    assert before.root.budget_consumed["model_calls"] == 4
    await asyncio.sleep(1.1)
    lease = await control.claim(run_id, "context-cas-check")
    image = await repo.prepare_recovery(lease)
    frame = image.loop_frames[0]
    operation = image.context_operations[0]
    with pytest.raises(RuntimeError, match="LOOP_STALE_GENERATION"):
        await repo.record_context_validation(lease, frame.frame_id, resume_generation=0, row_version=frame.row_version,
            operation_id=operation.operation_id, candidate=None, validation=None, reason="VALID")
    with pytest.raises(RuntimeError, match="LOOP_CAS_CONFLICT"):
        await repo.record_context_validation(lease, frame.frame_id, resume_generation=frame.resume_generation, row_version=frame.row_version - 1,
            operation_id=operation.operation_id, candidate=None, validation=None, reason="VALID")
    groups = context_sources(frame, image.loop_iterations)
    summary = parse_summary(restore_loop_model_response(image.models[-1]), groups)
    candidate = snapshot_candidate(frame, groups, image.context_snapshots[-1].payload["options"], 128, summary=summary).payload
    candidate["parent_id"] = "stale-parent"
    with pytest.raises(RuntimeError, match="CONTEXT_REJECTED_STALE"):
        await repo.accept_context_and_open_iteration(lease, frame.frame_id, resume_generation=frame.resume_generation,
            row_version=frame.row_version, candidate=candidate,
            validation={"reason": "VALID", **{k: True for k in ("source", "mandatory", "roles", "pairs", "budget")}}, operation_id=operation.operation_id)
    await control.release(lease)
    await run_recovery_window_worker(request, tmp_path, "context-accepted", run_id, 82)
    accepted = await repo.load(run_id)
    assert accepted.root.budget_consumed == before.root.budget_consumed
    assert len(accepted.models) == 4 and accepted.loop_iterations[-1].state == "OPEN"
    assert accepted.context_operations[0].state == "ACCEPTED"
    assert accepted.loop_iterations[-1].context_snapshot_id == accepted.context_snapshots[-1].snapshot_id
    await asyncio.sleep(1.1)
    await run_recovery_window_worker(request, tmp_path, "context-complete", run_id, 0)
    final = await repo.load(run_id)
    assert final.root.status == "SUCCEEDED"
    assert len(final.models) == final.root.budget_consumed["model_calls"] == 5
    assert final.context_operations[0].attempt_count == 1
    assert final.context_operations[0].created_generation == 0
    assert final.context_snapshots[-1].accepted_generation == 2
    assert final.loop_iterations[-1].accepted_model_attempt_number == 5
    assert final.loop_iterations[-1].model_recovery_count == 0
    assert len((tmp_path / "context_calls.jsonl").read_text(encoding="utf-8").splitlines()) == 5
    assert (await capture_context_evidence(repo, run_id))["body"]["coverage"] == "COMPLETE"


@pytest.mark.asyncio
async def test_context_failure_limits_and_source_preservation(clean_database, tmp_path):
    from core.runtime.budget import RunBudget, BudgetUsage
    from core.runtime.agent_loop.context import context_sources
    # Mandatory seed 超限，不裁剪、不调用模型。
    factory, repo, model, _ = await context_assembly(clean_database, tmp_path)
    factory._router.model_profiles = tuple(replace(p, context_window=1000) for p in factory._router.model_profiles)
    result, rid = await execute(factory)
    assert result.error_code == "LOOP_CONTEXT_EXHAUSTED"
    assert not (await repo.load(rid)).models and not model.requests
    # 近似计数不能给新 policy 授权。
    factory, repo, model, _ = await context_assembly(clean_database, tmp_path)
    factory._router.model_profiles = tuple(replace(p, request_counting_mode="HEURISTIC") for p in factory._router.model_profiles)
    result, rid = await execute(factory)
    assert result.error_code == "CONTEXT_BUDGET_UNCERTAIN" and not model.requests
    # 非法摘要拒绝后无 repair、无额外动作，费用仍保留。
    factory, repo, model, _ = await context_assembly(clean_database, tmp_path, provider=ContextProvider(invalid=True))
    result, rid = await execute(factory)
    image = await repo.load(rid)
    assert result.error_code == "CONTEXT_VALIDATION_FAILED"
    assert len(image.models) == image.root.budget_consumed["model_calls"] == 4
    assert image.context_operations[0].state == "REJECTED" and image.context_operations[0].attempt_count == 1
    assert not image.loop_frames[0].final_delivery_receipt
    groups = context_sources(image.loop_frames[0], image.loop_iterations)
    assert [g["mandatory"] for g in groups] == [False, True, True]
    frame_copy = SimpleNamespace(**{column.name: getattr(image.loop_frames[0], column.name) for column in image.loop_frames[0].__table__.columns})
    frame_copy.context_policy = {**frame_copy.context_policy, "mandatory_iterations": [1]}
    assert all(g["mandatory"] for g in context_sources(frame_copy, image.loop_iterations))
    copied_rows = [SimpleNamespace(**{column.name: getattr(i, column.name) for column in i.__table__.columns}) for i in image.loop_iterations]
    copied_rows[0].action_recipe = {**copied_rows[0].action_recipe, "read_only": False}
    assert context_sources(image.loop_frames[0], copied_rows)[0]["mandatory"]
    frame_copy.context_seed = {**frame_copy.context_seed, "protocol_mode": "structured"}
    structured = context_sources(frame_copy, image.loop_iterations)
    assert all(g["messages"][0]["role"] == "assistant" and g["messages"][1]["role"] == "user" for g in structured)
    assert all(json.loads(g["messages"][0]["content"])["action_id"] == json.loads(g["messages"][1]["content"])["action_id"] for g in structured)
    # 压缩无法取得新额度时，可靠 RAW 继续；下一次调用按原预算停止。
    factory, repo, model, _ = await context_assembly(clean_database, tmp_path)
    result, rid = await execute(factory, budget=RunBudget(max_model_calls=3))
    image = await repo.load(rid)
    assert result.stop_reason.value == "BUDGET_EXHAUSTED"
    assert image.root.budget_consumed["model_calls"] == 3 and not image.context_operations
    # Provider usage 超出预留与 Run 上限：保留真实 usage、保守结算，不产生 winner。
    class ExcessUsageProvider(ContextProvider):
        def invoke_request(self, *args, **kwargs):
            response = super().invoke_request(*args, **kwargs)
            return replace(response, actual_usage=BudgetUsage(input_tokens=40000, total_tokens=40000))
    factory, repo, model, _ = await context_assembly(clean_database, tmp_path, provider=ExcessUsageProvider())
    result, rid = await execute(factory, budget=RunBudget(max_input_tokens=30000))
    image = await repo.load(rid)
    assert result.stop_reason.value == "BUDGET_EXHAUSTED"
    assert image.models[0].usage_evidence["input_tokens"] == 40000
    assert image.models[0].state == "UNKNOWN" and image.models[0].cost_evidence["actual_exceeded_budget"]
    assert not image.loop_iterations[0].decision_id


@pytest.mark.asyncio
async def test_accepted_snapshot_immutable_and_rejected_request_rebinds_once(clean_database, tmp_path):
    from sqlalchemy import update
    from sqlalchemy.exc import DBAPIError
    from core.persistence.models import RuntimeContextSnapshotRow
    class RejectOnceProvider(ContextProvider):
        def invoke_request(self, *args, **kwargs):
            try:
                return super().invoke_request(*args, **kwargs)
            except ModelAdapterInvocationError:
                self.rejection = False
                raise
    factory, repo, _, _ = await context_assembly(clean_database, tmp_path, provider=RejectOnceProvider(rejection=True), trigger=100)
    result, rid = await execute(factory)
    image = await repo.load(rid)
    assert result.status.value == "SUCCEEDED", result.error_code
    assert len(image.loop_iterations) == 4 and image.loop_iterations[-1].context_rebind_count == 1
    assert len(image.models) == image.root.budget_consumed["model_calls"] == 6
    assert sum(m.state == "REJECTED" for m in image.models) == 1
    assert image.root.budget_consumed["retries"] == 1
    assert image.models[-1].context_snapshot_id == image.context_snapshots[-1].snapshot_id
    assert all(m.request_assessment["reliable"] for m in image.models)
    with pytest.raises(DBAPIError, match="CONTEXT_SNAPSHOT_IMMUTABLE"):
        # 直连测试事务保留 trigger 错误码，Database 业务包装会安全抹去细节。
        async with clean_database.engine.begin() as connection:
            await connection.execute(update(RuntimeContextSnapshotRow).where(RuntimeContextSnapshotRow.snapshot_id == image.context_snapshots[-1].snapshot_id).values(payload={"tampered": True}))
    assert (await capture_context_evidence(repo, rid))["body"]["coverage"] == "COMPLETE"
    row = image.context_snapshots[-1]
    missing = SimpleNamespace(**{c.name: (None if c.name == "validation" else getattr(row, c.name)) for c in row.__table__.columns})
    partial = build_context_evidence(replace(image, context_snapshots=(*image.context_snapshots[:-1], missing)))
    assert partial["coverage"] == "PARTIAL" and "CONTEXT_VALIDATION_MISSING" in partial["gaps"]
