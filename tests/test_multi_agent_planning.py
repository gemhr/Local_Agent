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


def direct_json(agent_id: str = "core_router") -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "decision": "DIRECT_ANSWER",
            "agent_id": agent_id,
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

    async def generate_plan(
        self,
        request: PlanningRequest,
        run_context: RunContext,
        *,
        memory_context_bundle=None,
        memory_injection_report_out=None,
    ) -> str:
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
async def test_deterministic_data_query_routes_to_data_analyst_without_model() -> None:
    """“查数据库…csv/xlsx”类数据查询在解析层直接委派 data_analyst，
    不调用模型，避免模型发明未注册 capability。"""
    model = FakePlanningModel(error=AssertionError("must not run"))
    data_query = "查数据库，mock_test_results.csv这个表的第四列的表头是什么"
    resolved = await resolver(model).resolve(
        PlanningRequest("core_router", data_query), context()
    )
    assert tuple(step.preferred_agent for step in resolved.plan.steps) == (
        "data_analyst",
        "synthesis_agent",
    )
    assert resolved.planning_source is PlanningSource.DETERMINISTIC_RULE
    assert model.calls == 0

    xlsx = await resolver(model).resolve(
        PlanningRequest(
            "core_router", "查询 excel 表格 exports.xlsx 的前三行"
        ),
        context(),
    )
    assert tuple(step.preferred_agent for step in xlsx.plan.steps) == (
        "data_analyst",
        "synthesis_agent",
    )
    assert model.calls == 0

    # 无数据别名时，带 .csv/.xlsx 引用的 query 走既有文档检索 fallback
    # （knowledge_expert 单透传），仍是确定性路径，不调用模型。
    file_query = await resolver(model).resolve(
        PlanningRequest("core_router", "查询 exports.xlsx 的前三行"),
        context(),
    )
    assert tuple(
        step.preferred_agent for step in file_query.plan.steps
    ) == ("knowledge_expert",)
    assert file_query.planning_source is PlanningSource.DETERMINISTIC_RULE
    assert model.calls == 0


@pytest.mark.asyncio
async def test_non_data_queries_still_use_model_path() -> None:
    """防过度路由：无数据别名或无语义信号的 query 仍走模型。"""
    model = FakePlanningModel(output=direct_json())
    for query in (
        "查看这个代码仓库结构",
        "数据库索引原理是什么",
        "查一下天气",
    ):
        resolved = await resolver(model).resolve(
            PlanningRequest("core_router", query), context()
        )
        assert resolved.planning_source is PlanningSource.MODEL
        assert tuple(step.preferred_agent for step in resolved.plan.steps) == (
            "core_router",
        )
    assert model.calls == 3


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
async def test_model_direct_binding_uses_original_request_and_delegate_keeps_typed_instruction() -> None:
    original_request = "用户原始请求：不要由 Planner 改写"
    direct = await resolver(FakePlanningModel(output=direct_json())).resolve(
        PlanningRequest("core_router", original_request), context()
    )
    direct_binding = direct.invocation_bindings.resolve_for_step(
        "answer", expected_agent_id="core_router"
    )
    assert direct_binding.instruction == original_request

    specialist_instruction = "仅供知识专家执行的 typed task"
    delegated = await resolver(
        FakePlanningModel(
            output=delegate_json(
                [{"task_id": "knowledge", "agent_id": "knowledge_expert", "instruction": specialist_instruction}],
                False,
            )
        )
    ).resolve(PlanningRequest("core_router", original_request), context())
    delegated_binding = delegated.invocation_bindings.resolve_for_step(
        "task-knowledge", expected_agent_id="knowledge_expert"
    )
    assert delegated_binding.instruction == specialist_instruction


@pytest.mark.asyncio
async def test_model_direct_forged_instruction_is_forbidden_without_echo_or_log(caplog) -> None:
    forged = "PLANNER_FORGED_INSTRUCTION_DO_NOT_LEAK"

    class CompilerMustNotRun:
        def __init__(self) -> None:
            self.calls = 0

        def compile(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("forged direct instruction must not reach Plan or Binding")

    compiler = CompilerMustNotRun()
    raw = json.dumps(
        {
            "schema_version": 1,
            "decision": "DIRECT_ANSWER",
            "agent_id": "core_router",
            "instruction": forged,
            "reason_code": "MODEL_DIRECT",
        }
    )
    with pytest.raises(PlanningError) as captured:
        await PlanResolver(
            DEFAULT_AGENT_REGISTRY, compiler, FakePlanningModel(output=raw)
        ).resolve(
            PlanningRequest("core_router", "原始请求"), context()
        )
    assert captured.value.error_code is PlanningErrorCode.PLANNER_FIELD_FORBIDDEN
    assert compiler.calls == 0
    assert forged not in str(captured.value)
    assert forged not in caplog.text


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
    decision = DirectAnswerDecision("core_router", "TEST")
    delegated = StrictPlanningDecisionParser.parse(
        delegate_json([{"task_id": "code", "agent_id": "code_expert", "instruction": RAW}], True)
    )
    assert RAW not in repr(request)
    assert RAW not in repr(decision)
    assert RAW not in repr(delegated)
    resolved = PlanCompiler(DEFAULT_AGENT_REGISTRY).compile(
        decision, planning_source=PlanningSource.MODEL, direct_instruction=RAW
    )
    assert "invocation_bindings" not in repr(resolved)
    with pytest.raises(AttributeError):
        request.user_request = "changed"
    with pytest.raises(TypeError):
        asdict(request)
    with pytest.raises(TypeError):
        asdict(decision)
