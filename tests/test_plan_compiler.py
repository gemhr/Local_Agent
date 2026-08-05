from datetime import UTC, datetime

import pytest

from core.runtime.agent_registry import DEFAULT_AGENT_REGISTRY
from core.runtime.invocation_bindings import AgentInvocationSpec, StepInvocationBindings
from core.runtime.multi_agent_planning import (
    DelegatedPlanDecision,
    DelegatedTaskDecision,
    DirectAnswerDecision,
    PlanningSource,
    ResolvedPlan,
)
from core.runtime.plan_compiler import (
    PlanCompileConfig,
    PlanCompileError,
    PlanCompileErrorCode,
    PlanCompiler,
)
from core.runtime.planning import (
    ExecutionKind,
    OutputPolicy,
    Plan,
    PlanSource,
    PlanStep,
    TaskCapabilityRequirements,
)


RAW = "summarize C:/private/cdt_field_mapping.md"


def compiler(config: PlanCompileConfig | None = None) -> PlanCompiler:
    return PlanCompiler(DEFAULT_AGENT_REGISTRY, config)


def task(task_id: str, agent_id: str, instruction: str = RAW, **kwargs) -> DelegatedTaskDecision:
    return DelegatedTaskDecision(task_id, agent_id, instruction, **kwargs)


def assert_binding_parity(resolved: ResolvedPlan) -> None:
    assert set(resolved.invocation_bindings.step_ids) == {
        step.step_id for step in resolved.plan.steps
    }
    for step in resolved.plan.steps:
        resolved.invocation_bindings.resolve_for_step(
            step.step_id, expected_agent_id=step.preferred_agent
        )
    assert RAW not in repr(resolved.plan)
    assert RAW not in repr(resolved)


def test_shape_0_core_direct() -> None:
    resolved = compiler().compile(
        DirectAnswerDecision("core_router", "MODEL_DIRECT"),
        planning_source=PlanningSource.MODEL,
        direct_instruction=RAW,
    )
    assert [(step.step_id, step.preferred_agent, step.depends_on, step.execution_kind, step.output_policy) for step in resolved.plan.steps] == [
        ("answer", "core_router", (), ExecutionKind.AGENT, OutputPolicy.FINAL_PASSTHROUGH)
    ]
    assert_binding_parity(resolved)


@pytest.mark.parametrize("agent_id", ["knowledge_expert", "code_expert", "data_analyst"])
def test_shape_1_authorized_explicit_entry_specialist(agent_id: str) -> None:
    resolved = compiler().compile(
        DirectAnswerDecision(agent_id, "EXPLICIT_ENTRY_SELECTION"),
        planning_source=PlanningSource.EXPLICIT_ENTRY,
        direct_instruction=RAW,
    )
    step = resolved.plan.steps[0]
    assert (step.step_id, step.preferred_agent, step.depends_on) == ("answer", agent_id, ())
    assert step.output_policy is OutputPolicy.FINAL_PASSTHROUGH
    assert_binding_parity(resolved)


def test_shape_1_single_delegated_knowledge_direct() -> None:
    resolved = compiler().compile(
        DelegatedPlanDecision((task("knowledge", "knowledge_expert"),), False),
        planning_source=PlanningSource.DETERMINISTIC_RULE,
    )
    step = resolved.plan.steps[0]
    assert (step.step_id, step.preferred_agent, step.depends_on) == (
        "task-knowledge", "knowledge_expert", ()
    )
    assert step.output_policy is OutputPolicy.FINAL_PASSTHROUGH
    assert_binding_parity(resolved)


@pytest.mark.parametrize("agent_id", ["code_expert", "data_analyst"])
def test_shape_2_single_specialist_and_synthesis(agent_id: str) -> None:
    resolved = compiler().compile(
        DelegatedPlanDecision((task("specialist", agent_id),), True),
        planning_source=PlanningSource.MODEL,
    )
    specialist, synthesis = resolved.plan.steps
    assert specialist.output_policy is OutputPolicy.INTERNAL
    assert specialist.depends_on == ()
    assert synthesis.preferred_agent == "synthesis_agent"
    assert synthesis.execution_kind is ExecutionKind.SYNTHESIS
    assert synthesis.output_policy is OutputPolicy.FINAL_SYNTHESIS
    assert synthesis.depends_on == (specialist.step_id,)
    assert_binding_parity(resolved)


def test_shape_3_fanout_is_stably_sorted_and_has_one_final_source() -> None:
    decision = DelegatedPlanDecision(
        (task("knowledge", "knowledge_expert"), task("code", "code_expert")),
        True,
    )
    resolved = compiler().compile(decision, planning_source=PlanningSource.MODEL)
    assert tuple(step.step_id for step in resolved.plan.steps) == (
        "task-code", "task-knowledge", "synthesis"
    )
    assert resolved.plan.steps[-1].depends_on == ("task-code", "task-knowledge")
    assert sum(step.output_policy is not OutputPolicy.INTERNAL for step in resolved.plan.steps) == 1
    assert all(not step.depends_on for step in resolved.plan.steps[:-1])
    assert_binding_parity(resolved)


def test_plan_and_step_ids_are_stable_without_instruction_digest() -> None:
    first = compiler().compile(
        DelegatedPlanDecision((task("code", "code_expert", "first secret"),), True),
        planning_source=PlanningSource.MODEL,
    )
    second = compiler().compile(
        DelegatedPlanDecision((task("code", "code_expert", "different secret"),), True),
        planning_source=PlanningSource.MODEL,
    )
    assert first.plan.plan_id == second.plan.plan_id
    assert tuple(step.step_id for step in first.plan.steps) == tuple(step.step_id for step in second.plan.steps)
    assert not hasattr(first.plan.steps[0], "instruction")
    assert not hasattr(first.plan.steps[0], "input_digest")


@pytest.mark.parametrize(
    "decision,source,error_code",
    [
        (DirectAnswerDecision("unknown_agent", "TEST"), PlanningSource.EXPLICIT_ENTRY, PlanCompileErrorCode.UNKNOWN_AGENT),
        (DirectAnswerDecision("synthesis_agent", "TEST"), PlanningSource.EXPLICIT_ENTRY, PlanCompileErrorCode.SYNTHESIS_ENTRY_FORBIDDEN),
        (DirectAnswerDecision("code_expert", "TEST"), PlanningSource.MODEL, PlanCompileErrorCode.MODEL_DIRECT_AGENT_NOT_ALLOWED),
        (DelegatedPlanDecision((), True), PlanningSource.MODEL, PlanCompileErrorCode.EMPTY_TASKS),
        (DelegatedPlanDecision((task("same", "code_expert"), task("same", "knowledge_expert")), True), PlanningSource.MODEL, PlanCompileErrorCode.DUPLICATE_TASK_ID),
        (DelegatedPlanDecision((task("INVALID", "code_expert"),), True), PlanningSource.MODEL, PlanCompileErrorCode.INVALID_TASK_ID),
        (DelegatedPlanDecision((task("a" * 65, "code_expert"),), True), PlanningSource.MODEL, PlanCompileErrorCode.INVALID_TASK_ID),
        (DelegatedPlanDecision((task("core", "core_router"),), True), PlanningSource.MODEL, PlanCompileErrorCode.DELEGATED_AGENT_NOT_ALLOWED),
        (DelegatedPlanDecision((task("synthesis", "synthesis_agent"),), True), PlanningSource.MODEL, PlanCompileErrorCode.INVALID_TASK_ID),
        (DelegatedPlanDecision((task("summary", "synthesis_agent"),), True), PlanningSource.MODEL, PlanCompileErrorCode.DELEGATED_AGENT_NOT_ALLOWED),
        (DelegatedPlanDecision((task("code", "code_expert"),), False), PlanningSource.MODEL, PlanCompileErrorCode.DIRECT_DELEGATION_NOT_ALLOWED),
        (DelegatedPlanDecision((task("code", "code_expert"), task("knowledge", "knowledge_expert")), False), PlanningSource.MODEL, PlanCompileErrorCode.INVALID_GRAPH_SHAPE),
        (DelegatedPlanDecision((task("code", "code_expert", required_capabilities=frozenset({"rag"})),), True), PlanningSource.MODEL, PlanCompileErrorCode.INVALID_CAPABILITY),
        (DelegatedPlanDecision((task("code", "code_expert", input_type="image"),), True), PlanningSource.MODEL, PlanCompileErrorCode.INVALID_INPUT_TYPE),
    ],
)
def test_typed_decision_rejection_matrix(decision, source, error_code) -> None:
    with pytest.raises(PlanCompileError) as captured:
        compiler().compile(
            decision,
            planning_source=source,
            direct_instruction=RAW if isinstance(decision, DirectAnswerDecision) else None,
        )
    assert captured.value.error_code is error_code
    assert RAW not in str(captured.value)


def test_instruction_and_plan_limits_fail_without_echoing_content() -> None:
    long = "sensitive-" * 5
    limited = compiler(PlanCompileConfig(max_agents=2, max_steps=3, max_instruction_chars=10, max_total_instruction_chars=20))
    with pytest.raises(PlanCompileError) as single:
        limited.compile(
            DelegatedPlanDecision((task("code", "code_expert", long),), True),
            planning_source=PlanningSource.MODEL,
        )
    assert single.value.error_code is PlanCompileErrorCode.INSTRUCTION_LIMIT_EXCEEDED
    assert long not in str(single.value)
    total_limited = compiler(PlanCompileConfig(max_agents=2, max_steps=3, max_instruction_chars=6, max_total_instruction_chars=10))
    with pytest.raises(PlanCompileError) as total:
        total_limited.compile(
            DelegatedPlanDecision((task("a", "code_expert", "123456"), task("b", "knowledge_expert", "abcdef")), True),
            planning_source=PlanningSource.MODEL,
        )
    assert total.value.error_code is PlanCompileErrorCode.PLAN_INSTRUCTION_LIMIT_EXCEEDED


def test_agent_and_step_limits_are_hard() -> None:
    decision = DelegatedPlanDecision((task("a", "code_expert"), task("b", "knowledge_expert")), True)
    with pytest.raises(PlanCompileError) as agents:
        compiler(PlanCompileConfig(max_agents=1, max_steps=3)).compile(decision, planning_source=PlanningSource.MODEL)
    assert agents.value.error_code is PlanCompileErrorCode.PLAN_LIMIT_EXCEEDED
    with pytest.raises(PlanCompileError) as steps:
        compiler(PlanCompileConfig(max_agents=2, max_steps=2)).compile(decision, planning_source=PlanningSource.MODEL)
    assert steps.value.error_code is PlanCompileErrorCode.PLAN_LIMIT_EXCEEDED


def plan(*steps: PlanStep) -> Plan:
    return Plan("unsafe-candidate", 1, "safe", steps, datetime.now(UTC), PlanSource.DETERMINISTIC)


def step(
    step_id: str,
    agent: str = "code_expert",
    depends_on: tuple[str, ...] = (),
    kind: ExecutionKind = ExecutionKind.AGENT,
    policy: OutputPolicy = OutputPolicy.INTERNAL,
) -> PlanStep:
    return PlanStep(step_id, "title", "description", depends_on, "done", agent, TaskCapabilityRequirements(), kind, policy)


@pytest.mark.parametrize(
    "candidate,error_code",
    [
        (plan(step("a"), step("a")), PlanCompileErrorCode.DUPLICATE_STEP_ID),
        (plan(step("a", depends_on=("missing",))), PlanCompileErrorCode.MISSING_DEPENDENCY),
        (plan(step("a", depends_on=("a",))), PlanCompileErrorCode.SELF_DEPENDENCY),
        (plan(step("a", depends_on=("b",)), step("b", depends_on=("a",))), PlanCompileErrorCode.DEPENDENCY_CYCLE),
        (plan(step("a", policy=OutputPolicy.FINAL_PASSTHROUGH), step("b", policy=OutputPolicy.FINAL_PASSTHROUGH)), PlanCompileErrorCode.MULTIPLE_FINAL_STEPS),
        (plan(step("a")), PlanCompileErrorCode.NO_FINAL_STEP),
        (plan(step("a", policy=OutputPolicy.FINAL_PASSTHROUGH), step("b", depends_on=("a",), policy=OutputPolicy.FINAL_PASSTHROUGH)), PlanCompileErrorCode.MULTIPLE_FINAL_STEPS),
        (plan(step("a"), step("synthesis", "synthesis_agent", ("a",), ExecutionKind.SYNTHESIS, OutputPolicy.INTERNAL)), PlanCompileErrorCode.FINAL_POLICY_NOT_ALLOWED),
        (plan(step("a", "code_expert", policy=OutputPolicy.FINAL_SYNTHESIS)), PlanCompileErrorCode.FINAL_POLICY_NOT_ALLOWED),
        (plan(step("core", "core_router"), step("synthesis", "synthesis_agent", ("core",), ExecutionKind.SYNTHESIS, OutputPolicy.FINAL_SYNTHESIS)), PlanCompileErrorCode.DELEGATED_AGENT_NOT_ALLOWED),
    ],
)
def test_defensive_plan_graph_rejection_matrix(candidate: Plan, error_code: PlanCompileErrorCode) -> None:
    with pytest.raises(PlanCompileError) as captured:
        compiler().validate_plan(candidate)
    assert captured.value.error_code is error_code


def test_resolved_plan_rejects_missing_extra_and_agent_mismatch_bindings() -> None:
    candidate = plan(step("answer", "core_router", policy=OutputPolicy.FINAL_PASSTHROUGH))
    for bindings in (
        StepInvocationBindings((AgentInvocationSpec("extra", "core_router", RAW),)),
        StepInvocationBindings((AgentInvocationSpec("answer", "code_expert", RAW),)),
    ):
        with pytest.raises(ValueError):
            ResolvedPlan(candidate, bindings, PlanningSource.EXPLICIT_ENTRY)


def test_compile_failures_do_not_log_raw_instruction(caplog) -> None:
    with pytest.raises(PlanCompileError):
        compiler().compile(
            DelegatedPlanDecision((task("code", "code_expert", RAW),), False),
            planning_source=PlanningSource.MODEL,
        )
    assert RAW not in caplog.text
