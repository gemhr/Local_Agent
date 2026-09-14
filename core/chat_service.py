#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""聊天应用服务层。"""

import asyncio
from collections.abc import AsyncIterator, Callable
import math
from typing import Any, Optional

from core.agent_router import AgentRouter
from core.runtime.metrics import ApplicationRuntimeGaugeProvider
from core.runtime.observability_dispatcher import RuntimeObservabilityDispatcher
from core.runtime import (
    AgentState,
    RunBudget,
    CancellationReason,
    process_run_registry,
    RunCoordinatorResult,
    OutputDeltaPayload,
    RuntimeEvent,
    RunEventJournal,
    ChatStreamCompatibilityAdapter,
    ChatStreamProtocolError,
    safe_transport_error_chunk,
    CoordinatedRuntimeFactory,
    RuntimeAdmissionGate,
    RuntimeAdmissionRejectedError,
    FaultInjectionController,
    ProjectIdentity,
    ProjectMemoryGrant,
)


class ChatRuntimeTransportError(RuntimeError):
    """A path-free failure raised by the coordinated transport boundary."""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


class ChatService:
    """对外暴露聊天、历史和记忆管理操作。"""

    def __init__(
        self,
        router: AgentRouter,
        state_observer: Callable[[AgentState], None] | None = None,
        event_journal: RunEventJournal | None = None,
        observability_dispatcher: RuntimeObservabilityDispatcher | None = None,
        gauge_provider: ApplicationRuntimeGaugeProvider | None = None,
        coordinated_runtime_factory: CoordinatedRuntimeFactory | None = None,
        run_registry=None,
        admission_gate: RuntimeAdmissionGate | None = None,
        disconnect_grace_seconds: float = 1.0,
    ) -> None:
        """初始化应用服务。

        Args:
            router: 负责路由、工具和记忆协调的核心对象。
            state_observer: 用于临时 AgentState 快照的可选测试或诊断回调。
        """
        self.router = router
        self._state_observer = state_observer
        self._event_journal = event_journal
        self._observability_dispatcher = observability_dispatcher
        self._gauge_provider = gauge_provider
        if coordinated_runtime_factory is not None and not isinstance(
            coordinated_runtime_factory, CoordinatedRuntimeFactory
        ):
            raise TypeError(
                "coordinated_runtime_factory must be CoordinatedRuntimeFactory"
            )
        self._coordinated_runtime_factory = coordinated_runtime_factory
        self._run_registry = run_registry or process_run_registry
        factory_gate = (
            coordinated_runtime_factory.services.admission_gate
            if coordinated_runtime_factory is not None
            else None
        )
        self._admission_gate = (
            admission_gate or factory_gate or RuntimeAdmissionGate()
        )
        if (
            isinstance(disconnect_grace_seconds, bool)
            or not isinstance(disconnect_grace_seconds, (int, float))
            or not math.isfinite(float(disconnect_grace_seconds))
            or float(disconnect_grace_seconds) < 0
        ):
            raise ValueError(
                "disconnect_grace_seconds must be finite and non-negative"
            )
        self._disconnect_grace_seconds = float(disconnect_grace_seconds)

    @property
    def run_registry(self):
        return self._run_registry

    @property
    def admission_gate(self) -> RuntimeAdmissionGate:
        return self._admission_gate

    def _observe_state(self, agent_state: AgentState) -> None:
        """通知可选观察者，但不在服务对象上存储 AgentState。"""
        if self._state_observer is not None:
            self._state_observer(agent_state)

    async def run_coordinated_agent(
        self,
        agent_id: str,
        query: str,
        *,
        run_id: str | None = None,
        timeout_seconds: float | None = None,
        budget: RunBudget | None = None,
        persist: bool = True,
        retrieval_cache_authz_domain: str | None = None,
    ) -> tuple[str | None, RunCoordinatorResult]:
        """通过 RunCoordinator 执行一条真实的非流式单 Agent 路径。

        所有聊天请求均通过 Coordinated Runtime；调用方不得绕过其生命周期。
        """
        events: list[RuntimeEvent] = []
        results: list[RunCoordinatorResult] = []
        async for event in self.stream_coordinated_agent_events(
            agent_id,
            query,
            run_id=run_id,
            timeout_seconds=timeout_seconds,
            budget=budget,
            persist=persist,
            retrieval_cache_authz_domain=retrieval_cache_authz_domain,
            _result_out=results,
        ):
            events.append(event)
        output = "".join(
            event.payload.text
            for event in events
            if isinstance(event.payload, OutputDeltaPayload)
        )
        if not results:
            raise RuntimeError("Coordinated Runtime 未返回结构化结果")
        result = results[0]
        return output if result.status.value == "SUCCEEDED" else None, result

    async def run_coordinated_agent_evaluation(
        self,
        agent_id: str,
        query: str,
        *,
        run_id: str | None = None,
        timeout_seconds: float | None = None,
        budget: RunBudget | None = None,
        persist: bool = True,
        fault_controller=None,
        episodic_evaluation_observer=None,
        evaluation_plan_resolver=None,
        project_identity: ProjectIdentity | None = None,
        project_grants: tuple[ProjectMemoryGrant, ...] = (),
    ) -> tuple[str | None, RunCoordinatorResult]:
        """Isolated evaluation-only entry mirroring ``run_coordinated_agent``.

        ``fault_controller`` and ``episodic_evaluation_observer`` are only ever
        provided by the isolated evaluation execution path through its strict
        typed control.  The normal production path never calls this method and
        never supplies either seam.
        """
        events: list[RuntimeEvent] = []
        results: list[RunCoordinatorResult] = []
        async for event in self.stream_coordinated_agent_events_evaluation(
            agent_id,
            query,
            run_id=run_id,
            timeout_seconds=timeout_seconds,
            budget=budget,
            persist=persist,
            fault_controller=fault_controller,
            episodic_evaluation_observer=episodic_evaluation_observer,
            evaluation_plan_resolver=evaluation_plan_resolver,
            project_identity=project_identity,
            project_grants=project_grants,
            _result_out=results,
        ):
            events.append(event)
        output = "".join(
            event.payload.text
            for event in events
            if isinstance(event.payload, OutputDeltaPayload)
        )
        if not results:
            raise RuntimeError("Coordinated Runtime 未返回结构化结果")
        result = results[0]
        return output if result.status.value == "SUCCEEDED" else None, result

    async def stream_coordinated_agent_events(
        self,
        agent_id: str,
        query: str,
        *,
        run_id: str | None = None,
        timeout_seconds: float | None = None,
        budget: RunBudget | None = None,
        persist: bool = True,
        _result_out: list[RunCoordinatorResult] | None = None,
        _cancellation_intent: list[CancellationReason] | None = None,
        project_identity: ProjectIdentity | None = None,
        project_grants: tuple[ProjectMemoryGrant, ...] = (),
        retrieval_cache_authz_domain: str | None = None,
    ) -> AsyncIterator[RuntimeEvent]:
        """以 Producer Task + 单 Consumer Channel 暴露真实 Coordinated 事件流。"""
        if self._coordinated_runtime_factory is None:
            raise ChatRuntimeTransportError(
                "RUNTIME_CONFIGURATION_ERROR"
            ) from None
        events = self._stream_factory_coordinated_events(
            agent_id,
            query,
            run_id=run_id,
            timeout_seconds=timeout_seconds,
            budget=budget,
            persist=persist,
            result_out=_result_out,
            cancellation_intent=_cancellation_intent,
            project_identity=project_identity,
            project_grants=project_grants,
            retrieval_cache_authz_domain=retrieval_cache_authz_domain,
        )
        try:
            async for event in events:
                yield event
        finally:
            await events.aclose()

    async def _create_coordinated_scope(
        self,
        agent_id: str,
        query: str,
        *,
        run_id: str | None,
        timeout_seconds: float | None,
        budget: RunBudget | None,
        persist: bool,
        fault_controller: FaultInjectionController | None = None,
        episodic_evaluation_observer=None,
        evaluation_plan_resolver=None,
        project_identity: ProjectIdentity | None = None,
        project_grants: tuple[ProjectMemoryGrant, ...] = (),
        retrieval_cache_authz_domain: str | None = None,
    ):
        factory = self._coordinated_runtime_factory
        if factory is None:
            raise ChatRuntimeTransportError(
                "RUNTIME_CONFIGURATION_ERROR"
            ) from None
        try:
            scope = await factory.create_run_scope(
                agent_id,
                query,
                run_id=run_id,
                timeout_seconds=timeout_seconds,
                budget=budget,
                persist=persist,
                fault_controller=fault_controller,
                episodic_evaluation_observer=episodic_evaluation_observer,
                evaluation_plan_resolver=evaluation_plan_resolver,
                project_identity=project_identity,
                project_grants=project_grants,
            )
            scope.run_context.attach_retrieval_cache_access(
                retrieval_cache_authz_domain
            )
            return scope
        except asyncio.CancelledError:
            raise
        except RuntimeAdmissionRejectedError:
            raise ChatRuntimeTransportError(
                "RUNTIME_SHUTTING_DOWN"
            ) from None
        except Exception:
            raise ChatRuntimeTransportError(
                "RUNTIME_SCOPE_CREATION_FAILED"
            ) from None

    async def _stream_factory_coordinated_events(
        self,
        agent_id: str,
        query: str,
        *,
        run_id: str | None,
        timeout_seconds: float | None,
        budget: RunBudget | None,
        persist: bool,
        result_out: list[RunCoordinatorResult] | None,
        cancellation_intent: list[CancellationReason] | None,
        project_identity: ProjectIdentity | None = None,
        project_grants: tuple[ProjectMemoryGrant, ...] = (),
        retrieval_cache_authz_domain: str | None = None,
    ) -> AsyncIterator[RuntimeEvent]:
        """Run the sole production coordinated event path through the factory.

        The production path never supplies a fault controller or any isolated
        evaluation observer; those seams exist only on the explicit evaluation
        entry ``stream_coordinated_agent_events_evaluation``.
        """
        scope = await self._create_coordinated_scope(
            agent_id,
            query,
            run_id=run_id,
            timeout_seconds=timeout_seconds,
            budget=budget,
            persist=persist,
            project_identity=project_identity,
            project_grants=project_grants,
            retrieval_cache_authz_domain=retrieval_cache_authz_domain,
        )
        consumer = self._consume_scope_events(
            scope,
            result_out=result_out,
            cancellation_intent=cancellation_intent,
        )
        try:
            async for event in consumer:
                yield event
        finally:
            await consumer.aclose()

    async def _stream_factory_coordinated_events_evaluation(
        self,
        agent_id: str,
        query: str,
        *,
        run_id: str | None,
        timeout_seconds: float | None,
        budget: RunBudget | None,
        persist: bool,
        fault_controller: FaultInjectionController | None,
        episodic_evaluation_observer=None,
        evaluation_plan_resolver=None,
        result_out: list[RunCoordinatorResult] | None,
        cancellation_intent: list[CancellationReason] | None,
        project_identity: ProjectIdentity | None = None,
        project_grants: tuple[ProjectMemoryGrant, ...] = (),
    ) -> AsyncIterator[RuntimeEvent]:
        """Isolated evaluation-only factory path (strict typed control only)."""
        scope = await self._create_coordinated_scope(
            agent_id,
            query,
            run_id=run_id,
            timeout_seconds=timeout_seconds,
            budget=budget,
            persist=persist,
            fault_controller=fault_controller,
            episodic_evaluation_observer=episodic_evaluation_observer,
            evaluation_plan_resolver=evaluation_plan_resolver,
            project_identity=project_identity,
            project_grants=project_grants,
        )
        consumer = self._consume_scope_events(
            scope,
            result_out=result_out,
            cancellation_intent=cancellation_intent,
        )
        try:
            async for event in consumer:
                yield event
        finally:
            await consumer.aclose()

    async def stream_coordinated_agent_events_evaluation(
        self,
        agent_id: str,
        query: str,
        *,
        run_id: str | None = None,
        timeout_seconds: float | None = None,
        budget: RunBudget | None = None,
        persist: bool = True,
        fault_controller: FaultInjectionController | None = None,
        episodic_evaluation_observer=None,
        evaluation_plan_resolver=None,
        _result_out: list[RunCoordinatorResult] | None = None,
        _cancellation_intent: list[CancellationReason] | None = None,
        project_identity: ProjectIdentity | None = None,
        project_grants: tuple[ProjectMemoryGrant, ...] = (),
    ) -> AsyncIterator[RuntimeEvent]:
        """Isolated evaluation-only event stream mirroring the production path."""
        events = self._stream_factory_coordinated_events_evaluation(
            agent_id,
            query,
            run_id=run_id,
            timeout_seconds=timeout_seconds,
            budget=budget,
            persist=persist,
            fault_controller=fault_controller,
            episodic_evaluation_observer=episodic_evaluation_observer,
            evaluation_plan_resolver=evaluation_plan_resolver,
            result_out=_result_out,
            cancellation_intent=_cancellation_intent,
            project_identity=project_identity,
            project_grants=project_grants,
        )
        try:
            async for event in events:
                yield event
        finally:
            await events.aclose()

    async def _consume_scope_events(
        self,
        scope,
        *,
        result_out: list[RunCoordinatorResult] | None,
        cancellation_intent: list[CancellationReason] | None,
    ) -> AsyncIterator[RuntimeEvent]:
        """Shared producer-task + single-consumer channel lifecycle."""

        async def produce() -> None:
            try:
                result = await scope.execute()
                if result_out is not None:
                    result_out.append(result)
                self._observe_state(scope.agent_state)
            finally:
                await scope.event_channel.close()

        producer_task = asyncio.create_task(produce())
        scope.bind_producer_task(producer_task)
        completed = False
        transport_completed = False
        transport_consumer = scope.event_channel.__aiter__()
        cancel_reason = (
            cancellation_intent[0]
            if cancellation_intent is not None
            else CancellationReason.CLIENT_DISCONNECTED
        )
        try:
            async for event in transport_consumer:
                yield event
            transport_completed = True
            await producer_task
            completed = True
        except GeneratorExit:
            cancel_reason = (
                cancellation_intent[0]
                if cancellation_intent is not None
                else CancellationReason.CLIENT_DISCONNECTED
            )
            scope.request_cancel(cancel_reason)
            raise
        except asyncio.CancelledError:
            cancel_reason = CancellationReason.CLIENT_DISCONNECTED
            scope.request_cancel(cancel_reason)
            raise
        finally:
            close_consumer = getattr(transport_consumer, "aclose", None)
            if callable(close_consumer):
                await close_consumer()
            if completed or transport_completed:
                await scope.close()
            elif not scope.is_closed:
                scope.request_cancel(cancel_reason)
                await self._cancel_and_drain_scope(scope, cancel_reason)

    async def _cancel_and_drain_scope(
        self,
        scope,
        reason: CancellationReason,
    ) -> None:
        """Boundedly shield only the minimum request-owned cleanup."""

        if reason is CancellationReason.STREAM_ENCODING_FAILED:
            await scope.force_abort(reason)
            return

        async def cleanup() -> None:
            drained = await scope.drain_and_close(
                self._disconnect_grace_seconds
            )
            if not drained:
                await scope.force_abort(reason)

        cleanup_task = asyncio.create_task(cleanup())
        try:
            await asyncio.wait_for(
                asyncio.shield(cleanup_task),
                timeout=self._disconnect_grace_seconds + 0.25,
            )
        except TimeoutError:
            await scope.force_abort(reason)
        finally:
            if not cleanup_task.done():
                cleanup_task.cancel()
            await asyncio.gather(cleanup_task, return_exceptions=True)

    async def stream_coordinated_agent_text(
        self,
        agent_id: str,
        query: str,
        *,
        run_id: str | None = None,
        budget: RunBudget | None = None,
        persist: bool = True,
        retrieval_cache_authz_domain: str | None = None,
    ) -> AsyncIterator[str]:
        """通过唯一 Transport Adapter 输出当前自定义纯文本分块协议。"""
        adapter = ChatStreamCompatibilityAdapter()
        cancellation_intent = [CancellationReason.CLIENT_DISCONNECTED]
        events = self.stream_coordinated_agent_events(
            agent_id,
            query,
            run_id=run_id,
            budget=budget,
            persist=persist,
            retrieval_cache_authz_domain=retrieval_cache_authz_domain,
            _cancellation_intent=cancellation_intent,
        )
        try:
            async for event in events:
                try:
                    chunk = adapter.adapt(event)
                except ChatStreamProtocolError as exc:
                    cancellation_intent[0] = (
                        CancellationReason.STREAM_ENCODING_FAILED
                    )
                    yield safe_transport_error_chunk(exc.error_code).text
                    return
                if chunk is not None:
                    yield chunk.text
        except ChatRuntimeTransportError as exc:
            yield safe_transport_error_chunk(exc.error_code).text
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            yield safe_transport_error_chunk(
                "RUNTIME_EXECUTION_FAILED"
            ).text
            return
        else:
            final_chunk = adapter.finish()
            if final_chunk is not None:
                yield final_chunk.text
        finally:
            await events.aclose()

    async def get_history(self, agent_id: str, limit: int, offset: int) -> list[dict]:
        """返回按显示顺序排列的一页历史消息。"""
        records = await self._memory_call(
            "get_chat_history",
            agent_id=agent_id,
            limit=limit,
            offset=offset,
            ascending=False,
        )
        return list(reversed(records))

    async def search_memory(self, keyword: str) -> list[dict]:
        """搜索持久化消息。"""
        return await self._memory_call(
            "search_messages",
            keyword,
            memory_scope=self.router.DIRECT_MEMORY_SCOPE,
        )

    async def get_all_memory(self) -> dict[str, list[dict[str, Any]]]:
        """返回记忆管理界面使用的完整记忆快照。"""
        return {
            "messages": await self._memory_call("get_all_messages"),
            "summaries": await self._memory_call("get_all_summaries"),
        }

    async def delete_memory(
        self,
        message_ids: Optional[list[int]] = None,
        delete_all: bool = False,
    ) -> dict[str, Any]:
        """删除指定消息或清空全部记忆。"""
        if delete_all:
            await self._memory_call("clear_all_memory")
            return {
                "status": "success",
                "affected_agent_ids": list(self.router.agents_config.keys()),
                "refresh_agent_ids": list(self.router.agents_config.keys()),
                "delete_all": True,
            }
        result = await self._memory_call("delete_messages", message_ids or [])
        result["status"] = "success"
        result["delete_all"] = False
        return result

    async def _memory_call(self, method_name: str, *args, **kwargs):
        """调用应用级异步 PG store；仅为显式测试装配兼容同步 fake。"""
        manager = self.router.memory_manager
        async_store = getattr(manager, "async_store", None)
        owner = async_store if async_store is not None else manager
        method = getattr(owner, method_name)
        result = method(*args, **kwargs)
        if hasattr(result, "__await__"):
            return await result
        return await asyncio.to_thread(lambda: result)
