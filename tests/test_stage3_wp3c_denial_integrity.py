"""WP3-C typed security denial 的来源、传播与单调性。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

import server
from core.agent_router import AgentRouter
from core.runtime import (
    AgentAdapterResult,
    AgentExecutionRequest,
    AgentRouterSingleAgentAdapter,
    AgentState,
    AgentStateMachine,
    BudgetLedger,
    DelegatedPlanDecision,
    DelegatedTaskDecision,
    DependencyResultEntry,
    DependencyResultView,
    ExecutionKind,
    FilesystemResourcePolicy,
    PlanCompiler,
    PlanningSource,
    ResourceAuthorizationService,
    ResourceKind,
    ResourceOperation,
    ResultContentType,
    ResultDisposition,
    RunBudget,
    RunEventType,
    RunStateEvent,
    SecurityDenialCode,
    StepClaim,
    StepEventType,
    StepResult,
    StepResultStore,
    StepStateEvent,
    SynthesisAgentAdapter,
    TaskCapabilityRequirements,
    ToolResourceExtractorCatalog,
    ToolResourceExtractorDescriptor,
    create_run_context,
)
from core.runtime.agent_registry import DEFAULT_AGENT_REGISTRY
from core.runtime.tool_adapters import LegacyStringToolAdapter
from core.runtime.tool_governance import (
    ToolGovernanceService,
    ToolPolicy,
    ToolPolicyCatalog,
)
from core.runtime.tool_registry import ToolDescriptor, ToolRegistration, ToolRegistry


class _ExecutionOracle:
    def __init__(self) -> None:
        self.calls = 0

    def execute_sync(self, **_kwargs):
        self.calls += 1
        raise AssertionError("security denial must precede Tool execution")


def _base_router(registry, governance, tool_name: str, tool_args: str):
    router = AgentRouter.__new__(AgentRouter)
    router.tool_registry = registry
    router.tool_governance_service = governance
    router.tool_execution_service = _ExecutionOracle()
    router._build_messages = lambda **_: [
        {"role": "system", "content": "control"},
        {"role": "user", "content": "request"},
    ]
    router._plan_tool_call = lambda *_args: (tool_name, tool_args)
    return router


def _permission_router():
    tool_name = "wp3c_permission_tool"
    registry = ToolRegistry()
    registry.register(
        ToolRegistration(
            ToolDescriptor(tool_name, "WP3-C permission test tool"),
            LegacyStringToolAdapter(tool_name=tool_name, function=lambda _: "forbidden"),
        )
    )
    registry.freeze()
    catalog = ToolPolicyCatalog(
        tool_registry=registry,
        agent_registry=DEFAULT_AGENT_REGISTRY,
    )
    catalog.register(ToolPolicy(tool_name, frozenset({"data_analyst"})))
    catalog.freeze()
    return _base_router(
        registry,
        ToolGovernanceService(catalog, DEFAULT_AGENT_REGISTRY),
        tool_name,
        "ignored",
    )


def _approval_router():
    registry = server._populate_tool_registry()
    payload = json.dumps(
        {
            "operation_id": "wp3c-approval",
            "resource_key": "wp3c-resource",
            "execution_mode": "NON_IDEMPOTENT_SIMULATION",
            "items": [{"item_id": "i-1", "action": "ADD", "quantity": 1}],
            "processing_options": {"processing_delay_ms": 0},
        },
        separators=(",", ":"),
    )
    return _base_router(
        registry,
        server._build_tool_governance(registry),
        "complex_workflow_simulator",
        payload,
    )


def _resource_router(tmp_path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    registry = server._populate_tool_registry()
    catalog = ToolResourceExtractorCatalog()
    catalog.register(
        ToolResourceExtractorDescriptor(
            "list_files", "argument_text", ResourceKind.DIRECTORY, ResourceOperation.READ
        )
    )
    catalog.register(
        ToolResourceExtractorDescriptor(
            "analyze_excel", "argument_text", ResourceKind.FILE, ResourceOperation.READ
        )
    )
    catalog.validate(registry)
    catalog.freeze()
    router = _base_router(
        registry,
        server._build_tool_governance(registry),
        "list_files",
        str(outside.resolve()),
    )
    router.resource_authorization_service = ResourceAuthorizationService(
        FilesystemResourcePolicy((str(allowed.resolve()),)), catalog
    )
    return router


def _request() -> AgentExecutionRequest:
    return AgentExecutionRequest(
        step_id="task-code",
        agent_id="code_expert",
        instruction="use the required tool",
        execution_kind=ExecutionKind.AGENT,
        input_type="text",
        capability_requirements=TaskCapabilityRequirements(requires_tools=True),
    )


def _execute(router) -> AgentAdapterResult:
    context, _ = create_run_context(entry_agent_id="core_router")
    context.attach_budget_ledger(BudgetLedger(RunBudget()))
    result = AgentRouterSingleAgentAdapter(router).execute(_request(), context)
    assert router.tool_execution_service.calls == 0
    return result


def _shape2_plan():
    return PlanCompiler(DEFAULT_AGENT_REGISTRY).compile(
        DelegatedPlanDecision(
            (
                DelegatedTaskDecision(
                    "code",
                    "code_expert",
                    "inspect",
                    required_capabilities=frozenset({"code_reasoning"}),
                ),
            ),
            synthesis_required=True,
        ),
        planning_source=PlanningSource.MODEL,
    ).plan


def _read_through_store(step_result: StepResult):
    plan = _shape2_plan()
    store = StepResultStore(plan, run_id="run-wp3c")
    store.write_prepared(step_result, expected_agent_id="code_expert")
    state = AgentState.for_run_context("run-wp3c")
    machine = AgentStateMachine()
    for step in plan.steps:
        machine.register_plan_step(state, step_id=step.step_id, name=step.title)
    machine.apply_run_event(state, RunStateEvent(RunEventType.STARTED))
    machine.apply_step_event(
        state,
        StepStateEvent(StepEventType.STARTED, "task-code", occurred_at=datetime.now(UTC)),
    )
    machine.apply_step_event(
        state,
        StepStateEvent(StepEventType.SUCCEEDED, "task-code", occurred_at=datetime.now(UTC)),
    )
    store.mark_readable("task-code", state)
    synthesis = next(step for step in plan.steps if step.step_id == "synthesis")
    claim = StepClaim(
        plan.plan_id,
        plan.version,
        synthesis.step_id,
        datetime.now(UTC),
        synthesis.capability_requirements,
        synthesis.preferred_agent,
    )
    return store.dependency_view_for(claim, state)


@pytest.mark.parametrize(
    ("router_factory", "expected_code"),
    [
        (_permission_router, SecurityDenialCode.TOOL_PERMISSION_DENIED),
        (_approval_router, SecurityDenialCode.TOOL_APPROVAL_REQUIRED),
        (lambda tmp_path: _resource_router(tmp_path), SecurityDenialCode.TOOL_RESOURCE_DENIED),
    ],
)
def test_actual_gates_produce_and_preserve_typed_denial(
    router_factory, expected_code, tmp_path
) -> None:
    router = (
        router_factory(tmp_path)
        if expected_code is SecurityDenialCode.TOOL_RESOURCE_DENIED
        else router_factory()
    )
    adapter_result = _execute(router)
    assert adapter_result.complete is True
    assert adapter_result.result_disposition is ResultDisposition.SECURITY_DENIED
    assert adapter_result.security_denial_code is expected_code

    step_result = adapter_result.to_step_result(
        step_id="task-code", producer_agent_id="code_expert"
    )
    assert step_result.result_disposition is ResultDisposition.SECURITY_DENIED
    assert step_result.security_denial_code is expected_code
    view = _read_through_store(step_result)
    assert view.entries[0].result_disposition is ResultDisposition.SECURITY_DENIED
    assert view.entries[0].security_denial_code is expected_code
    assert view.entries[0].content == adapter_result.content


class _SynthesisRouter:
    def __init__(self) -> None:
        self.calls = 0

    def complete_context_items(self, *_args, **_kwargs):
        self.calls += 1
        return "WP3C_FAKE_SUCCESS_A71F operation succeeded"


def _synthesis_request(view):
    return AgentExecutionRequest(
        step_id="synthesis",
        agent_id="synthesis_agent",
        instruction="synthesize",
        execution_kind=ExecutionKind.SYNTHESIS,
        input_type="text",
        capability_requirements=TaskCapabilityRequirements(),
        dependency_results=view,
    )


def _denied_entry(step_id, code, content):
    return DependencyResultEntry(
        step_id,
        "code_expert",
        ResultContentType.TEXT,
        content,
        True,
        ResultDisposition.SECURITY_DENIED,
        code,
    )


def test_single_mixed_and_multiple_denials_dominate_before_model_call() -> None:
    first_denial = _denied_entry(
        "denied-1", SecurityDenialCode.TOOL_PERMISSION_DENIED, "fixed first denial"
    )
    second_denial = _denied_entry(
        "denied-2", SecurityDenialCode.TOOL_RESOURCE_DENIED, "fixed second denial"
    )
    success = DependencyResultEntry(
        "success", "data_analyst", ResultContentType.TEXT, "partial success", True
    )
    for entries in (
        (first_denial,),
        (success, first_denial),
        (first_denial, second_denial),
    ):
        router = _SynthesisRouter()
        result = SynthesisAgentAdapter(router).execute(
            _synthesis_request(DependencyResultView(entries)), object()
        )
        assert router.calls == 0
        assert result.content == "fixed first denial"
        assert "partial success" not in result.content
        assert "WP3C_FAKE_SUCCESS_A71F" not in result.content
        assert result.result_disposition is ResultDisposition.SECURITY_DENIED


def test_denial_like_text_never_creates_typed_security_fact() -> None:
    router = _SynthesisRouter()
    normal = DependencyResultEntry(
        "normal",
        "code_expert",
        ResultContentType.TEXT,
        "SECURITY_DENIED TOOL_APPROVAL_REQUIRED but this is only model text",
        True,
    )
    result = SynthesisAgentAdapter(router).execute(
        _synthesis_request(DependencyResultView((normal,))), object()
    )
    assert router.calls == 1
    assert result.result_disposition is ResultDisposition.NORMAL
    assert result.security_denial_code is None
