from dataclasses import asdict
import json

import pytest

from core.runtime.agent_registry import AgentRegistryError, AgentRegistryErrorCode, DEFAULT_AGENT_REGISTRY
from core.runtime.context import RunContext
from core.runtime.multi_agent_planning import (
    DelegatedPlanDecision,
    DirectAnswerDecision,
    PlanResolver,
    PlanningError,
    PlanningErrorCode,
    PlanningRequest,
    PlanningSource,
    StrictPlanningDecisionParser,
)
from core.runtime.plan_compiler import PlanCompileError, PlanCompileErrorCode, PlanCompiler
from core.runtime.planning import OutputPolicy


RAW = "private model output C:/secret/file.md"


def direct_json(agent_id: str = "core_router", instruction: str = "answer safely") -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "decision": "DIRECT_ANSWER",
            "agent_id": agent_id,
            "instruction": instruction,
            "reason_code": "MODEL_DIRECT",
        }
    )


def delegate_json(tasks, synthesis_required: bool) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "decision": "DELEGATE",
            "tasks": tasks,
            "synthesis_required": synthesis_required,
        }
    )


class FakePlanningModel:
    def __init__(self, output: str | None = None, error: Exception | None = None) -> None:
        self.output = output
        self.error = error
        self.calls = 0

    async def generate_plan(self, request: PlanningRequest, run_context: RunContext) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.output is not None
        return self.output


def resolver(model: FakePlanningModel | None = None) -> PlanResolver:
    return PlanResolver(DEFAULT_AGENT_REGISTRY, PlanCompiler(DEFAULT_AGENT_REGISTRY), model)


def context() -> RunContext:
    return RunContext.create(entry_agent_id="core_router")


def test_strict_parser_accepts_direct_and_delegated_typed_schema() -> None:
    direct = StrictPlanningDecisionParser.parse(direct_json())
    assert isinstance(direct, DirectAnswerDecision)
    delegated = StrictPlanningDecisionParser.parse(
        delegate_json(
            [{"task_id": "code", "agent_id": "code_expert", "instruction": "inspect", "capabilities": ["code_reasoning"]}],
            True,
        )
    )
    assert isinstance(delegated, DelegatedPlanDecision)
    assert delegated.tasks[0].required_capabilities == frozenset({"code_reasoning"})


@pytest.mark.parametrize(
    "raw,error_code",
    [
        ("not-json " + RAW, PlanningErrorCode.PLANNER_SCHEMA_INVALID),
        (json.dumps(["not-object", RAW]), PlanningErrorCode.PLANNER_SCHEMA_INVALID),
        (json.dumps({"schema_version": 2, "decision": "DIRECT_ANSWER"}), PlanningErrorCode.PLANNER_SCHEMA_VERSION_UNSUPPORTED),
        (json.dumps({"schema_version": 1, "decision": "UNKNOWN", "raw": RAW}), PlanningErrorCode.PLANNER_DECISION_UNKNOWN),
        (json.dumps({"schema_version": 1, "decision": "DIRECT_ANSWER", "agent_id": "core_router", "instruction": RAW, "reason_code": "MODEL_DIRECT", "output_policy": "FINAL_PASSTHROUGH"}), PlanningErrorCode.PLANNER_FIELD_FORBIDDEN),
        (delegate_json([{"task_id": "code", "agent_id": "code_expert", "instruction": RAW, "optional_dependencies": []}], True), PlanningErrorCode.OPTIONAL_DEPENDENCY_UNSUPPORTED),
        (delegate_json([{"task_id": "code", "agent_id": "code_expert", "instruction": RAW, "depends_on": []}], True), PlanningErrorCode.PLANNER_FIELD_FORBIDDEN),
        (delegate_json([{"task_id": "code", "agent_id": "code_expert", "instruction": RAW, "output_type": "arbitrary"}], True), PlanningErrorCode.PLANNER_FIELD_FORBIDDEN),
        (delegate_json([{"task_id": "code", "agent_id": "code_expert", "instruction": ""}], True), PlanningErrorCode.PLANNER_SCHEMA_INVALID),
    ],
)
def test_strict_parser_rejects_schema_and_permission_fields_without_raw_echo(raw: str, error_code: PlanningErrorCode) -> None:
    with pytest.raises(PlanningError) as captured:
        StrictPlanningDecisionParser.parse(raw)
    assert captured.value.error_code is error_code
    assert RAW not in str(captured.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_id", ["knowledge_expert", "code_expert", "data_analyst"])
async def test_explicit_specialist_is_deterministic_and_never_calls_model(agent_id: str) -> None:
    model = FakePlanningModel(error=AssertionError("must not run"))
    resolved = await resolver(model).resolve(PlanningRequest(agent_id, RAW), context())
    assert model.calls == 0
    assert resolved.planning_source is PlanningSource.EXPLICIT_ENTRY
    assert resolved.plan.steps[0].preferred_agent == agent_id
    assert resolved.plan.steps[0].output_policy is OutputPolicy.FINAL_PASSTHROUGH


@pytest.mark.asyncio
async def test_unknown_and_synthesis_selected_agents_fail_without_model_or_fallback() -> None:
    model = FakePlanningModel(output=direct_json())
    for agent_id, code in (
        ("unknown", AgentRegistryErrorCode.UNKNOWN_AGENT),
        ("synthesis_agent", AgentRegistryErrorCode.ENTRY_AGENT_NOT_ALLOWED),
    ):
        with pytest.raises(AgentRegistryError) as captured:
            await resolver(model).resolve(PlanningRequest(agent_id, RAW), context())
        assert captured.value.error_code is code
    assert model.calls == 0


@pytest.mark.asyncio
async def test_core_deterministic_direct_knowledge_code_and_fanout_rules() -> None:
    model = FakePlanningModel(error=AssertionError("must not run"))
    direct = await resolver(model).resolve(PlanningRequest("core_router", "你好"), context())
    assert direct.plan.steps[0].preferred_agent == "core_router"
    knowledge = await resolver(model).resolve(
        PlanningRequest("core_router", "调用知识专家总结 cdt_field_mapping.md"), context()
    )
    assert len(knowledge.plan.steps) == 1
    assert knowledge.plan.steps[0].preferred_agent == "knowledge_expert"
    document = await resolver(model).resolve(
        PlanningRequest("core_router", "讲讲 cdt_field_mapping.md"), context()
    )
    assert document.plan.steps[0].preferred_agent == "knowledge_expert"
    code = await resolver(model).resolve(
        PlanningRequest("core_router", "调用代码专家检查实现"), context()
    )
    assert tuple(step.preferred_agent for step in code.plan.steps) == ("code_expert", "synthesis_agent")
    fanout = await resolver(model).resolve(
        PlanningRequest("core_router", "调用知识专家和代码专家核验方案"), context()
    )
    assert tuple(step.preferred_agent for step in fanout.plan.steps) == (
        "code_expert", "knowledge_expert", "synthesis_agent"
    )
    assert model.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output,expected_agents",
    [
        (direct_json(), ("core_router",)),
        (delegate_json([{"task_id": "knowledge", "agent_id": "knowledge_expert", "instruction": "find source"}], False), ("knowledge_expert",)),
        (delegate_json([{"task_id": "code", "agent_id": "code_expert", "instruction": "inspect"}], True), ("code_expert", "synthesis_agent")),
        (delegate_json([{"task_id": "knowledge", "agent_id": "knowledge_expert", "instruction": "find"}, {"task_id": "code", "agent_id": "code_expert", "instruction": "inspect"}], True), ("code_expert", "knowledge_expert", "synthesis_agent")),
    ],
)
async def test_unresolved_core_request_calls_model_once_and_compiles_typed_output(output: str, expected_agents: tuple[str, ...]) -> None:
    model = FakePlanningModel(output=output)
    resolved = await resolver(model).resolve(
        PlanningRequest("core_router", "这是一个无法确定路由的复杂请求"), context()
    )
    assert model.calls == 1
    assert resolved.planning_source is PlanningSource.MODEL
    assert tuple(step.preferred_agent for step in resolved.plan.steps) == expected_agents


@pytest.mark.asyncio
async def test_schema_compile_and_model_failures_never_fallback_to_core() -> None:
    cases = (
        (FakePlanningModel(output="invalid " + RAW), PlanningError, PlanningErrorCode.PLANNER_SCHEMA_INVALID),
        (FakePlanningModel(output=direct_json("code_expert")), PlanCompileError, PlanCompileErrorCode.MODEL_DIRECT_AGENT_NOT_ALLOWED),
        (FakePlanningModel(output=delegate_json([{"task_id": "unknown", "agent_id": "unknown_agent", "instruction": RAW}], True)), PlanCompileError, PlanCompileErrorCode.UNKNOWN_AGENT),
        (FakePlanningModel(error=RuntimeError(RAW)), PlanningError, PlanningErrorCode.PLANNING_MODEL_FAILED),
    )
    for model, error_type, error_code in cases:
        with pytest.raises(error_type) as captured:
            await resolver(model).resolve(
                PlanningRequest("core_router", "未决请求"), context()
            )
        assert captured.value.error_code is error_code
        assert RAW not in str(captured.value)
        assert model.calls == 1


def test_sensitive_planning_objects_are_immutable_repr_and_asdict_safe() -> None:
    request = PlanningRequest("core_router", RAW)
    decision = DirectAnswerDecision("core_router", RAW, "TEST")
    delegated = StrictPlanningDecisionParser.parse(
        delegate_json([{"task_id": "code", "agent_id": "code_expert", "instruction": RAW}], True)
    )
    assert RAW not in repr(request)
    assert RAW not in repr(decision)
    assert RAW not in repr(delegated)
    resolved = PlanCompiler(DEFAULT_AGENT_REGISTRY).compile(
        decision, planning_source=PlanningSource.MODEL
    )
    assert "invocation_bindings" not in repr(resolved)
    with pytest.raises(AttributeError):
        request.user_request = "changed"
    with pytest.raises(TypeError):
        asdict(request)
    with pytest.raises(TypeError):
        asdict(decision)
