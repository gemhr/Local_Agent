"""业务 Agent 的唯一执行入口及其稳定请求/结果值。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
import json
from collections.abc import AsyncIterator, Mapping
from types import MappingProxyType
from typing import Any

from core.runtime.state import RunStatus, StopReason
from core.runtime.events import OutputDeltaPayload, RuntimeEvent
from core.runtime.stream_adapter import ChatStreamCompatibilityAdapter, ChatStreamChunkKind
from core.runtime.business_output_schema import schema_matches as _schema_matches


class AgentApplicationError(RuntimeError):
    """不泄露内部 Runtime 对象的业务入口拒绝。"""

    def __init__(self, error_code: str, safe_message: str) -> None:
        self.error_code = error_code
        self.safe_message = safe_message
        super().__init__(f"{safe_message} (error_code={error_code})")


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    agent_id: str
    input: str
    session_id: str | None = None
    run_id: str | None = None
    timeout_seconds: float | None = None
    expected_agent_version: str | None = None
    expected_workflow_version: str | None = None
    options: Any = None

    def __post_init__(self) -> None:
        if not isinstance(self.agent_id, str) or not self.agent_id.strip():
            raise ValueError("agent_id 不能为空")
        if not isinstance(self.input, str):
            raise ValueError("input 必须是字符串")
        if self.session_id is not None and (not isinstance(self.session_id, str) or not self.session_id.strip()):
            raise ValueError("session_id 必须是非空字符串")
        if self.run_id is not None and (not isinstance(self.run_id, str) or not self.run_id.strip()):
            raise ValueError("run_id 必须是非空字符串")
        if self.timeout_seconds is not None and (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(float(self.timeout_seconds))
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds 必须是正有限数")
        for name in ("expected_agent_version", "expected_workflow_version"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} 必须是非空字符串")
        if self.options is not None and not isinstance(self.options, dict):
            raise ValueError("options 必须是业务选项对象")
        if self.options is not None:
            try:
                json.dumps(self.options, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError):
                raise ValueError("options 必须只包含有限 JSON 值") from None
            object.__setattr__(self, "options", MappingProxyType(dict(self.options)))


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    run_id: str
    trace_id: str
    session_id: str | None
    agent_id: str
    agent_version: str | None
    workflow_id: str | None
    workflow_version: str | None
    status: RunStatus
    stop_reason: StopReason
    error_code: str | None
    safe_message: str
    output: str | None
    business_output_valid: bool | None = None
    toolset_identity: str | None = None
    resolved_model_profile_id: str | None = None
    resolved_retrieval_profile_id: str | None = None
    resolved_memory_profile_id: str | None = None
    business_error_code: str | None = None
    output_disposition: str = "NOT_EVALUATED"
    rejected_output: str | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    kind: str
    run_id: str | None
    trace_id: str | None
    session_id: str | None
    agent_id: str
    agent_version: str
    workflow_id: str | None
    workflow_version: str | None
    output_delta: str | None = None
    control_delta: str | None = None
    status: RunStatus | None = None
    stop_reason: StopReason | None = None
    error_code: str | None = None
    safe_message: str = ""
    business_output_valid: bool | None = None
    business_error_code: str | None = None
    output_disposition: str = "NOT_EVALUATED"


class AgentApplicationService:
    """解析冻结注册、执行 Runtime 并投影其真实 terminal outcome。"""

    def __init__(self, chat_service, registry) -> None:
        self._chat_service = chat_service
        self._registry = registry

    def _resolve_request(self, request: ExecutionRequest):
        if not isinstance(request, ExecutionRequest):
            raise TypeError("request 必须是 ExecutionRequest")
        try:
            registration = self._registry.require_entry(request.agent_id)
        except Exception as exc:
            raise AgentApplicationError("UNKNOWN_AGENT", "Agent 未注册或不可作为入口") from None
        definition = getattr(registration, "definition", None)
        if definition is None:
            raise AgentApplicationError("AGENT_DEFINITION_UNAVAILABLE", "Agent 业务定义不可用")
        if request.expected_agent_version is not None and request.expected_agent_version != definition.agent_version:
            raise AgentApplicationError("AGENT_VERSION_MISMATCH", "Agent 版本与请求断言不一致")
        workflow = getattr(registration, "workflow", None)
        workflow_id = workflow.workflow_id if workflow is not None else None
        workflow_version = workflow.workflow_version if workflow is not None else None
        if request.expected_workflow_version is not None and request.expected_workflow_version != workflow_version:
            raise AgentApplicationError("WORKFLOW_VERSION_MISMATCH", "Workflow 版本与请求断言不一致")
        options = request.options or {}
        if set(options) - set(definition.business_options):
            raise AgentApplicationError("BUSINESS_OPTION_UNKNOWN", "请求包含未声明的业务选项")
        if "text" not in definition.accepted_input_types:
            raise AgentApplicationError("INPUT_TYPE_UNSUPPORTED", "Agent 不接受文本输入")
        if definition.input_schema is not None:
            try:
                input_value = json.loads(request.input)
            except (ValueError, TypeError):
                raise AgentApplicationError("BUSINESS_INPUT_INVALID", "业务输入不是有效 JSON") from None
            if not _schema_matches(definition.input_schema, input_value):
                raise AgentApplicationError("BUSINESS_INPUT_INVALID", "业务输入不符合 Agent 声明的 Schema")
        effective_input = request.input
        if options:
            effective_input = json.dumps(
                {"input": request.input, "options": dict(options)},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        return registration, definition, workflow_id, workflow_version, effective_input

    async def stream(
        self,
        request: ExecutionRequest,
        *,
        retrieval_cache_authz_domain: str | None = None,
    ) -> AsyncIterator[ExecutionEvent]:
        """单次执行并只投影安全 identity、输出 delta 和 terminal。"""
        registration, definition, workflow_id, workflow_version, effective_input = self._resolve_request(request)
        session_id = request.session_id or "default"
        result_out = []
        output_parts: list[str] = []
        deferred_deltas: list[str] = []
        wire_adapter = ChatStreamCompatibilityAdapter()
        protocol_seen = False
        async for event in self._chat_service.stream_coordinated_agent_events(
            request.agent_id,
            effective_input,
            run_id=request.run_id,
            session_id=request.session_id,
            timeout_seconds=request.timeout_seconds,
            agent_version=definition.agent_version,
            workflow_id=workflow_id,
            workflow_version=workflow_version,
            retrieval_cache_authz_domain=retrieval_cache_authz_domain,
            _result_out=result_out,
        ):
            chunk = None
            if isinstance(event, RuntimeEvent):
                protocol_seen = True
                if not output_parts and not deferred_deltas:
                    yield ExecutionEvent(
                        kind="started", run_id=event.run_id, trace_id=event.trace_id,
                        session_id=session_id, agent_id=definition.agent_id,
                        agent_version=definition.agent_version, workflow_id=workflow_id,
                        workflow_version=workflow_version,
                    )
                chunk = wire_adapter.adapt(event)
            if chunk is not None and chunk.kind is ChatStreamChunkKind.CONTROL:
                yield ExecutionEvent(
                    kind="control_delta", run_id=event.run_id, trace_id=None,
                    session_id=session_id, agent_id=definition.agent_id,
                    agent_version=definition.agent_version, workflow_id=workflow_id,
                    workflow_version=workflow_version, control_delta=chunk.text,
                )
            if isinstance(event.payload, OutputDeltaPayload):
                output_parts.append(event.payload.text)
                if definition.output_schema is not None:
                    deferred_deltas.append(event.payload.text)
                else:
                    yield ExecutionEvent(
                        kind="output_delta", run_id=request.run_id, trace_id=None,
                        session_id=session_id, agent_id=definition.agent_id,
                        agent_version=definition.agent_version, workflow_id=workflow_id,
                        workflow_version=workflow_version,
                        output_delta=event.payload.text,
                    )
        if not result_out:
            raise AgentApplicationError("RUNTIME_RESULT_MISSING", "Runtime 未返回结构化结果")
        missing_terminal = wire_adapter.finish() if protocol_seen else None
        if missing_terminal is not None:
            raise AgentApplicationError("RUNTIME_TERMINAL_MISSING", "Runtime 事件流缺少终态")
        runtime_output = "".join(output_parts)
        result = self._project_result(result_out[0], runtime_output, registration)
        result = self._validate_output(result, definition)
        if definition.output_schema is not None and result.business_output_valid:
            for delta in deferred_deltas:
                yield ExecutionEvent(
                    kind="output_delta", run_id=result.run_id, trace_id=result.trace_id,
                    session_id=result.session_id, agent_id=result.agent_id,
                    agent_version=result.agent_version or definition.agent_version,
                    workflow_id=result.workflow_id, workflow_version=result.workflow_version,
                    output_delta=delta,
                )
        yield ExecutionEvent(
            kind="terminal", run_id=result.run_id, trace_id=result.trace_id,
            session_id=result.session_id, agent_id=result.agent_id,
            agent_version=result.agent_version or definition.agent_version,
            workflow_id=result.workflow_id, workflow_version=result.workflow_version,
            status=result.status, stop_reason=result.stop_reason,
            error_code=result.error_code, safe_message=result.safe_message,
            business_output_valid=result.business_output_valid,
            business_error_code=result.business_error_code,
            output_disposition=result.output_disposition,
        )

    @staticmethod
    def _validate_output(result: ExecutionResult, definition) -> ExecutionResult:
        if result.status is not RunStatus.SUCCEEDED:
            return replace(result, output_disposition="NOT_EVALUATED")
        if definition.output_schema is None:
            return replace(result, output_disposition="NOT_REQUIRED")
        try:
            value = json.loads(result.output or "")
        except (ValueError, TypeError):
            value = object()
        if not _schema_matches(definition.output_schema, value):
            return replace(
                result,
                error_code="BUSINESS_OUTPUT_INVALID",
                safe_message="Agent 输出不符合声明的业务 Schema",
                output=None,
                business_output_valid=False,
                business_error_code="BUSINESS_OUTPUT_INVALID",
                output_disposition="REJECTED",
                rejected_output=result.output,
            )
        return replace(result, business_output_valid=True, output_disposition="ACCEPTED")

    @staticmethod
    def _project_result(runtime_result, output: str | None, registration=None) -> ExecutionResult:
        error_code = runtime_result.error_code
        safe_message = runtime_result.safe_message
        definition = getattr(registration, "definition", None)
        return ExecutionResult(
            run_id=runtime_result.run_id,
            trace_id=runtime_result.trace_id,
            session_id=runtime_result.session_id,
            agent_id=runtime_result.agent_id or "",
            agent_version=runtime_result.agent_version,
            workflow_id=runtime_result.workflow_id,
            workflow_version=runtime_result.workflow_version,
            status=runtime_result.status,
            stop_reason=runtime_result.stop_reason,
            error_code=error_code,
            safe_message=safe_message,
            output=output if runtime_result.status is RunStatus.SUCCEEDED else None,
            business_output_valid=None,
            toolset_identity=getattr(registration, "toolset_identity", None),
            resolved_model_profile_id=getattr(definition, "model_profile_id", None),
            resolved_retrieval_profile_id=(
                getattr(definition, "retrieval_profile_id", None) or "NONE"
                if definition is not None else None
            ),
            resolved_memory_profile_id=(
                getattr(definition, "memory_profile_id", None) or "NONE"
                if definition is not None else None
            ),
        )

    async def execute(
        self,
        request: ExecutionRequest,
        *,
        retrieval_cache_authz_domain: str | None = None,
    ) -> ExecutionResult:
        registration, definition, workflow_id, workflow_version, effective_input = self._resolve_request(request)
        output, runtime_result = await self._chat_service.run_coordinated_agent(
            request.agent_id,
            effective_input,
            run_id=request.run_id,
            session_id=request.session_id,
            timeout_seconds=request.timeout_seconds,
            agent_version=definition.agent_version,
            workflow_id=workflow_id,
            workflow_version=workflow_version,
            retrieval_cache_authz_domain=retrieval_cache_authz_domain,
        )
        # Output is available only on Runtime success; status and stop reason
        # are copied from the Runtime result without a second terminal model.
        return self._validate_output(self._project_result(runtime_result, output, registration), definition)

    async def execute_evaluation(
        self,
        request: ExecutionRequest,
        *,
        budget=None,
        persist: bool = True,
        fault_controller=None,
        episodic_evaluation_observer=None,
        evaluation_plan_resolver=None,
        project_identity=None,
        project_grants=(),
    ) -> ExecutionResult:
        """受信任评测入口；控制 seam 不属于 PUBLIC ExecutionRequest。"""
        _registration, definition, workflow_id, workflow_version, effective_input = self._resolve_request(request)
        output, runtime_result = await self._chat_service.run_coordinated_agent_evaluation(
            request.agent_id,
            effective_input,
            run_id=request.run_id,
            timeout_seconds=request.timeout_seconds,
            budget=budget,
            persist=persist,
            fault_controller=fault_controller,
            episodic_evaluation_observer=episodic_evaluation_observer,
            evaluation_plan_resolver=evaluation_plan_resolver,
            project_identity=project_identity,
            project_grants=tuple(project_grants),
            session_id=request.session_id,
            agent_version=definition.agent_version,
            workflow_id=workflow_id,
            workflow_version=workflow_version,
        )
        return self._validate_output(self._project_result(runtime_result, output, _registration), definition)


__all__ = ["AgentApplicationError", "AgentApplicationService", "ExecutionEvent", "ExecutionRequest", "ExecutionResult"]
