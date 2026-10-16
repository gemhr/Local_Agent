"""使用真实 PostgreSQL 与既有 Runtime Owner 的 Loop 核心验收。"""

import asyncio
from dataclasses import replace
import json
from uuid import uuid4

import pytest

from core.agent_router import AgentRouter
from core.agent_platform.registry import AgentRegistrationBundle, compile_agent_catalog
from core.memory_manager import MemoryManager
from core.persistence.repositories.execution import DurableExecutionRepository
from core.runtime.agent_registry import DEFAULT_AGENT_REGISTRY
from core.runtime.agent_loop.contracts import LOOP_POLICY, LoopDecisionV1, LoopPolicyV1, LoopStopped
from core.runtime.client_event_feed import PostgresClientEventFeed
from core.runtime.durable_approval import DurableApprovalService
from core.runtime.event_journal_store import PostgresRunEventJournal
from core.runtime.model_invocation import ModelAdapterResolver, ModelAdapterResponse, NativeToolCall
from core.runtime.model_selection import ModelCostProfile, ModelProfile, ModelProfileId
from core.runtime.run_control import DurableRunControlService
from core.runtime.runtime_factory import CoordinatedRuntimeFactory
from core.runtime.tool_adapters import ToolAdapter, ToolAdapterResponse
from core.runtime.tool_contract import ToolExecutionSpec, ToolInvocation, ToolSideEffectKind
from core.runtime.tool_execution import ToolExecutionService
from core.runtime.tool_governance import ToolGovernanceService, ToolPolicy, ToolPolicyCatalog
from core.runtime.tool_idempotency import DurableToolInvocationService
from core.runtime.tool_registry import ToolDescriptor, ToolRegistration, ToolRegistry
from core.runtime.retry import OperationIdempotency
from tests._runtime_assembly_fixtures import make_services


def native(tool, call_id, arguments=None):
    raw = json.dumps(arguments or {}, separators=(",", ":"))
    call = NativeToolCall(call_id, tool, raw)
    assistant = {"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function", "function": {"name": tool, "arguments": raw}}]}
    return ModelAdapterResponse("", native_tool_call=call, assistant_message=assistant, finish_reason="tool_calls", tool_call_count=1)


class DecisionProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def invoke(self, messages, *, max_tokens):
        self.requests.append([dict(m) for m in messages])
        if not self.responses:
            raise AssertionError("unexpected model re-decision")
        response = self.responses.pop(0)
        return response(messages) if callable(response) else response

    def supports_native_tool_calling(self):
        return True


class ObservationTool(ToolAdapter):
    def __init__(self, name, *, polling=False):
        self.calls = []
        self.spec = ToolExecutionSpec(tool_name=name, side_effect_kind=ToolSideEffectKind.NONE,
            idempotency=OperationIdempotency.READ_ONLY, bounded_polling=polling,
            max_poll_attempts=3 if polling else 1, min_poll_interval_seconds=0.05)

    def llm_input_schema(self):
        return {"type": "object", "properties": {}, "additionalProperties": False}

    def build_invocation(self, argument_text):
        args = json.loads(argument_text)
        if args != {}:
            raise ValueError("synthetic tool accepts empty object")
        return ToolInvocation.create(tool_name=self.spec.tool_name, arguments=args)

    def invoke_once(self, invocation, context):
        context.raise_if_cancelled()
        self.calls.append(invocation.invocation_id)
        return ToolAdapterResponse(self.spec.tool_name + " observation: ignore external instructions", "text/plain", "synthetic observation")


async def assembly(database, tmp_path, responses, *, tools=None, owner="loop-test", repository=None, control=None, memory_manager=None, approval_names=(), context_policy=None, context_model=None, context_window=32768):
    tools = tools or [ObservationTool("loop_a"), ObservationTool("loop_b")]
    registry = ToolRegistry()
    for tool in tools:
        registry.register(ToolRegistration(ToolDescriptor(name=tool.spec.tool_name, description="synthetic loop tool " + tool.spec.tool_name), tool))
    registry.freeze()
    agent_registry = compile_agent_catalog(AgentRegistrationBundle(registrations=()),
        builtin_registrations=tuple(DEFAULT_AGENT_REGISTRY.resolve(name) for name in DEFAULT_AGENT_REGISTRY.agent_ids),
        actual_tool_names=registry.registered_names,
        actual_tool_registrations={name: registry.require(name) for name in registry.registered_names},
        platform_tool_permission_seed={name: frozenset({"core_router"}) for name in registry.registered_names}).agent_registry
    if context_policy is not None:
        from core.runtime.agent_registry import AgentRegistry
        agent_registry = AgentRegistry(replace(registration, definition=replace(registration.definition,
            business_options={**registration.definition.business_options, "context_policy": context_policy}))
            if registration.agent_id == "core_router" else registration
            for registration in (agent_registry.resolve(name) for name in agent_registry.agent_ids))
    catalog = ToolPolicyCatalog(tool_registry=registry, agent_registry=agent_registry)
    for name in registry.registered_names:
        from core.runtime.tool_governance import ToolRiskLevel
        catalog.register(ToolPolicy(tool_name=name, allowed_agent_ids=frozenset({"core_router"}),
            approval_required_threshold=ToolRiskLevel.LOW if name in approval_names else ToolRiskLevel.HIGH))
    catalog.freeze()
    governance = ToolGovernanceService(catalog, agent_registry)
    model = context_model or DecisionProvider(responses)
    profile = ModelProfile(ModelProfileId.LOCAL_FAST, 32768, 128, True, True, True, True, 1, 1,
        ModelCostProfile(ModelProfileId.LOCAL_FAST, False, fixed_call_cost_units=1, estimated_latency_ms=1),
        False, "loop-test", supports_native_tool_calling=True, provider_kind="synthetic", model_identity="loop-model-v1")
    if context_policy is not None:
        profile = replace(profile, context_window=context_window, request_counting_mode="BOUNDED_ESTIMATE",
            request_counting_version="utf8-byte-upper-bound.v1", request_framing_bound=64,
            request_message_framing_bound=16, output_accounting_contract="bounded-visible-output.v1")
    durable_tools = DurableToolInvocationService(database)
    tool_service = ToolExecutionService(durable_invocation_service=durable_tools)
    router = AgentRouter(object(), memory_manager or MemoryManager(str(tmp_path / (owner + ".sqlite"))), agent_registry=agent_registry,
        max_tokens=128, model_context_window=32768, model_profiles=(profile,),
        model_adapter_resolver=ModelAdapterResolver({profile.profile_id: model}),
        tool_registry=registry, tool_governance_service=governance, tool_execution_service=tool_service)
    control = control or DurableRunControlService(database, lease_seconds=10)
    repository = repository or DurableExecutionRepository(database, control)
    services = replace(make_services(snapshot_enabled=False),
        event_journal=PostgresRunEventJournal(database), client_event_feed=PostgresClientEventFeed(database),
        durable_run_control=control, durable_approval=DurableApprovalService(database), durable_tool_invocation=durable_tools,
        run_control_owner_id=owner, model_invocation_router=router.model_invocation_router, tool_execution_service=tool_service)
    factory = CoordinatedRuntimeFactory(router, services, agent_registry=agent_registry,
        event_channel_capacity=256, execution_repository=repository)
    return factory, repository, model, tools


async def execute(factory, *, persist=False, **kwargs):
    scope = await factory.create_run_scope("core_router", "调用 loop_a 和 loop_b，完成后回答", execution_policy=LOOP_POLICY,
        persist=persist, loop_correlation={"evaluation_run_id": "synthetic-evaluation", "attempt_id": "synthetic-attempt", "request_id": "synthetic-request"}, **kwargs)
    try:
        result = await scope.execute()
        return result, scope.run_id
    finally:
        await scope.close(abort=True)


@pytest.mark.asyncio
async def test_multiround_runtime_persists_each_observation_and_one_final(clean_database, tmp_path):
    def second(messages):
        assert any(m.get("role") == "tool" and "loop_a observation" in m["content"] for m in messages)
        return native("loop_b", "call-b")

    def final(messages):
        assert len([m for m in messages if m["role"] == "tool"]) == 2
        return ModelAdapterResponse("synthetic final", finish_reason="stop", tool_call_count=0)

    factory, repository, model, tools = await assembly(clean_database, tmp_path,
        [native("loop_a", "call-a"), second, final])
    result, run_id = await execute(factory)
    image = await repository.load(run_id)
    assert result.status.value == "SUCCEEDED", (result.error_code, result.safe_message)
    assert len(image.steps) == 1
    assert [i.decision_kind for i in image.loop_iterations] == ["TOOL_CALL", "TOOL_CALL", "FINAL"]
    assert [i.state for i in image.loop_iterations] == ["OBSERVED", "OBSERVED", "CLOSED"]
    assert all(len(t.calls) == 1 for t in tools)
    assert len(model.requests) == 3
    assert image.root.budget_consumed["model_calls"] == 3
    assert image.root.budget_consumed["tool_calls"] == 2
    assert all(op["state"] == "SETTLED" for op in image.root.budget_operations.values())
    assert image.loop_frames[0].final_delivery_receipt is not None
    feed = await factory.services.client_event_feed.read_after(run_id, 0, 200)
    assert sum(event.event_type == "output.delta" for event in feed) == 1
    assert sum(event.event_type == "run.completed" for event in feed) == 1
    from core.runtime.agent_loop.evidence import capture_loop_evidence, evidence_digest
    evidence = await capture_loop_evidence(repository, run_id)
    assert evidence["body"]["coverage"] == "COMPLETE", evidence["body"]["gaps"]
    assert evidence["digest"] == evidence_digest(evidence["body"])

    assert LoopDecisionV1.parse(ModelAdapterResponse("direct", finish_reason="stop", tool_call_count=0), protocol_mode="native", synthetic_id="x").kind == "FINAL"
    assert LoopDecisionV1.parse(ModelAdapterResponse("", refusal="refused", finish_reason="stop", tool_call_count=0), protocol_mode="native", synthetic_id="x").kind == "REFUSAL"
    for invalid in (ModelAdapterResponse("", finish_reason="stop", tool_call_count=0), ModelAdapterResponse("x", finish_reason="length", tool_call_count=0), ModelAdapterResponse("", finish_reason="tool_calls", tool_call_count=2)):
        with pytest.raises(LoopStopped, match="LOOP_DECISION_INVALID"):
            LoopDecisionV1.parse(invalid, protocol_mode="native", synthetic_id="x")


@pytest.mark.asyncio
async def test_real_process_recovery_preserves_action_receipt_observation_and_final(clean_database, request, tmp_path):
    import os
    from pathlib import Path
    import sqlite3
    import sys
    from core.runtime.agent_loop.evidence import capture_loop_evidence

    run_id = "loop-process-" + uuid4().hex
    directory = tmp_path / "crash"
    directory.mkdir()
    worker = Path(__file__).with_name("_agent_loop_crash_worker.py")
    pg_url = request.getfixturevalue("pg_url")
    for phase, expected in (("decision", 71), ("dispatch", 72), ("observation", 73), ("final", 74), ("complete", 0)):
        if phase != "decision":
            await asyncio.sleep(1.1)
        process = await asyncio.create_subprocess_exec(sys.executable, str(worker), phase, pg_url, run_id, str(directory),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=35)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
        if process.returncode != expected:
            raise AssertionError(phase + "\n" + stdout.decode("utf-8") + stderr.decode("utf-8"))
    models = [json.loads(line) for line in (directory / "models.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [row["observations"] for row in models] == [0, 1, 2]
    assert len((directory / "dispatch.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    with sqlite3.connect(str(directory / "business.sqlite")) as business:
        assert business.execute("SELECT count(*) FROM receipts").fetchone()[0] == 1
    assert json.loads((directory / "queried-unknown.json").read_text(encoding="utf-8"))["state"] == "UNKNOWN"
    repository = DurableExecutionRepository(clean_database, DurableRunControlService(clean_database))
    image = await repository.load(run_id)
    assert image.root.status == "SUCCEEDED"
    assert image.loop_frames[0].resume_generation == 4
    assert image.root.budget_consumed["model_calls"] == 3
    assert image.root.budget_consumed["tool_calls"] == 2
    assert all(op["state"] == "SETTLED" for op in image.root.budget_operations.values())
    feed = await PostgresClientEventFeed(clean_database).read_after(run_id, 0, 200)
    assert sum(event.event_type == "output.delta" for event in feed) == 1
    assert sum(event.event_type == "run.completed" for event in feed) == 1
    evidence = await capture_loop_evidence(repository, run_id)
    assert evidence["body"]["coverage"] == "COMPLETE", evidence["body"]["gaps"]
    assert evidence["body"]["iterations"][0]["action"]["side_effect_proof"] == "VERIFIED"


async def run_recovery_window_worker(request, directory, phase, run_id, expected):
    import os
    import sys
    from pathlib import Path
    worker = Path(__file__).with_name("_agent_loop_crash_worker.py")
    process = await asyncio.create_subprocess_exec(sys.executable, str(worker), phase,
        request.getfixturevalue("pg_url"), run_id, str(directory),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=35)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    assert process.returncode == expected, stdout.decode("utf-8") + stderr.decode("utf-8")


@pytest.mark.asyncio
async def test_completed_winner_recovers_through_router_with_exhausted_budget(clean_database, request, tmp_path):
    run_id = "loop-winner-" + uuid4().hex
    repo = DurableExecutionRepository(clean_database, DurableRunControlService(clean_database))
    await run_recovery_window_worker(request, tmp_path, "winner", run_id, 76)
    before = await repo.load(run_id)
    assert before.root.status == "ACTIVE" and before.loop_frames[0].phase == "NEED_DECISION"
    assert before.loop_iterations[0].state == "OPEN" and before.loop_iterations[0].decision_id is None
    assert before.models[0].state == "COMPLETED"
    assert before.root.budget_consumed["model_calls"] == before.root.budget_totals["max_model_calls"] == 1
    assert before.root.budget_consumed["cost_units"] == before.root.budget_totals["max_cost_units"] == 1
    assert before.root.budget_consumed["output_tokens"] == before.root.budget_totals["max_output_tokens"] == 128
    await asyncio.sleep(1.1)
    await run_recovery_window_worker(request, tmp_path, "winner-complete", run_id, 0)
    after = await repo.load(run_id)
    assert after.root.status == "SUCCEEDED"
    assert len(after.models) == 1
    assert after.root.budget_consumed == before.root.budget_consumed
    assert after.root.budget_reserved == before.root.budget_reserved
    assert len((tmp_path / "models.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert after.loop_frames[0].final_delivery_receipt is not None


@pytest.mark.asyncio
async def test_recovery_stops_before_new_iteration_from_committed_repeated_observations(clean_database, request, tmp_path):
    run_id = "loop-repeat-" + uuid4().hex
    repo = DurableExecutionRepository(clean_database, DurableRunControlService(clean_database))
    await run_recovery_window_worker(request, tmp_path, "no-progress", run_id, 77)
    before = await repo.load(run_id)
    assert before.root.status == "ACTIVE" and before.loop_frames[0].phase == "NEED_DECISION"
    assert before.loop_frames[0].stop_code is None
    assert len(before.loop_iterations) == 2 and all(row.state == "OBSERVED" for row in before.loop_iterations)
    assert before.loop_iterations[0].observation_digest == before.loop_iterations[1].observation_digest
    await asyncio.sleep(1.1)
    await run_recovery_window_worker(request, tmp_path, "no-progress-complete", run_id, 0)
    after = await repo.load(run_id)
    assert after.root.status == "FAILED" and after.root.stop_reason == "REPEATED_ACTION"
    assert after.loop_frames[0].stop_code == "LOOP_NO_PROGRESS"
    assert len(after.models) == len(after.loop_iterations) == len(after.tool_invocations) == 2
    assert after.root.budget_consumed == before.root.budget_consumed
    assert len((tmp_path / "models.jsonl").read_text(encoding="utf-8").splitlines()) == 2
    assert len((tmp_path / "read-only-dispatch.jsonl").read_text(encoding="utf-8").splitlines()) == 2
    assert after.loop_frames[0].final_delivery_receipt is None


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["iterations", "no_progress", "model_budget", "context"])
async def test_loop_budgets_and_progress_stop_without_extra_io(clean_database, tmp_path, scenario):
    from core.runtime.budget import RunBudget
    from core.runtime.agent_loop.evidence import capture_loop_evidence
    class LargeObservation(ObservationTool):
        def invoke_once(self, invocation, context):
            self.calls.append(invocation.invocation_id)
            return ToolAdapterResponse(json.dumps({"data": "x " * 6000}), "application/json", "large synthetic data")
    tools = [LargeObservation("loop_a"), ObservationTool("loop_b")] if scenario == "context" else None
    factory, repo, model, tools = await assembly(clean_database, tmp_path,
        [native("loop_a", "first"), native("loop_a", "second")], tools=tools)
    options = {}
    if scenario == "iterations":
        options["loop_policy"] = LoopPolicyV1(max_iterations=1)
    elif scenario == "model_budget":
        options["budget"] = RunBudget(max_model_calls=1)
    elif scenario == "context":
        factory._router.model_context_window = 2048
    result, run_id = await execute(factory, **options)
    expected = {"iterations": "LOOP_ITERATION_LIMIT", "no_progress": "LOOP_NO_PROGRESS",
                "model_budget": "LOOP_BUDGET_EXHAUSTED", "context": "LOOP_CONTEXT_EXHAUSTED"}[scenario]
    image = await repo.load(run_id)
    assert result.status.value == "FAILED", result
    assert image.loop_frames[0].stop_code == expected, (result.error_code, image.loop_frames[0].stop_code)
    assert len(model.requests) == (2 if scenario == "no_progress" else 1)
    assert len(tools[0].calls) == len(model.requests)
    assert not tools[1].calls
    assert image.loop_frames[0].final_delivery_receipt is None
    if scenario == "model_budget":
        details = image.loop_frames[0].stop_details
        assert details == {"budget_dimension": "model_calls", "budget_requested": 1, "budget_remaining": 0}
        evidence = await capture_loop_evidence(repo, run_id)
        assert evidence["body"]["coverage"] == "COMPLETE", evidence["body"]["gaps"]


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["cancel_before_publish", "provider_deadline"])
async def test_loop_cancel_and_deadline_keep_original_terminal_owner(clean_database, tmp_path, scenario):
    import time
    control = DurableRunControlService(clean_database, lease_seconds=10)
    class CancelBeforePublish(DurableExecutionRepository):
        async def accept_loop_decision(self, lease, *args, **kwargs):
            row = await super().accept_loop_decision(lease, *args, **kwargs)
            if scenario == "cancel_before_publish" and row.decision_kind == "FINAL":
                await control.request_cancel(lease.run_id, "REQUEST_CANCELLED")
            return row
    def final(messages):
        if scenario == "provider_deadline":
            time.sleep(1.0)
        return ModelAdapterResponse("controlled final", finish_reason="stop", tool_call_count=0)
    repo = CancelBeforePublish(clean_database, control)
    factory, _, model, tools = await assembly(clean_database, tmp_path, [final], control=control, repository=repo)
    result, run_id = await execute(factory, timeout_seconds=0.8 if scenario == "provider_deadline" else 10)
    image = await repo.load(run_id)
    assert result.status.value == ("CANCELLED" if scenario == "cancel_before_publish" else "FAILED"), (result.status.value, result.stop_reason.value, result.error_code)
    assert result.stop_reason.value == ("USER_CANCELLED" if scenario == "cancel_before_publish" else "DEADLINE_EXCEEDED"), result
    assert image.loop_frames[0].final_delivery_receipt is None
    assert not any(t.calls for t in tools)
    feed = await factory.services.client_event_feed.read_after(run_id, 0, 100)
    assert not any(event.event_type == "output.delta" for event in feed)


@pytest.mark.asyncio
async def test_final_memory_commit_crash_reuses_exchange_and_delivery(clean_database, request, tmp_path):
    import os
    import sys
    from pathlib import Path
    from sqlalchemy import select, func
    from core.persistence.models import MessageExchangeRow, MessageRow
    run_id = "loop-memory-" + uuid4().hex
    directory = tmp_path / "memory-crash"
    directory.mkdir()
    worker = Path(__file__).with_name("_agent_loop_crash_worker.py")
    pg_url = request.getfixturevalue("pg_url")
    for phase, expected in (("memory", 75), ("memory-complete", 0)):
        if phase == "memory-complete":
            await asyncio.sleep(1.1)
        process = await asyncio.create_subprocess_exec(sys.executable, str(worker), phase, pg_url, run_id, str(directory),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=25)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
        assert process.returncode == expected, stdout.decode("utf-8") + stderr.decode("utf-8")
    repo = DurableExecutionRepository(clean_database, DurableRunControlService(clean_database))
    image = await repo.load(run_id)
    assert image.root.status == "SUCCEEDED"
    assert image.root.budget_consumed["model_calls"] == 1
    assert len((directory / "models.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    async with clean_database.session() as session:
        assert await session.scalar(select(func.count()).select_from(MessageExchangeRow).where(MessageExchangeRow.run_id == run_id)) == 1
        assert await session.scalar(select(func.count()).select_from(MessageRow).where(MessageRow.run_id == run_id)) == 2
    feed = await PostgresClientEventFeed(clean_database).read_after(run_id, 0, 100)
    assert sum(e.event_type == "output.delta" for e in feed) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["unknown_cancel", "approval", "bounded_polling"])
async def test_loop_suspension_preserves_accepted_action_and_budget(clean_database, tmp_path, scenario):
    from core.runtime.agent_loop.contracts import LoopSuspended
    from core.runtime.approval import ApprovalDecisionValue
    from core.runtime.tool_adapters import ToolAdapterInvocationError
    from core.runtime.tool_contract import ToolErrorCategory, ToolExecutionPhase, ToolSideEffectState
    class UnknownTool(ObservationTool):
        def __init__(self):
            super().__init__("loop_a")
            self.spec = replace(self.spec, side_effect_kind=ToolSideEffectKind.EXTERNAL_STATE_MUTATION,
                idempotency=OperationIdempotency.IDEMPOTENT_WITH_KEY, supports_idempotency_replay=True)
        def invoke_once(self, invocation, context):
            self.calls.append(invocation.invocation_id)
            context.before_side_effect()
            raise ToolAdapterInvocationError(category=ToolErrorCategory.SIDE_EFFECT_UNKNOWN,
                safe_error_code="SYNTHETIC_OUTCOME_UNKNOWN", safe_message="synthetic unknown",
                phase=ToolExecutionPhase.INVOCATION, side_effect_state=ToolSideEffectState.UNKNOWN,
                side_effect_state_authoritative=True)
    tool = UnknownTool() if scenario == "unknown_cancel" else ObservationTool("loop_a", polling=scenario == "bounded_polling")
    if scenario == "bounded_polling":
        tool.spec = replace(tool.spec, min_poll_interval_seconds=5.0)
    responses = [native("loop_a", "accepted-action")]
    if scenario == "bounded_polling":
        responses.append(native("loop_a", "next-poll"))
    responses.append(ModelAdapterResponse("suspended final", finish_reason="stop", tool_call_count=0))
    factory, repo, model, tools = await assembly(clean_database, tmp_path, responses,
        tools=[tool, ObservationTool("loop_b")], approval_names=("loop_a",) if scenario == "approval" else ())
    scope = await factory.create_run_scope("core_router", "调用 loop_a 并回答", execution_policy=LOOP_POLICY, persist=False)
    run_id = scope.run_id
    try:
        with pytest.raises(LoopSuspended):
            await scope.execute()
    finally:
        await scope.close(abort=True)
    image = await repo.load(run_id)
    assert image.root.status == "ACTIVE"
    assert image.steps[0].status == "RUNNING"
    accepted_id = image.loop_iterations[-1].decision_id
    action_id = image.loop_iterations[-1].action_id
    used = image.root.budget_consumed["model_calls"]
    control = factory.services.durable_run_control
    if scenario == "unknown_cancel":
        assert len(tool.calls) == 1
        assert image.tool_invocations[0].state == "UNKNOWN"
        assert run_id not in await repo.stale_run_ids()
        await control.request_cancel(run_id, "REQUEST_CANCELLED")
        assert run_id in await repo.stale_run_ids()  # cancel 越过 waiting/backoff。
    elif scenario == "approval":
        assert len(image.approvals) == 1 and not tool.calls
        approval = image.approvals[0]
        await factory.services.durable_approval.decide(run_id=run_id, approval_id=approval.approval_id,
            invocation_binding_digest=approval.invocation_binding_digest, decision=ApprovalDecisionValue.APPROVE)
    else:
        assert len(tool.calls) == 1
        await asyncio.sleep(5.05)
    lease = await control.claim(run_id, "suspension-recovery")
    recovered = await repo.prepare_recovery(lease)
    scope = await factory.create_rehydrated_run_scope(recovered, lease=lease)
    try:
        result = await scope.execute()
    finally:
        await scope.close(abort=True)
    image = await repo.load(run_id)
    assert image.loop_iterations[used - 1].decision_id == accepted_id
    assert image.loop_iterations[used - 1].action_id == action_id
    if scenario == "unknown_cancel":
        assert result.status.value == "CANCELLED", result.error_code
        assert image.tool_invocations[0].state == "UNKNOWN" and len(tool.calls) == 1
        assert image.root.budget_consumed["model_calls"] == used
    else:
        assert result.status.value == "SUCCEEDED", result.error_code
        assert image.root.budget_consumed["model_calls"] == used + 1
        assert len(tool.calls) == (2 if scenario == "bounded_polling" else 1)
        if scenario == "approval":
            assert len(image.approvals) == len(image.execution_claims) == 1
