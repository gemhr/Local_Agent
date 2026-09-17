"""Stage8 外部业务平台的 Typed Port、deterministic Mock 与 Tool Adapter。

Mock 平台只模拟外部系统 truth；AgentCore 的副作用仍必须由现有 Tool Runtime
负责治理、审批、幂等和执行状态。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Generic, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from core.runtime.retry import OperationIdempotency
from core.runtime.tool_adapters import ToolAdapter, ToolAdapterContext, ToolAdapterResponse
from core.runtime.tool_contract import (
    ToolExecutionSpec, ToolExecutionStatus, ToolInvocation, ToolSideEffectKind,
    ToolSideEffectState, thaw_json,
)


class _DTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FeatureDocument(_DTO):
    feature_id: str
    version: int = Field(ge=1)
    title: str
    content: str
    updated_at: str


class CodeDiff(_DTO):
    feature_id: str
    commit_id: str
    affected_files: list[str]
    diff_summary: str
    changed_components: list[str]


class MeetingSummary(_DTO):
    meeting_id: str
    change_points: list[str]
    clarifications: list[str]
    known_risks: list[str]
    unsupported_scenarios: list[str]
    config_changes: list[str]
    compatibility_notes: list[str]


class EnvironmentSnapshot(_DTO):
    environment_id: str
    version: str
    variant: str
    network_type: str
    board: str
    ue: str
    tool_version: str
    availability: str = "FREE"
    ip: str = ""
    capabilities: list[str] = Field(default_factory=list)
    feature_flags: list[str] = Field(default_factory=list)
    status: str | None = None

    @property
    def effective_status(self) -> str:
        return self.status or self.availability


class TestCaseSnapshot(_DTO):
    case_id: str
    version: int = Field(ge=1)
    title: str
    inputs: dict[str, str]
    expected_result: str
    assertion: str


class ExecutorSnapshot(_DTO):
    executor_id: str
    name: str
    capabilities: list[str]
    availability: str


class LogRecord(_DTO):
    execution_id: str
    lines: list[str]


class TicketRecord(_DTO):
    ticket_id: str
    status: str
    title: str
    severity: str


class TicketDraft(_DTO):
    title: str = Field(min_length=1)
    severity: str = Field(pattern="^(LOW|MEDIUM|HIGH|CRITICAL)$")
    description: str = Field(min_length=1)


class StartExecutionRequest(_DTO):
    provider_case_id: str = Field(min_length=1)
    case_path: str = Field(min_length=1)
    environment_id: str = Field(min_length=1)
    environment_ip: str = Field(min_length=1)
    execution_list_ref: str = Field(min_length=1)
    executor_id: str = "EXECUTOR-001"
    parameters: dict[str, str] = Field(default_factory=dict)


class ExecutionRecord(_DTO):
    execution_id: str
    case_id: str
    environment_id: str
    executor_id: str
    status: str
    parameters: dict[str, str]


class GetFeatureDocumentRequest(_DTO):
    feature_id: str = Field(min_length=1)


class GetCodeDiffRequest(_DTO):
    feature_id: str = Field(min_length=1)


class GetMeetingSummaryRequest(_DTO):
    meeting_id: str = Field(min_length=1)


class GetCaseRequest(_DTO):
    case_id: str = Field(min_length=1)


class GetEnvironmentRequest(_DTO):
    environment_id: str = Field(min_length=1)


class SearchEnvironmentsRequest(_DTO):
    version: str | None = None
    network_type: str | None = None
    hardware_type: str | None = None
    required_capabilities: list[str] = Field(default_factory=list)
    feature_flags: list[str] = Field(default_factory=list)


class GetExecutorRequest(_DTO):
    executor_id: str = Field(min_length=1)


class GetLogsRequest(_DTO):
    execution_id: str = Field(min_length=1)
    max_lines: int = Field(default=100, ge=1, le=1000)


class SearchTicketsRequest(_DTO):
    query: str = Field(min_length=1)


class CreateTicketRequest(TicketDraft):
    pass


class CaseGenerationRequest(_DTO):
    feature_id: str = Field(min_length=1)
    mission_id: str = Field(min_length=1)
    test_plan_subject_id: str = Field(min_length=1)
    test_plan_version: int = Field(ge=1)
    test_plan_digest: str = Field(min_length=1)
    scenario_id: str = Field(min_length=1)
    scenario_description: str = Field(min_length=1)
    preconditions: list[str] = Field(default_factory=list)
    expected_behavior: str = Field(min_length=1)


class GeneratedCaseResult(_DTO):
    provider_case_id: str = Field(min_length=1)
    case_path: str = Field(min_length=1)


def case_generation_idempotency_key(request: CaseGenerationRequest) -> str:
    """由完整且稳定的 Case Generation 业务输入构造 provider 幂等键。"""
    payload = request.model_dump(mode="json")
    return f"stage8_generate_case:{json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"


class _FeaturePort(Protocol):
    def get_feature_document(self, feature_id: str) -> FeatureDocument: ...
    def get_code_diff(self, feature_id: str) -> CodeDiff: ...
    def get_meeting_summary(self, meeting_id: str) -> MeetingSummary: ...


class _CasePort(Protocol):
    def get_case(self, case_id: str) -> TestCaseSnapshot: ...


class _EnvironmentPort(Protocol):
    def search_environments(self, request: SearchEnvironmentsRequest) -> list[EnvironmentSnapshot]: ...
    def get_environment(self, environment_id: str) -> EnvironmentSnapshot: ...


class _ExecutorPort(Protocol):
    def get_executor(self, executor_id: str) -> ExecutorSnapshot: ...
    def start_execution(self, request: StartExecutionRequest) -> ExecutionRecord: ...
    def get_execution(self, execution_id: str) -> ExecutionRecord: ...


class _LogPort(Protocol):
    def get_logs(self, execution_id: str, max_lines: int) -> LogRecord: ...


class _TicketPort(Protocol):
    def create_ticket(self, draft: TicketDraft) -> TicketRecord: ...
    def search_tickets(self, query: str) -> list[TicketRecord]: ...


# 对外的 Port 名称保持业务语义；Mock 实现可以同时满足这些窄协议。
FeatureDocumentPlatform = _FeaturePort
CodePlatform = _FeaturePort
MeetingSummaryPlatform = _FeaturePort
CasePlatform = _CasePort
EnvironmentPlatform = _EnvironmentPort
ExecutorPlatform = _ExecutorPort
LogPlatform = _LogPort
TicketPlatform = _TicketPort


class CaseGenerationPlatform(Protocol):
    def generate_case(self, request: CaseGenerationRequest) -> GeneratedCaseResult: ...
    def generate_case_with_key(
        self, request: CaseGenerationRequest, idempotency_key: str
    ) -> tuple[GeneratedCaseResult, bool]: ...


@dataclass
class DeterministicMockPlatform:
    """所有 WP2 mock port 的最小 process-local implementation。"""

    features: dict[str, FeatureDocument] = field(default_factory=dict)
    diffs: dict[str, CodeDiff] = field(default_factory=dict)
    meetings: dict[str, MeetingSummary] = field(default_factory=dict)
    cases: dict[str, TestCaseSnapshot] = field(default_factory=dict)
    environments: dict[str, EnvironmentSnapshot] = field(default_factory=dict)
    executors: dict[str, ExecutorSnapshot] = field(default_factory=dict)
    executions: dict[str, ExecutionRecord] = field(default_factory=dict)
    execution_idempotency: dict[str, ExecutionRecord] = field(default_factory=dict)
    tickets: dict[str, TicketRecord] = field(default_factory=dict)
    logs: dict[str, LogRecord] = field(default_factory=dict)
    generated_cases: dict[str, GeneratedCaseResult] = field(default_factory=dict)

    @classmethod
    def seeded(cls) -> "DeterministicMockPlatform":
        return cls(
            features={"FEATURE-001": FeatureDocument(feature_id="FEATURE-001", version=1, title="消息撤回", content="支持已发送消息撤回。", updated_at="2026-01-01T00:00:00Z")},
            diffs={"FEATURE-001": CodeDiff(feature_id="FEATURE-001", commit_id="abc123", affected_files=["core/messaging.py"], diff_summary="新增撤回路径", changed_components=["messaging"])},
            meetings={"MEETING-001": MeetingSummary(meeting_id="MEETING-001", change_points=["增加撤回窗口"], clarifications=["撤回后不可恢复"], known_risks=["权限校验"], unsupported_scenarios=[], config_changes=[], compatibility_notes=[])},
            cases={"CASE-001": TestCaseSnapshot(case_id="CASE-001", version=1, title="撤回消息", inputs={"role": "owner"}, expected_result="消息不可见", assertion="message.status == REVOKED")},
            environments={"ENV-001": EnvironmentSnapshot(environment_id="ENV-001", version="1", variant="staging", network_type="isolated", board="linux", ue="mock-ue", tool_version="agentcore-mock-1", availability="FREE", status="FREE", ip="10.0.0.1", capabilities=["CASE_EXECUTION"], feature_flags=[])},
            executors={"EXECUTOR-001": ExecutorSnapshot(executor_id="EXECUTOR-001", name="Mock Executor", capabilities=["CASE_EXECUTION"], availability="AVAILABLE")},
        )

    def get_feature_document(self, feature_id): return self.features[feature_id]
    def get_code_diff(self, feature_id): return self.diffs[feature_id]
    def get_meeting_summary(self, meeting_id): return self.meetings[meeting_id]
    def get_case(self, case_id): return self.cases[case_id]
    def get_environment(self, environment_id): return self.environments[environment_id]
    def search_environments(self, request):
        def matches(item):
            return (
                (request.version is None or item.version == request.version)
                and (request.network_type is None or item.network_type == request.network_type)
                and (request.hardware_type is None or item.board == request.hardware_type)
                and set(request.required_capabilities).issubset(item.capabilities)
                and set(request.feature_flags).issubset(item.feature_flags)
            )
        return sorted((item for item in self.environments.values() if matches(item)), key=lambda item: item.environment_id)
    def get_executor(self, executor_id): return self.executors[executor_id]
    def get_execution(self, execution_id): return self.executions[execution_id]
    def get_logs(self, execution_id, max_lines):
        return LogRecord(execution_id=execution_id, lines=self.logs.get(execution_id, LogRecord(execution_id=execution_id, lines=[])).lines[:max_lines])
    @staticmethod
    def execution_idempotency_key(request: StartExecutionRequest) -> str:
        """由完整执行输入构造稳定且可解释的 provider 幂等键。"""
        payload = request.model_dump(mode="json")
        return f"stage8_start_execution:{json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))}"

    def start_execution_with_key(self, request: StartExecutionRequest, idempotency_key: str) -> tuple[ExecutionRecord, bool]:
        expected_key = self.execution_idempotency_key(request)
        if idempotency_key != expected_key:
            raise ValueError("start_execution idempotency key must bind all execution parameters")
        existing = self.execution_idempotency.get(idempotency_key)
        if existing is not None:
            return existing, True
        execution_id = f"EXEC-{len(self.executions) + 1:03d}"
        record = ExecutionRecord(execution_id=execution_id, case_id=request.provider_case_id, environment_id=request.environment_id, executor_id=request.executor_id, status="RUNNING", parameters={**request.parameters, "case_path": request.case_path, "environment_ip": request.environment_ip, "execution_list_ref": request.execution_list_ref})
        self.executions[execution_id] = record
        self.execution_idempotency[idempotency_key] = record
        self.logs[execution_id] = LogRecord(execution_id=execution_id, lines=["execution accepted", "status=RUNNING"])
        return record, False

    def start_execution(self, request: StartExecutionRequest, idempotency_key: str | None = None) -> ExecutionRecord:
        record, _ = self.start_execution_with_key(request, idempotency_key or self.execution_idempotency_key(request))
        return record

    def generate_case_with_key(self, request: CaseGenerationRequest, idempotency_key: str) -> tuple[GeneratedCaseResult, bool]:
        expected_key = case_generation_idempotency_key(request)
        if idempotency_key != expected_key:
            raise ValueError("generate_case idempotency key must bind the full request")
        existing = self.generated_cases.get(idempotency_key)
        if existing is not None:
            return existing, True
        provider_case_id = f"CASE-GEN-{len(self.generated_cases) + 1:03d}"
        result = GeneratedCaseResult(
            provider_case_id=provider_case_id,
            case_path=f"/generated/cases/{provider_case_id}",
        )
        self.generated_cases[idempotency_key] = result
        return result, False

    def generate_case(self, request: CaseGenerationRequest, idempotency_key: str | None = None) -> GeneratedCaseResult:
        result, _ = self.generate_case_with_key(
            request, idempotency_key or case_generation_idempotency_key(request)
        )
        return result
    def create_ticket(self, draft):
        ticket_id = f"BUG-{len(self.tickets) + 1:03d}"
        record = TicketRecord(ticket_id=ticket_id, status="OPEN", title=draft.title, severity=draft.severity)
        self.tickets[ticket_id] = record
        return record
    def search_tickets(self, query):
        q = query.casefold()
        return [ticket for ticket in self.tickets.values() if q in ticket.title.casefold()]


RequestT = TypeVar("RequestT", bound=BaseModel)
ResultT = TypeVar("ResultT", bound=BaseModel)


class Stage8PlatformToolAdapter(ToolAdapter, Generic[RequestT, ResultT]):
    def __init__(self, tool_name: str, request_type: type[RequestT], result_type: type[ResultT], call: Callable, *, side_effect: bool = False, approval: bool = False, idempotency: OperationIdempotency | None = None, resource_key: str | None = None, keyed_call: Callable | None = None):
        self.request_type, self.result_type, self._call = request_type, result_type, call
        self._resource_key, self._keyed_call = resource_key, keyed_call
        idempotency_kind = idempotency or (OperationIdempotency.IDEMPOTENT_WITH_KEY if side_effect else OperationIdempotency.READ_ONLY)
        self.spec = ToolExecutionSpec(tool_name=tool_name, side_effect_kind=(ToolSideEffectKind.EXTERNAL_STATE_MUTATION if side_effect else ToolSideEffectKind.NONE), idempotency=idempotency_kind, requires_resource_key=side_effect, supports_side_effect_checkpoint=side_effect, supports_idempotency_replay=(idempotency_kind is OperationIdempotency.IDEMPOTENT_WITH_KEY and keyed_call is not None))
        self._side_effect = side_effect

    def llm_input_schema(self):
        schema = self.request_type.model_json_schema()
        schema.pop("title", None)
        return schema

    def build_invocation(self, argument_text: str) -> ToolInvocation:
        try:
            payload = json.loads(argument_text) if argument_text.strip() else {}
            request = self.request_type.model_validate(payload)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            from core.runtime.tool_adapters import ToolAdapterInvocationError
            from core.runtime.tool_contract import ToolErrorCategory, ToolExecutionPhase
            raise ToolAdapterInvocationError(category=ToolErrorCategory.VALIDATION, safe_error_code="STAGE8_TOOL_INPUT_INVALID", safe_message="Stage8 Tool input validation failed.", phase=ToolExecutionPhase.VALIDATION) from exc
        values = request.model_dump(mode="json")
        resource_key = self._resource_key or next((values[key] for key in ("feature_id", "case_id", "environment_id", "executor_id", "execution_id") if key in values), None)
        idempotency_key = None
        if self.spec.idempotency is OperationIdempotency.IDEMPOTENT_WITH_KEY:
            request_for_key = self.request_type.model_validate(values)
            if self.spec.tool_name == "stage8_start_execution":
                idempotency_key = DeterministicMockPlatform.execution_idempotency_key(request_for_key)
            elif self.spec.tool_name == "stage8_generate_case":
                idempotency_key = case_generation_idempotency_key(request_for_key)
            else:
                idempotency_key = f"{self.spec.tool_name}:{json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))}"
        return ToolInvocation.create(tool_name=self.spec.tool_name, arguments=values, resource_key=(f"stage8:{resource_key}" if self._side_effect and resource_key and self._resource_key is None else resource_key), idempotency_key=idempotency_key)

    def invoke_once(self, invocation: ToolInvocation, context: ToolAdapterContext) -> ToolAdapterResponse:
        request = self.request_type.model_validate(thaw_json(invocation.arguments))
        replayed = False
        if self._side_effect:
            context.before_side_effect()
        if self._keyed_call is not None:
            result, replayed = self._keyed_call(request, invocation.idempotency_key)
        else:
            result = self._call(request)
        payload = result.model_dump(mode="json") if isinstance(result, BaseModel) else TypeAdapter(self.result_type).dump_python(result, mode="json")
        return ToolAdapterResponse(content=json.dumps(payload, ensure_ascii=False, sort_keys=True), content_type="application/json", safe_summary=f"{self.spec.tool_name} completed", side_effect_state=(ToolSideEffectState.COMMITTED if self._side_effect else ToolSideEffectState.NOT_STARTED), idempotency_replayed=replayed, provider_operation_id=getattr(result, "execution_id", None) or getattr(result, "ticket_id", None) or getattr(result, "provider_case_id", None))


def build_stage8_tool_adapters(platform: DeterministicMockPlatform | None = None) -> tuple[tuple[str, str, ToolAdapter], ...]:
    p = platform if platform is not None else DeterministicMockPlatform.seeded()
    def item(name, description, req, result, fn, side=False, **kwargs): return (name, description, Stage8PlatformToolAdapter(name, req, result, fn, side_effect=side, **kwargs))
    return (
        item("stage8_get_feature_document", "Query a typed feature document from the mock business platform.", GetFeatureDocumentRequest, FeatureDocument, lambda r: p.get_feature_document(r.feature_id)),
        item("stage8_get_code_diff", "Query a typed code diff from the mock business platform.", GetCodeDiffRequest, CodeDiff, lambda r: p.get_code_diff(r.feature_id)),
        item("stage8_get_meeting_summary", "Query a typed meeting summary from the mock business platform.", GetMeetingSummaryRequest, MeetingSummary, lambda r: p.get_meeting_summary(r.meeting_id)),
        item("stage8_get_case", "Query an official typed test case snapshot.", GetCaseRequest, TestCaseSnapshot, lambda r: p.get_case(r.case_id)),
        item("stage8_get_environment", "Query a typed test environment snapshot.", GetEnvironmentRequest, EnvironmentSnapshot, lambda r: p.get_environment(r.environment_id)),
        item("stage8_search_environments", "Search typed test environments by requirements.", SearchEnvironmentsRequest, list[EnvironmentSnapshot], p.search_environments),
        item("stage8_get_executor", "Query a typed executor snapshot.", GetExecutorRequest, ExecutorSnapshot, lambda r: p.get_executor(r.executor_id)),
        item("stage8_get_logs", "Query bounded logs for an external execution.", GetLogsRequest, LogRecord, lambda r: p.get_logs(r.execution_id, r.max_lines)),
        item("stage8_search_tickets", "Search external tickets without creating a ticket.", SearchTicketsRequest, list[TicketRecord], lambda r: p.search_tickets(r.query)),
        item("stage8_start_execution", "Start one external test execution through governed runtime.", StartExecutionRequest, ExecutionRecord, p.start_execution, True, keyed_call=p.start_execution_with_key),
        item("stage8_generate_case", "Generate one official executable test case from a reviewed scenario.", CaseGenerationRequest, GeneratedCaseResult, p.generate_case, True, keyed_call=p.generate_case_with_key),
        item("stage8_create_ticket", "Create one external ticket after required tool approval.", CreateTicketRequest, TicketRecord, p.create_ticket, True, idempotency=OperationIdempotency.NON_IDEMPOTENT, resource_key="stage8:tickets"),
    )


def build_feature_context(platform: DeterministicMockPlatform, feature_id: str, meeting_id: str = "MEETING-001"):
    """从外部 Typed snapshots 组装 WP1 可直接消费的 FeatureContext。"""
    from core.stage8.specialists import EvidenceRef, EvidenceSourceType, FeatureContext
    feature, diff, meeting = platform.get_feature_document(feature_id), platform.get_code_diff(feature_id), platform.get_meeting_summary(meeting_id)
    return FeatureContext(feature_id=feature.feature_id, feature_document=feature.content, code_diff=diff.diff_summary, affected_modules=diff.changed_components, meeting_summary="；".join(meeting.change_points + meeting.clarifications), retrieval_evidence=[EvidenceRef(evidence_id=f"platform:{feature_id}:feature", source_type=EvidenceSourceType.FEATURE_DOCUMENT, source_ref=feature.feature_id, summary=feature.title), EvidenceRef(evidence_id=f"platform:{feature_id}:diff", source_type=EvidenceSourceType.CODE_DIFF, source_ref=diff.commit_id, summary=diff.diff_summary), EvidenceRef(evidence_id=f"platform:{feature_id}:meeting:{meeting.meeting_id}", source_type=EvidenceSourceType.MEETING_SUMMARY, source_ref=meeting.meeting_id, summary="；".join(meeting.change_points + meeting.clarifications))])


class FeatureContextBuilder:
    """WP1 FeatureContext 的最小 deterministic 组装入口。"""

    def __init__(self, platform: DeterministicMockPlatform):
        self.platform = platform

    def build(self, feature_id: str, meeting_id: str = "MEETING-001"):
        return build_feature_context(self.platform, feature_id, meeting_id)


__all__ = [name for name in globals() if not name.startswith("_")]
