"""业务 Agent 的唯一执行入口及其稳定请求/结果值。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from contextlib import aclosing
import math
import json
from collections.abc import AsyncIterator, Mapping
from types import MappingProxyType
from typing import Any, TYPE_CHECKING

from core.runtime.memory_scope import MemoryAccessContext
from core.runtime.state import RunStatus, StopReason
from core.agent_platform.runtime_adapter import (
    ExecutionResult,
    NativeRuntimeAdapter,
    RuntimeAdapter,
    RuntimeAdapterError,
    RuntimeRequest,
)
from core.runtime.business_output_schema import schema_matches as _schema_matches

if TYPE_CHECKING:
    from core.agent_platform.opencode_runtime_adapter import OpenCodeRuntimeAdapter
    from core.persistence.repositories.external_runs import ExternalRunRepository


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
    runtime_instance_id: str | None = None

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
        for name in ("expected_agent_version", "expected_workflow_version", "runtime_instance_id"):
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

    def __init__(
        self, chat_service, registry, *,
        runtime_adapter: RuntimeAdapter | None = None,
        runtime_instances: Mapping[str, OpenCodeRuntimeAdapter] | None = None,
        external_runs: ExternalRunRepository | None = None,
    ) -> None:
        self._chat_service = chat_service
        self._registry = registry
        self._native_adapter = (
            runtime_adapter if isinstance(runtime_adapter, NativeRuntimeAdapter)
            else NativeRuntimeAdapter(chat_service)
        )
        self._runtime_adapter = runtime_adapter if runtime_adapter is not None else self._native_adapter
        self._runtime_instances = dict(runtime_instances or {})
        self._external_runs = external_runs

    def _select_runtime(self, request: ExecutionRequest) -> RuntimeAdapter:
        if request.runtime_instance_id is None:
            return self._runtime_adapter
        adapter = self._runtime_instances.get(request.runtime_instance_id)
        # WP2 只开放普通文本入口，避免绕过业务 Agent 的工具及执行策略。
        if adapter is None or request.agent_id != "core_router":
            raise AgentApplicationError("RUNTIME_INSTANCE_UNAVAILABLE", "Runtime 实例未启用或不可用于此 Agent")
        return adapter

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
        if request.agent_id == "ci_execution_status" and options:
            raise AgentApplicationError("BUSINESS_OPTION_UNKNOWN", "状态查询不接受客户端策略选项")
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
        memory_access_context: MemoryAccessContext | None = None,
        require_new_run: bool = False,
    ) -> AsyncIterator[ExecutionEvent]:
        """单次执行并只投影安全 identity、输出 delta 和 terminal。"""
        registration, definition, workflow_id, workflow_version, effective_input = self._resolve_request(request)
        adapter = self._select_runtime(request)
        external_runs = self._external_runs if request.runtime_instance_id is not None else None
        runtime_request = RuntimeRequest(
            agent_id=request.agent_id, input=effective_input, agent_version=definition.agent_version,
            workflow_id=workflow_id, workflow_version=workflow_version,
            run_id=request.run_id, session_id=request.session_id, timeout_seconds=request.timeout_seconds,
            retrieval_cache_authz_domain=retrieval_cache_authz_domain, memory_access_context=memory_access_context,
            runtime_instance_id=request.runtime_instance_id,
            require_new_run=require_new_run,
        )
        result = None
        deferred_deltas: list[str] = []
        try:
            async with aclosing(adapter.stream(runtime_request)) as events:
                async for event in events:
                    if isinstance(event, ExecutionResult):
                        result = event
                        if external_runs is not None:
                            result = self._validate_output(result, definition)
                            if result.business_output_valid and result.output is not None:
                                await external_runs.append_update(result.run_id, "output.delta", {"text": result.output})
                            await external_runs.finish(result)
                        break
                    if event.kind == "output_delta" and definition.output_schema is not None:
                        deferred_deltas.append(event.text or "")
                        continue
                    if external_runs is not None:
                        if event.kind == "output_delta":
                            await external_runs.append_update(event.run_id, "output.delta", {"text": event.text})
                        elif event.kind == "control_delta":
                            await external_runs.append_update(event.run_id, "run.progress", {"phase": event.text})
                    yield ExecutionEvent(
                        kind=event.kind, run_id=event.run_id,
                        trace_id=event.trace_id if event.kind == "started" else None,
                        session_id=request.session_id or "default", agent_id=definition.agent_id,
                        agent_version=definition.agent_version, workflow_id=workflow_id,
                        workflow_version=workflow_version,
                        output_delta=event.text if event.kind == "output_delta" else None,
                        control_delta=event.text if event.kind == "control_delta" else None,
                    )
        except RuntimeAdapterError as exc:
            raise AgentApplicationError(exc.error_code, exc.safe_message) from None
        if result is None:
            raise AgentApplicationError("RUNTIME_RESULT_MISSING", "Runtime 未返回结构化结果")
        if request.runtime_instance_id is None:
            result = self._apply_registration(result, registration)
        if external_runs is None:
            result = self._validate_output(result, definition)
        if definition.output_schema is not None and result.business_output_valid:
            approved_deltas = [result.output] if request.runtime_instance_id is not None else deferred_deltas
            for delta in approved_deltas:
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
    def _apply_registration(result: ExecutionResult, registration) -> ExecutionResult:
        definition = registration.definition
        return replace(
            result,
            toolset_identity=getattr(registration, "toolset_identity", None),
            resolved_model_profile_id=getattr(definition, "model_profile_id", None),
            resolved_retrieval_profile_id=getattr(definition, "retrieval_profile_id", None) or "NONE",
            resolved_memory_profile_id=getattr(definition, "memory_profile_id", None) or "NONE",
        )

    async def execute(
        self,
        request: ExecutionRequest,
        *,
        retrieval_cache_authz_domain: str | None = None,
        memory_access_context: MemoryAccessContext | None = None,
        adaptive_route: dict | None = None,
    ) -> ExecutionResult:
        registration, definition, workflow_id, workflow_version, effective_input = self._resolve_request(request)
        adapter = self._select_runtime(request)
        runtime_request = RuntimeRequest(
            agent_id=request.agent_id, input=effective_input, agent_version=definition.agent_version,
            workflow_id=workflow_id, workflow_version=workflow_version,
            run_id=request.run_id, session_id=request.session_id, timeout_seconds=request.timeout_seconds,
            retrieval_cache_authz_domain=retrieval_cache_authz_domain, memory_access_context=memory_access_context,
            runtime_instance_id=request.runtime_instance_id,
        )
        single_call = definition.business_options.get("stage13_single_call") is True
        if request.runtime_instance_id is not None:
            if adaptive_route is not None or single_call:
                raise AgentApplicationError("RUNTIME_INSTANCE_UNAVAILABLE", "Runtime 实例未启用或不可用于此 Agent")
            try:
                result = await adapter.execute(runtime_request)
            except RuntimeAdapterError as exc:
                raise AgentApplicationError(exc.error_code, exc.safe_message) from None
            result = self._validate_output(result, definition)
            if self._external_runs is not None:
                if result.output is not None:
                    await self._external_runs.append_update(result.run_id, "output.delta", {"text": result.output})
                await self._external_runs.finish(result)
            return result

        route = None
        if adaptive_route is not None:
            from core.agent_platform.ci_status import validate_route
            route = validate_route(adaptive_route, agent_id=request.agent_id)
            if route["query"] != json.loads(request.input):
                raise AgentApplicationError("CI_STATUS_INPUT_INVALID", "查询与可信绑定不一致")
        if route is not None or single_call:
            result = await self._native_adapter.execute_trusted(
                runtime_request, adaptive_route=route, single_call=single_call,
            )
        else:
            result = await adapter.execute(runtime_request)
        return self._validate_output(self._apply_registration(result, registration), definition)

    async def read_external_result(self, run_id: str) -> dict | None:
        """按已授权 Run 的绑定重读正文，必要时收敛未知状态。"""
        if self._external_runs is None:
            return None
        row = await self._external_runs.get(run_id)
        if row is None:
            return None
        output = None
        read_error = None
        if row.terminal_status in (None, RunStatus.SUCCEEDED.value):
            adapter = self._runtime_instances.get(row.instance_id)
            try:
                if adapter is None:
                    raise RuntimeAdapterError("OPENCODE_RESULT_UNKNOWN", "外部执行实例不可用")
                definition = self._registry.require_entry(row.agent_id).definition
                if definition is None or definition.agent_version != row.agent_version:
                    raise RuntimeAdapterError("OPENCODE_RESULT_UNKNOWN", "Agent 业务定义版本不可用")
                result = await adapter.reread(row)
                if result is not None:
                    result = self._validate_output(result, definition)
                    if row.terminal_status is not None and (
                        result.status.value != row.terminal_status or result.external_assistant_id != row.assistant_message_id
                    ):
                        raise RuntimeAdapterError("OPENCODE_RESULT_UNKNOWN", "外部结果与已确认状态不一致")
                    row = await self._external_runs.finish(result)
                    if (
                        result.status.value == row.terminal_status
                        and result.external_assistant_id == row.assistant_message_id
                    ):
                        output = result.output
            except (LookupError, RuntimeAdapterError):
                if row.terminal_status is None:
                    await self._external_runs.observe_unknown(run_id)
                    row = await self._external_runs.get(run_id)
                read_error = "OPENCODE_RESULT_UNKNOWN"
        return {
            "run_id": row.run_id,
            "runtime_instance_id": row.instance_id,
            "observation_state": row.observation_state,
            "status": row.terminal_status,
            "stop_reason": row.stop_reason,
            "error_code": row.error_code,
            "read_error_code": read_error,
            "output": output,
            "external_session_id": row.external_session_id,
            "user_message_id": row.user_message_id,
            "assistant_message_id": row.assistant_message_id,
        }

    async def cancel_detached_external(self, run_id: str) -> None:
        """已绑定会话失去本地句柄时，仍只对原实例尝试取消。"""
        if self._external_runs is None:
            return
        row = await self._external_runs.get(run_id)
        if row is None or row.observation_state not in {"ACTIVE", "UNKNOWN"}:
            return
        adapter = self._runtime_instances.get(row.instance_id)
        try:
            if adapter is None:
                raise RuntimeAdapterError("OPENCODE_RESULT_UNKNOWN", "外部执行实例不可用")
            result = await adapter.cancel_detached(row)
        except RuntimeAdapterError:
            await self._external_runs.observe_unknown(run_id)
            return
        if result is not None:
            await self._external_runs.finish(result)

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
        execution_policy=None,
        loop_policy=None,
        loop_correlation=None,
        adaptive_stop_enabled=None,
        memory_access_context: MemoryAccessContext | None = None,
    ) -> ExecutionResult:
        """受信任评测入口；控制 seam 不属于 PUBLIC ExecutionRequest。"""
        if request.runtime_instance_id is not None:
            raise AgentApplicationError("RUNTIME_INSTANCE_UNAVAILABLE", "评测入口未启用外部 Runtime 实例")
        registration, definition, workflow_id, workflow_version, effective_input = self._resolve_request(request)
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
            execution_policy=execution_policy,
            loop_policy=loop_policy,
            loop_correlation=loop_correlation,
            adaptive_stop_enabled=adaptive_stop_enabled,
            memory_access_context=memory_access_context,
            session_id=request.session_id,
            agent_version=definition.agent_version,
            workflow_id=workflow_id,
            workflow_version=workflow_version,
        )
        result = self._native_adapter.project_result(runtime_result, output)
        return self._validate_output(self._apply_registration(result, registration), definition)

    async def execute_agent_loop(self, request: ExecutionRequest, *, loop_correlation: dict) -> ExecutionResult:
        """受信 Loop 评测入口：policy 来自注册定义而非请求正文。"""
        from core.runtime.agent_loop.contracts import LOOP_POLICY
        _, definition, _, _, _ = self._resolve_request(request)
        if definition.business_options.get("execution_policy") != LOOP_POLICY:
            raise AgentApplicationError("LOOP_POLICY_REQUIRED", "Agent 未启用 durable Loop 策略")
        return await self.execute_evaluation(request, execution_policy=LOOP_POLICY,
                                             loop_correlation=loop_correlation)


__all__ = ["AgentApplicationError", "AgentApplicationService", "ExecutionEvent", "ExecutionRequest", "ExecutionResult"]
