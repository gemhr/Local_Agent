"""SynthesisAgentAdapter contract: whitelist input, strict prompt, fail-closed."""

from __future__ import annotations

import pytest

from core.runtime import (
    AgentExecutionRequest,
    ContextSourceType,
    ContextTrustLevel,
    DependencyResultEntry,
    DependencyResultView,
    ExecutionKind,
    ResultContentType,
    SynthesisAgentAdapter,
    SynthesisInputError,
    SynthesisInputErrorCode,
    TaskCapabilityRequirements,
)


class _RecordingRouter:
    def __init__(self, output: str = "final candidate") -> None:
        self.output = output
        self.calls: list[tuple[str, str, dict]] = []
        self.fail = False

    def complete_context_items(self, agent_id: str, items, **kwargs) -> str:
        self.calls.append((agent_id, items, kwargs))
        if self.fail:
            raise RuntimeError("simulated synthesis failure")
        return self.output


def _view(*entries):
    return DependencyResultView(entries)


def _entry(step_id: str, agent_id: str, content: str, *, complete: bool = True):
    return DependencyResultEntry(
        step_id,
        agent_id,
        ResultContentType.TEXT,
        content,
        complete,
    )


def _request(router, view):
    return AgentExecutionRequest(
        step_id="synthesis",
        agent_id="synthesis_agent",
        instruction="Synthesize all explicitly required specialist results.",
        execution_kind=ExecutionKind.SYNTHESIS,
        input_type="text",
        capability_requirements=TaskCapabilityRequirements(),
        dependency_results=view,
    )


def test_model_call_equals_one_when_all_results_present() -> None:
    router = _RecordingRouter()
    adapter = SynthesisAgentAdapter(router)
    view = _view(
        _entry("task-code", "code_expert", "code finding"),
        _entry("task-knowledge", "knowledge_expert", "knowledge finding"),
    )
    result = adapter.execute(_request(router, view), object())
    assert len(router.calls) == 1
    assert result.content == "final candidate"
    assert result.complete is True


def test_prompt_contains_provided_results_in_stable_order() -> None:
    router = _RecordingRouter()
    adapter = SynthesisAgentAdapter(router)
    view = _view(
        _entry("task-code", "code_expert", "CODE_BODY"),
        _entry("task-knowledge", "knowledge_expert", "KNOWLEDGE_BODY"),
    )
    adapter.execute(_request(router, view), object())
    items = router.calls[0][1]
    results = [
        item for item in items
        if item.source_type is ContextSourceType.STEP_RESULT
    ]
    assert [item.content for item in results] == ["CODE_BODY", "KNOWLEDGE_BODY"]
    assert [item.source_ref for item in results] == ["code_expert", "knowledge_expert"]
    assert all(item.trust_level is ContextTrustLevel.USER_CONTENT for item in results)
    assert items[0].source_type is ContextSourceType.SYSTEM_INSTRUCTION
    assert items[0].trust_level is ContextTrustLevel.TRUSTED_INSTRUCTION


def test_prompt_never_contains_unlisted_agents() -> None:
    router = _RecordingRouter()
    adapter = SynthesisAgentAdapter(router)
    view = _view(_entry("task-code", "code_expert", "CODE_BODY"))
    adapter.execute(_request(router, view), object())
    items = router.calls[0][1]
    rendered = "\n".join(item.content + item.source_ref for item in items)
    assert "data_analyst" not in rendered
    assert "core_router" not in rendered


def test_missing_view_means_zero_model_calls() -> None:
    router = _RecordingRouter()
    adapter = SynthesisAgentAdapter(router)
    request = AgentExecutionRequest(
        step_id="synthesis",
        agent_id="synthesis_agent",
        instruction="synthesize",
        execution_kind=ExecutionKind.SYNTHESIS,
        input_type="text",
        capability_requirements=TaskCapabilityRequirements(),
        dependency_results=None,
    )
    with pytest.raises(SynthesisInputError) as exc_info:
        adapter.execute(request, object())
    assert (
        exc_info.value.error_code
        is SynthesisInputErrorCode.MISSING_DEPENDENCIES
    )
    assert router.calls == []


def test_incomplete_required_result_means_zero_model_calls() -> None:
    router = _RecordingRouter()
    adapter = SynthesisAgentAdapter(router)
    view = _view(
        _entry("task-code", "code_expert", "partial", complete=False)
    )
    with pytest.raises(SynthesisInputError) as exc_info:
        adapter.execute(_request(router, view), object())
    assert exc_info.value.error_code is SynthesisInputErrorCode.INCOMPLETE_RESULT
    assert router.calls == []


def test_empty_view_means_zero_model_calls() -> None:
    router = _RecordingRouter()
    adapter = SynthesisAgentAdapter(router)
    with pytest.raises(SynthesisInputError) as exc_info:
        adapter.execute(_request(router, _view()), object())
    assert (
        exc_info.value.error_code
        is SynthesisInputErrorCode.MISSING_DEPENDENCIES
    )
    assert router.calls == []


def test_synthesis_failure_has_no_fallback_or_second_call() -> None:
    router = _RecordingRouter()
    router.fail = True
    adapter = SynthesisAgentAdapter(router)
    view = _view(_entry("task-code", "code_expert", "code finding"))
    with pytest.raises(SynthesisInputError) as exc_info:
        adapter.execute(_request(router, view), object())
    assert (
        exc_info.value.error_code
        is SynthesisInputErrorCode.SYNTHESIS_MODEL_FAILED
    )
    assert len(router.calls) == 1


def test_synthesis_call_uses_role_aware_context_and_no_memory() -> None:
    router = _RecordingRouter()
    adapter = SynthesisAgentAdapter(router)
    view = _view(_entry("task-code", "code_expert", "code finding"))
    adapter.execute(_request(router, view), object())
    kwargs = router.calls[0][2]
    assert "persist" not in kwargs
    assert "history_policy" not in kwargs
    assert kwargs["user_query"].startswith("Synthesize")
    assert not hasattr(adapter, "memory_manager")
    assert not hasattr(adapter, "journal")
    assert not hasattr(adapter, "store")


def test_synthesis_adapter_has_no_full_store_interface() -> None:
    adapter = SynthesisAgentAdapter(_RecordingRouter())
    for forbidden in ("get_all", "dependency_view_for", "write_prepared"):
        assert not hasattr(adapter, forbidden)


def test_raw_content_not_in_exception() -> None:
    secret = "SECRET_SPECIALIST_RESULT_DO_NOT_PERSIST"
    router = _RecordingRouter()
    router.fail = True
    adapter = SynthesisAgentAdapter(router)
    view = _view(_entry("task-code", "code_expert", secret))
    try:
        adapter.execute(_request(router, view), object())
    except SynthesisInputError as exc:
        rendered = f"{exc!r} {str(exc)}"
        assert secret not in rendered
    else:
        pytest.fail("expected SynthesisInputError")


@pytest.mark.asyncio
@pytest.mark.parametrize(("enabled", "output_limit", "investigation_output", "expected_calls", "mode"), [
    (False, 320, 128, 2, "legacy"), (True, 320, 128, 3, "protected"),
    (True, 319, 128, 1, "blocked"), (True, None, 128, 3, "unlimited"),
    (True, 320, 200, 2, "overrun"), (True, 320, 128, 2, "cancelled"),
    (True, 320, 128, 2, "direct"),
])
async def test_finite_dag_delivers_protected_synthesis(
        clean_database, tmp_path, monkeypatch, enabled, output_limit, investigation_output, expected_calls, mode):
    import json
    from hashlib import sha256
    from core.runtime.budget import BudgetUsage, RunBudget
    from core.runtime.events import RuntimeEventType
    from core.runtime.model_invocation import ModelAdapterResponse
    from core.runtime.runtime_factory import CoordinatedRuntimeFactory
    from tests.test_agent_batch_handoff import Provider
    from tests.test_agent_loop_runtime import assembly
    from tests.test_dynamic_planning_lifecycle import delegated_json, direct_json

    investigation_usage = BudgetUsage(input_tokens=400, output_tokens=investigation_output,
                                      total_tokens=400 + investigation_output)
    synthesis_usage = BudgetUsage(input_tokens=400, output_tokens=20, total_tokens=420)
    plan = {**json.loads(delegated_json(multi_step=False)), "synthesis_required": True}
    output_caps = []

    class RecordingProvider(Provider):
        def invoke(self, messages, *, max_tokens):
            output_caps.append(max_tokens)
            return super().invoke(messages, max_tokens=max_tokens)

    model = RecordingProvider([
        ModelAdapterResponse(direct_json() if mode == "direct" else json.dumps(plan)),
        ModelAdapterResponse("required evidence", actual_usage=investigation_usage),
        ModelAdapterResponse("protected final synthesis", actual_usage=synthesis_usage),
    ])
    initial, repo, _, _ = await assembly(clean_database, tmp_path, [], context_model=model)
    factory = CoordinatedRuntimeFactory(initial._router, initial._services,
        agent_registry=initial._agent_registry, execution_repository=repo, shared_run_budget=enabled)
    scope = await factory.create_run_scope("core_router", "coordinate a professional review",
        persist=False, budget=(RunBudget(max_total_tokens=4096, max_output_tokens=output_limit)
                               if output_limit else RunBudget(max_model_calls=4)))
    if mode == "cancelled":
        start_model = repo.start_model_attempt

        async def cancel_before_synthesis(lease, **kwargs):
            if kwargs["step_id"] == "synthesis":
                await repo.run_control.request_cancel(lease.run_id, reason="REQUEST_CANCELLED")
            return await start_model(lease, **kwargs)

        monkeypatch.setattr(repo, "start_model_attempt", cancel_before_synthesis)
    try:
        result = await scope.execute()
        image = await repo.load(scope.run_id)
        assert len(model.requests) == expected_calls, (result.error_code, [(s.step_id, s.safe_error) for s in image.steps])
        records = await factory.services.event_journal.read_after(scope.run_id, 0, 1000)
        output = [r for r in records if r.event_type is RuntimeEventType.OUTPUT_DELTA]
        assert scope.run_context.budget_ledger.synthesis_budget is None
        if mode == "direct":
            assert result.status.value == "SUCCEEDED" and len(output) == 1
            assert "synthesis_budget" not in image.root.resume_input
            return
        if expected_calls != 3:
            assert result.status.value == ("CANCELLED" if mode == "cancelled" else "FAILED")
            assert not output
            assert not any(image.root.budget_reserved.values())
            if mode == "overrun":
                assert image.root.budget_consumed["output_tokens"] == 328
            return
        assert result.status.value == "SUCCEEDED", (result.error_code, result.safe_message,
            [(s.step_id, s.status, s.safe_error, s.typed_result_payload) for s in image.steps],
            [(m.step_id, m.state, m.safe_error) for m in image.models], len(model.requests))
        delivered, = output
        assert delivered.safe_payload["text_digest"] == sha256(b"protected final synthesis").hexdigest()
        if output_limit is None:
            assert "synthesis_budget" not in image.root.resume_input
            return
        guard = image.root.resume_input["synthesis_budget"]
        assert guard["output_cap"] == 64 and guard["headroom"]["total_tokens"] > 64
        assert output_caps == [128, 128, 64]
        operations = image.root.budget_operations
        assert len(operations) == 2
        assert sorted(op["actual"]["total_tokens"] for op in operations.values()) == [420, 528]
        assert all(op["actual"]["model_calls"] == 1 for op in operations.values())
        assert not any(image.root.budget_reserved.values())
        final = next(step for step in image.steps if step.step_id == guard["step_id"])
        assert final.typed_result_payload["content"] == "protected final synthesis"
    finally:
        await scope.close(abort=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [True, False], ids=["winner", "abandoned"])
async def test_synthesis_headroom_is_atomic_and_recovery_preserves_durable_winner(
        clean_database, tmp_path, committed):
    import asyncio
    from datetime import UTC, datetime
    from uuid import uuid4
    from core.persistence.repositories.execution import DurableExecutionRepository
    from core.persistence.repositories.memory_lifecycle import MemoryLifecycleRepository
    from core.runtime.execution_aggregate import ExecutionRootInput, ModelAttemptState
    from core.runtime.events import RuntimeEventType
    from core.runtime.model_invocation import ModelAdapterResponse
    from core.runtime.planning import OutputPolicy, Plan, PlanSource, PlanStep
    from core.runtime.run_control import DurableRunControlService, OwnershipLost
    from tests.test_agent_batch_handoff import Provider
    from tests.test_agent_loop_runtime import assembly

    run_id = str(uuid4())
    control = DurableRunControlService(clean_database, lease_seconds=30)
    repo = DurableExecutionRepository(clean_database, control)
    provider = Provider([ModelAdapterResponse("recovered synthesis")])
    factory, _, _, _ = await assembly(clean_database, tmp_path, [], context_model=provider,
        repository=repo, control=control)
    registry = factory._agent_registry
    registration = registry.resolve("core_router")
    performers = {name: registry.resolve(name) for name in ("core_router", "code_expert", "synthesis_agent")}
    lease = await control.claim(run_id, "synthesis-first")
    await MemoryLifecycleRepository(clean_database).bind_run(lease, None, [], [])
    requirements = TaskCapabilityRequirements()
    steps = tuple(PlanStep(name, name, name, (), "provide evidence", "code_expert",
        requirements, output_policy=OutputPolicy.INTERNAL) for name in ("left", "right"))
    final = PlanStep("synthesis", "synthesis", "synthesize", ("left", "right"),
        "deliver evidence", "synthesis_agent", requirements, ExecutionKind.SYNTHESIS, OutputPolicy.FINAL_SYNTHESIS)
    plan = Plan(run_id, 1, "budget race", (*steps, final), datetime.now(UTC), PlanSource.DETERMINISTIC)
    await repo.initialize(ExecutionRootInput(run_id, {
        "synthesis_budget": {"step_id": "synthesis", "output_cap": 128, "headroom": {"total_tokens": 1000}},
        "entry_agent_id": "core_router", "resolved_agent_id": "core_router",
        "agent_version": registration.definition.agent_version, "toolset_identity": registration.toolset_identity,
        "resolved_model_profile_id": registration.definition.model_profile_id,
        "resolved_retrieval_profile_id": registration.definition.retrieval_profile_id or "NONE",
        "resolved_memory_profile_id": registration.definition.memory_profile_id or "NONE",
        "performer_versions": {name: reg.definition.agent_version for name, reg in performers.items()},
        "performer_identities": {name: reg.binding_identity() for name, reg in performers.items()},
        "user_query": "combine the committed evidence",
    }, plan, None, {"max_total_tokens": 11000}, {}, {}), lease=lease)
    for step in steps:
        assert await repo.start_step(lease, step_id=step.step_id, plan_version=1) == 1

    def attempt(step_id):
        return dict(step_id=step_id, attempt=1, model_attempt=1,
                    operation_id=f"model:{step_id}:1:1")

    async def reserve(step):
        return await repo.start_model_attempt(lease, **attempt(step.step_id), request_digest="0" * 64,
            provider_kind="synthetic", profile_identity="shared", reserved_usage={"total_tokens": 5200})

    outcomes = await asyncio.gather(*(reserve(step) for step in steps), return_exceptions=True)
    assert outcomes.count(None) == 1, outcomes
    winner, = [step for step, outcome in zip(steps, outcomes, strict=True) if outcome is None]
    failure, = [outcome for outcome in outcomes if outcome is not None]
    assert "LOOP_BUDGET_EXHAUSTED:total_tokens" in str(failure)
    if committed:
        await repo.finish_model_attempt(lease, **attempt(winner.step_id), state=ModelAttemptState.COMPLETED,
            result={"output": "winner"}, actual_usage={"total_tokens": 5300})
        await repo.complete_step(lease, step_id=winner.step_id, plan_version=1, attempt=1,
            result={"content": "winner", "complete": True})
    await control.release(lease)
    resumed = await control.claim(run_id, "synthesis-second")
    try:
        image = await repo.prepare_recovery(resumed)
        again = await repo.prepare_recovery(resumed)
        assert again.root.resume_input == image.root.resume_input
        assert again.root.budget_operations == image.root.budget_operations
        assert again.root.budget_consumed["total_tokens"] == (5300 if committed else 5200)
        assert not any(again.root.budget_reserved.values())
        row, = again.models
        with pytest.raises(OwnershipLost):
            await repo.finish_model_attempt(lease, **attempt(winner.step_id),
                state=ModelAttemptState.COMPLETED, result={"output": "stale"}, actual_usage={"total_tokens": 5300})
        winner_status = next(s for s in again.steps if s.step_id == winner.step_id).status
        if committed:
            assert row.state == "COMPLETED" and row.result_binding["output"] == "winner"
            assert winner_status == "SUCCEEDED"
        else:
            assert row.state == "UNKNOWN" and row.result_binding is None and winner_status == "BLOCKED"
            assert again.root.budget_operations[attempt(winner.step_id)["operation_id"]]["source"] == "RECOVERY_CONSERVATIVE"
        other, = [step for step in steps if step != winner]
        number = await repo.start_step(resumed, step_id=other.step_id, plan_version=1)
        await repo.complete_step(resumed, step_id=other.step_id, plan_version=1, attempt=number,
            result={"content": "other evidence", "complete": True})
        ready = await repo.load(run_id)
        scope = await factory.create_rehydrated_run_scope(ready, lease=resumed, persist=False)
        try:
            result = await scope.execute()
            assert result.status.value == ("SUCCEEDED" if committed else "FAILED"), result.error_code
        finally:
            await scope.close(abort=True)
        delivered = await repo.load(run_id)
        assert len(provider.requests) == int(committed) and len(delivered.models) == 1 + int(committed)
        assert delivered.root.budget_operations[attempt(winner.step_id)["operation_id"]] == (
            image.root.budget_operations[attempt(winner.step_id)["operation_id"]])
        records = await factory.services.event_journal.read_after(run_id, 0, 1000)
        assert sum(record.event_type is RuntimeEventType.OUTPUT_DELTA for record in records) == int(committed)
    finally:
        await control.release(resumed)
