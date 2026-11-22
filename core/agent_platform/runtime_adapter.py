"""应用层使用的 Run 级执行合同及现有原生 Runtime 的适配。"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol

from core.runtime.events import OutputDeltaPayload, RuntimeEventType
from core.runtime.state import RunStatus, StopReason
from core.runtime.stream_adapter import ChatStreamChunkKind, ChatStreamCompatibilityAdapter

if TYPE_CHECKING:
    from core.chat_service import ChatService
    from core.runtime.memory_scope import MemoryAccessContext
    from core.runtime.run_coordinator import RunCoordinatorResult


@dataclass(frozen=True, slots=True)
class RuntimeRequest:
    """通过业务校验的 Run 输入。Run ID 同时用于既有取消控制。"""

    agent_id: str
    input: str
    agent_version: str
    workflow_id: str | None = None
    workflow_version: str | None = None
    run_id: str | None = None
    session_id: str | None = None
    timeout_seconds: float | None = None
    retrieval_cache_authz_domain: str | None = None
    memory_access_context: MemoryAccessContext | None = None
    runtime_instance_id: str | None = None
    require_new_run: bool = False


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
    external_assistant_id: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeUpdate:
    kind: Literal["started", "output_delta", "control_delta"]
    run_id: str
    trace_id: str
    text: str | None = None


class RuntimeAdapterError(RuntimeError):
    """执行流无法提供可信结果时使用的安全错误。"""

    def __init__(self, error_code: str, safe_message: str) -> None:
        self.error_code = error_code
        self.safe_message = safe_message
        super().__init__(f"{safe_message} (error_code={error_code})")


class RuntimeAdapter(Protocol):
    async def execute(self, request: RuntimeRequest) -> ExecutionResult:
        """等待单个 Run 的最终结果。"""
        ...

    def stream(self, request: RuntimeRequest) -> AsyncGenerator[RuntimeUpdate | ExecutionResult, None]:
        """单消费者执行流。完成时产出最终结果，提前关闭时清理本次执行。"""
        ...


class NativeRuntimeAdapter:
    def __init__(self, chat_service: ChatService) -> None:
        self._chat_service = chat_service

    @staticmethod
    def _chat_kwargs(request: RuntimeRequest) -> dict:
        return {
            "run_id": request.run_id,
            "session_id": request.session_id,
            "timeout_seconds": request.timeout_seconds,
            "agent_version": request.agent_version,
            "workflow_id": request.workflow_id,
            "workflow_version": request.workflow_version,
            "retrieval_cache_authz_domain": request.retrieval_cache_authz_domain,
            "memory_access_context": request.memory_access_context,
        }

    async def execute(self, request: RuntimeRequest) -> ExecutionResult:
        return await self.execute_trusted(request)

    async def execute_trusted(
        self, request: RuntimeRequest, *, adaptive_route: dict | None = None, single_call: bool = False,
    ) -> ExecutionResult:
        kwargs = self._chat_kwargs(request)
        if single_call:
            from core.runtime.budget import RunBudget

            kwargs["persist"] = False
            kwargs["budget"] = RunBudget(
                max_model_calls=1, max_remote_model_calls=1, max_retries=0,
                max_step_starts=1, max_tool_calls=8, max_concurrency=1,
                max_elapsed_seconds=min(90, request.timeout_seconds or 90),
            )
        elif adaptive_route is not None:
            kwargs["adaptive_route"] = adaptive_route
        output, result = await self._chat_service.run_coordinated_agent(
            request.agent_id, request.input, **kwargs,
        )
        return self.project_result(result, output)

    async def stream(self, request: RuntimeRequest) -> AsyncGenerator[RuntimeUpdate | ExecutionResult, None]:
        results: list[RunCoordinatorResult] = []
        output_parts: list[str] = []
        wire_adapter = ChatStreamCompatibilityAdapter()
        started = False
        terminal_control = None
        async with aclosing(self._chat_service.stream_coordinated_agent_events(
            request.agent_id, request.input, _result_out=results, **self._chat_kwargs(request),
            **({"require_new_run": True} if request.require_new_run else {}),
        )) as events:
            async for event in events:
                chunk = wire_adapter.adapt(event)
                if event.event_type is RuntimeEventType.RUN_STARTED and not started:
                    started = True
                    yield RuntimeUpdate("started", event.run_id, event.trace_id)
                if chunk is not None and chunk.kind is ChatStreamChunkKind.CONTROL:
                    control = RuntimeUpdate("control_delta", event.run_id, event.trace_id, chunk.text)
                    if event.event_type is RuntimeEventType.RUN_COMPLETED:
                        terminal_control = control
                    else:
                        yield control
                if isinstance(event.payload, OutputDeltaPayload):
                    output_parts.append(event.payload.text)
                    yield RuntimeUpdate("output_delta", event.run_id, event.trace_id, event.payload.text)
        if not results:
            raise RuntimeAdapterError("RUNTIME_RESULT_MISSING", "Runtime 未返回结构化结果")
        if wire_adapter.finish() is not None:
            raise RuntimeAdapterError("RUNTIME_TERMINAL_MISSING", "Runtime 事件流缺少终态")
        if terminal_control is not None:
            yield terminal_control
        yield self.project_result(results[0], "".join(output_parts))

    @staticmethod
    def project_result(runtime_result: RunCoordinatorResult, output: str | None) -> ExecutionResult:
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
            error_code=runtime_result.error_code,
            safe_message=runtime_result.safe_message,
            output=output if runtime_result.status is RunStatus.SUCCEEDED else None,
        )
