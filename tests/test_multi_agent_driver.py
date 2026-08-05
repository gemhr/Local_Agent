"""MultiAgentDriver contract: real claim->plan->binding->registry->adapter."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.runtime import (
    AgentAdapterFactory,
    AgentAdapterResult,
    AgentExecutionRequest,
    AgentRouterSingleAgentAdapter,
    DelegatedPlanDecision,
    DelegatedTaskDecision,
    MultiAgentDriver,
    MultiAgentDriverError,
    MultiAgentDriverErrorCode,
    PlanCompiler,
    PlanSource,
    PlanningSource,
    ResultContentType,
    StepClaim,
    StepInvocationBindings,
    StepResult,
    TaskCapabilityRequirements,
)
from core.runtime.agent_registry import DEFAULT_AGENT_REGISTRY
from core.runtime.cancellation import CancellationReason, CancellationSource
from core.runtime.invocation_bindings import AgentInvocationSpec
from core.runtime.planning import ExecutionKind, Plan, PlanStep, OutputPolicy


class _RecordingAdapter:
    def __init__(self, label: str) -> None:
        self.label = label
        self.requests: list[AgentExecutionRequest] = []

    def execute(self, request, run_context):
        self.requests.append(request)
        return AgentAdapterResult(request.content_type, f"{self.label}:ok")


class _StubCoordinator:
    def __init__(
        self,
        *,
        plan,
        bindings,
        state,
        emitter=None,
        user_request=None,
        store=None,
    ) -> None:
        self.plan = plan
        self.invocation_bindings = bindings
        self.agent_state = state
        self.event_emitter = emitter
        self.user_request = user_request
        self.step_result_store = store


def build_shape2():
    decision = DelegatedPlanDecision(
        tasks=(
            DelegatedTaskDecision(
                "code",
                "code_expert",
                "Inspect the code contract.",
                required_capabilities=frozenset({"code_reasoning"}),
            ),
        ),
        synthesis_required=True,
    )
    return PlanCompiler(DEFAULT_AGENT_REGISTRY).compile(
        decision, planning_source=PlanningSource.MODEL
    )


def claim_for(plan, step_id: str, *, agent_id: str | None = None) -> StepClaim:
    step = next(item for item in plan.steps if item.step_id == step_id)
    return StepClaim(
        plan.plan_id,
        plan.version,
        step_id,
        datetime.now(UTC),
        step.capability_requirements,
        agent_id or step.preferred_agent,
    )


def _default_factory(adapters=None):
    if adapters is None:
        adapters = (
            ("core_router_adapter", _RecordingAdapter("core")),
            ("data_analyst_adapter", _RecordingAdapter("data")),
            ("code_expert_adapter", _RecordingAdapter("code")),
            ("knowledge_expert_adapter", _RecordingAdapter("knowledge")),
            ("synthesis_agent_adapter", _RecordingAdapter("synthesis")),
        )
    return AgentAdapterFactory(DEFAULT_AGENT_REGISTRY, adapters)


def make_driver(coordinator, *, adapters=None, registry=None):
    factory = _default_factory(adapters)
    return MultiAgentDriver(
        router=object(),
        coordinator=coordinator,
        adapter_factory=factory,
        registry=registry or DEFAULT_AGENT_REGISTRY,
    )


def test_claim_resolves_plan_binding_registry_adapter() -> None:
    resolved = build_shape2()
    coordinator = _StubCoordinator(
        plan=resolved.plan,
        bindings=resolved.invocation_bindings,
        state=object(),
    )
    code_adapter = _RecordingAdapter("code")
    synthesis_adapter = _RecordingAdapter("synthesis")
    factory = AgentAdapterFactory(
        DEFAULT_AGENT_REGISTRY,
        (
            ("core_router_adapter", _RecordingAdapter("core")),
            ("data_analyst_adapter", _RecordingAdapter("data")),
            ("knowledge_expert_adapter", _RecordingAdapter("knowledge")),
            ("code_expert_adapter", code_adapter),
            ("synthesis_agent_adapter", synthesis_adapter),
        ),
    )
    driver = MultiAgentDriver(
        router=object(),
        coordinator=coordinator,
        adapter_factory=factory,
        registry=DEFAULT_AGENT_REGISTRY,
    )
    result = driver.execute(claim_for(resolved.plan, "task-code"), object())
    assert isinstance(result, StepResult)
    assert result.step_id == "task-code"
    assert result.producer_agent_id == "code_expert"
    assert result.content == "code:ok"
    assert len(code_adapter.requests) == 1
    request = code_adapter.requests[0]
    assert request.agent_id == "code_expert"
    assert request.instruction == "Inspect the code contract."
    assert request.execution_kind is ExecutionKind.AGENT
    assert request.dependency_results is None
    assert len(synthesis_adapter.requests) == 0


def test_synthesis_attaches_dependency_view() -> None:
    resolved = build_shape2()
    store = _StoreFake()
    coordinator = _StubCoordinator(
        plan=resolved.plan,
        bindings=resolved.invocation_bindings,
        state=object(),
        user_request="original user question",
        store=store,
    )
    adapter = _RecordingAdapter("synthesis")
    driver = make_driver(
        coordinator,
        adapters=(
            ("core_router_adapter", _RecordingAdapter("core")),
            ("data_analyst_adapter", _RecordingAdapter("data")),
            ("knowledge_expert_adapter", _RecordingAdapter("knowledge")),
            ("code_expert_adapter", _RecordingAdapter("code")),
            ("synthesis_agent_adapter", adapter),
        ),
    )
    driver.execute(claim_for(resolved.plan, "synthesis"), object())
    assert len(adapter.requests) == 1
    request = adapter.requests[0]
    assert request.execution_kind is ExecutionKind.SYNTHESIS
    assert request.dependency_results is not None
    assert "original user question" in request.instruction
    assert store.last_claim.step_id == "synthesis"


class _StoreFake:
    def __init__(self) -> None:
        self.last_claim = None
        self.views = 0

    def dependency_view_for(self, claim, state):
        self.last_claim = claim
        self.views += 1
        from core.runtime import DependencyResultEntry, DependencyResultView

        return DependencyResultView(
            (
                DependencyResultEntry(
                    "task-code", "code_expert", ResultContentType.TEXT, "ok", True
                ),
            )
        )


def test_binding_agent_mismatch_fails_closed() -> None:
    resolved = build_shape2()
    coordinator = _StubCoordinator(
        plan=resolved.plan,
        bindings=resolved.invocation_bindings,
        state=object(),
    )
    driver = make_driver(coordinator)
    with pytest.raises(MultiAgentDriverError) as exc_info:
        driver.execute(
            claim_for(resolved.plan, "task-code", agent_id="data_analyst"),
            object(),
        )
    assert exc_info.value.error_code is MultiAgentDriverErrorCode.BINDING_MISMATCH


def test_registry_plan_mismatch_fails_closed() -> None:
    capabilities = TaskCapabilityRequirements()
    plan = Plan(
        "custom-plan",
        1,
        "custom",
        (
            PlanStep(
                "task-ghost",
                "ghost",
                "g",
                (),
                "done",
                "ghost_agent",
                capabilities,
                ExecutionKind.AGENT,
                OutputPolicy.INTERNAL,
            ),
            PlanStep(
                "synthesis",
                "synthesis",
                "s",
                ("task-ghost",),
                "done",
                "synthesis_agent",
                capabilities,
                ExecutionKind.SYNTHESIS,
                OutputPolicy.FINAL_SYNTHESIS,
            ),
        ),
        datetime.now(UTC),
        PlanSource.DETERMINISTIC,
    )
    bindings = StepInvocationBindings(
        (
            AgentInvocationSpec("task-ghost", "ghost_agent", "inspect"),
            AgentInvocationSpec("synthesis", "synthesis_agent", "synthesize"),
        )
    )
    coordinator = _StubCoordinator(
        plan=plan,
        bindings=bindings,
        state=object(),
    )
    driver = make_driver(coordinator)
    with pytest.raises(MultiAgentDriverError) as exc_info:
        driver.execute(claim_for(plan, "task-ghost"), object())
    assert exc_info.value.error_code is MultiAgentDriverErrorCode.REGISTRY_MISMATCH


def test_specialist_call_uses_persist_false() -> None:
    resolved = build_shape2()
    coordinator = _StubCoordinator(
        plan=resolved.plan,
        bindings=resolved.invocation_bindings,
        state=object(),
    )
    calls: list[dict] = []

    class PersistRouter:
        def complete_single_agent(self, agent_id, query, **kwargs):
            calls.append(kwargs)
            return "out"

    factory = AgentAdapterFactory(
        DEFAULT_AGENT_REGISTRY,
        (
            ("core_router_adapter", AgentRouterSingleAgentAdapter(PersistRouter())),
            ("data_analyst_adapter", AgentRouterSingleAgentAdapter(PersistRouter())),
            ("knowledge_expert_adapter", AgentRouterSingleAgentAdapter(PersistRouter())),
            ("code_expert_adapter", AgentRouterSingleAgentAdapter(PersistRouter())),
            ("synthesis_agent_adapter", AgentRouterSingleAgentAdapter(PersistRouter())),
        ),
    )
    driver = MultiAgentDriver(
        router=PersistRouter(),
        coordinator=coordinator,
        adapter_factory=factory,
        registry=DEFAULT_AGENT_REGISTRY,
    )
    driver.execute(claim_for(resolved.plan, "task-code"), object())
    assert calls[0]["persist"] is False


def test_driver_has_no_store_or_gate_write_capability() -> None:
    resolved = build_shape2()
    coordinator = _StubCoordinator(
        plan=resolved.plan,
        bindings=resolved.invocation_bindings,
        state=object(),
    )
    driver = make_driver(coordinator)
    for forbidden in (
        "write_prepared",
        "mark_readable",
        "seal",
        "clear",
        "output_gate",
        "deliver",
    ):
        assert not hasattr(driver, forbidden)


def test_cancellation_propagates_to_specialist_call() -> None:
    resolved = build_shape2()
    coordinator = _StubCoordinator(
        plan=resolved.plan,
        bindings=resolved.invocation_bindings,
        state=object(),
    )
    source = CancellationSource()
    source.cancel(CancellationReason.REQUEST_CANCELLED)

    class CancellationAwareRouter:
        def complete_single_agent(self, agent_id, query, *, run_context, **kwargs):
            run_context.raise_if_inactive()
            return "out"

    factory = AgentAdapterFactory(
        DEFAULT_AGENT_REGISTRY,
        (
            ("core_router_adapter", AgentRouterSingleAgentAdapter(CancellationAwareRouter())),
            ("data_analyst_adapter", AgentRouterSingleAgentAdapter(CancellationAwareRouter())),
            ("knowledge_expert_adapter", AgentRouterSingleAgentAdapter(CancellationAwareRouter())),
            ("code_expert_adapter", AgentRouterSingleAgentAdapter(CancellationAwareRouter())),
            ("synthesis_agent_adapter", AgentRouterSingleAgentAdapter(CancellationAwareRouter())),
        ),
    )
    driver = MultiAgentDriver(
        router=CancellationAwareRouter(),
        coordinator=coordinator,
        adapter_factory=factory,
        registry=DEFAULT_AGENT_REGISTRY,
    )
    from core.runtime.cancellation import RunCancelledError

    context = _CancelledRunContext(source.token)
    with pytest.raises(RunCancelledError):
        driver.execute(claim_for(resolved.plan, "task-code"), context)


class _CancelledRunContext:
    def __init__(self, token) -> None:
        self.cancellation_token = token

    def raise_if_inactive(self):
        self.cancellation_token.raise_if_cancelled()


def test_result_conversion_preserves_claim_identity() -> None:
    resolved = build_shape2()
    coordinator = _StubCoordinator(
        plan=resolved.plan,
        bindings=resolved.invocation_bindings,
        state=object(),
    )
    adapter = _RecordingAdapter("code")
    driver = make_driver(
        coordinator,
        adapters=(
            ("core_router_adapter", _RecordingAdapter("core")),
            ("data_analyst_adapter", _RecordingAdapter("data")),
            ("knowledge_expert_adapter", _RecordingAdapter("knowledge")),
            ("code_expert_adapter", adapter),
            ("synthesis_agent_adapter", _RecordingAdapter("synthesis")),
        ),
    )
    result = driver.execute(claim_for(resolved.plan, "task-code"), object())
    assert isinstance(result, StepResult)
    assert result.producer_agent_id == "code_expert"
    assert result.content_type is ResultContentType.TEXT
    assert result.complete is True
