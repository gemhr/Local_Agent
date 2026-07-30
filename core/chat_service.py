#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""聊天应用服务层。"""

import asyncio
from collections.abc import AsyncIterator, Callable
import threading
from typing import Any, Generator, Optional

from core.agent_router import AgentRouter
from core.runtime.metrics import ApplicationRuntimeGaugeProvider
from core.runtime.observability_dispatcher import RuntimeObservabilityDispatcher
from core.runtime import (
    AgentLoop,
    AgentState,
    LEGACY_DEFAULT_SESSION_ID,
    LEGACY_AGENT_ROUTER_STEP_ID,
    LegacyAgentRouterDriver,
    create_run_context,
    BudgetLedger,
    RunBudget,
    CancellationReason,
    RunHandle,
    process_run_registry,
    RunCoordinatorResult,
    OutputDeltaPayload,
    RuntimeEvent,
    RunEventJournal,
    ChatStreamCompatibilityAdapter,
    ChatStreamProtocolError,
    safe_transport_error_chunk,
    ChatRuntimeMode,
    ChatRuntimeSelector,
    CoordinatedRuntimeFactory,
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
        event_channel_capacity: int = 32,
        event_journal: RunEventJournal | None = None,
        observability_dispatcher: RuntimeObservabilityDispatcher | None = None,
        gauge_provider: ApplicationRuntimeGaugeProvider | None = None,
        runtime_selector: ChatRuntimeSelector | None = None,
        coordinated_runtime_factory: CoordinatedRuntimeFactory | None = None,
        run_registry=None,
    ) -> None:
        """初始化应用服务。

        Args:
            router: 负责路由、工具和记忆协调的核心对象。
            state_observer: 用于临时 AgentState 快照的可选测试或诊断回调。
        """
        self.router = router
        self._state_observer = state_observer
        if (
            isinstance(event_channel_capacity, bool)
            or not isinstance(event_channel_capacity, int)
            or event_channel_capacity <= 0
        ):
            raise ValueError("event_channel_capacity 必须是正整数")
        self._event_channel_capacity = event_channel_capacity
        self._event_journal = event_journal
        self._observability_dispatcher = observability_dispatcher
        self._gauge_provider = gauge_provider
        self._runtime_selector = runtime_selector or ChatRuntimeSelector(
            ChatRuntimeMode.COORDINATED
        )
        if not isinstance(self._runtime_selector, ChatRuntimeSelector):
            raise TypeError("runtime_selector must be ChatRuntimeSelector")
        if coordinated_runtime_factory is not None and not isinstance(
            coordinated_runtime_factory, CoordinatedRuntimeFactory
        ):
            raise TypeError(
                "coordinated_runtime_factory must be CoordinatedRuntimeFactory"
            )
        self._coordinated_runtime_factory = coordinated_runtime_factory
        self._run_registry = run_registry or process_run_registry

    @property
    def run_registry(self):
        return self._run_registry

    def selected_runtime_mode(self) -> ChatRuntimeMode:
        """Capture the configured enum once at the request boundary."""
        return self._runtime_selector.selected_runtime_mode()

    def stream_chat(self, agent_id: str, query: str, file_path: str = "", run_id: str | None = None) -> Generator[str, None, None]:
        """流式执行一次对话。

        Args:
            agent_id: 智能体标识。
            query: 用户输入文本。
            file_path: 可选附件路径。

        Yields:
            str: 助手增量输出。
        """
        final_query = query
        if file_path:
            final_query += f"\n\nPlease analyze this file path: '{file_path}'"
        run_context, cancellation_source = create_run_context(
            entry_agent_id=agent_id,
            session_id=LEGACY_DEFAULT_SESSION_ID,
            run_id=run_id,
        )
        # 在此生成器栈帧中保留取消源，避免丢失取消控制权。
        _cancellation_source = cancellation_source
        # ChatService 是当前 Legacy 主链路的 Parent Runtime，单 Run 创建并持有账本。
        run_context.attach_budget_ledger(BudgetLedger(RunBudget(), deadline_remaining=run_context.remaining_seconds))
        agent_state = AgentState.for_run_context(run_context.run_id)
        self._run_registry.register(
            RunHandle(
                run_context.run_id,
                cancellation_source,
                agent_state,
                "chat_service",
            )
        )
        driver = LegacyAgentRouterDriver(self.router, user_query=final_query, agent_id=agent_id)
        loop = AgentLoop()
        deadline_timer: threading.Timer | None = None
        remaining = run_context.remaining_seconds()
        if remaining is not None:
            # Timer 只属于本 Run，finally 一定取消；不创建永久后台任务。
            deadline_timer = threading.Timer(remaining, cancellation_source.cancel, args=(CancellationReason.DEADLINE_EXCEEDED,))
            deadline_timer.daemon = True
            deadline_timer.start()
        try:
            yield from loop.run_stream(
                run_context=run_context,
                agent_state=agent_state,
                driver=driver,
                state_observer=self._observe_state,
            )
        except GeneratorExit:
            cancellation_source.cancel(CancellationReason.CLIENT_DISCONNECTED)
            raise
        finally:
            if deadline_timer is not None:
                deadline_timer.cancel()
            self._run_registry.unregister(run_context.run_id)
            _ = _cancellation_source

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
        budget: RunBudget | None = None,
        persist: bool = True,
    ) -> tuple[str | None, RunCoordinatorResult]:
        """通过 RunCoordinator 执行一条真实的非流式单 Agent 路径。

        默认 ``stream_chat`` 继续由 Legacy AgentLoop 持有生命周期；调用方必须
        二选一，不能让同一个 run_id 同时进入 Legacy 与 Coordinated 路径。
        """
        events: list[RuntimeEvent] = []
        results: list[RunCoordinatorResult] = []
        async for event in self.stream_coordinated_agent_events(
            agent_id,
            query,
            run_id=run_id,
            budget=budget,
            persist=persist,
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
        budget: RunBudget | None = None,
        persist: bool = True,
        _result_out: list[RunCoordinatorResult] | None = None,
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
            budget=budget,
            persist=persist,
            result_out=_result_out,
        )
        try:
            async for event in events:
                yield event
        finally:
            await events.aclose()

    async def _stream_factory_coordinated_events(
        self,
        agent_id: str,
        query: str,
        *,
        run_id: str | None,
        budget: RunBudget | None,
        persist: bool,
        result_out: list[RunCoordinatorResult] | None,
    ) -> AsyncIterator[RuntimeEvent]:
        """Run the sole production coordinated event path through the factory."""
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
                budget=budget,
                persist=persist,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ChatRuntimeTransportError(
                "RUNTIME_SCOPE_CREATION_FAILED"
            ) from None

        async def produce() -> None:
            try:
                result = await scope.execute()
                if result_out is not None:
                    result_out.append(result)
                self._observe_state(scope.agent_state)
            finally:
                await scope.event_channel.close()

        producer_task = asyncio.create_task(produce())
        completed = False
        try:
            async for event in scope.event_channel:
                yield event
            await producer_task
            completed = True
        except GeneratorExit:
            scope.cancellation_source.cancel(
                CancellationReason.CLIENT_DISCONNECTED
            )
            await scope.close(abort=True)
            producer_task.cancel()
            await asyncio.gather(producer_task, return_exceptions=True)
            raise
        except asyncio.CancelledError:
            scope.cancellation_source.cancel(
                CancellationReason.CLIENT_DISCONNECTED
            )
            await scope.close(abort=True)
            producer_task.cancel()
            await asyncio.gather(producer_task, return_exceptions=True)
            raise
        finally:
            if completed:
                await scope.close()
            else:
                scope.cancellation_source.cancel(
                    CancellationReason.CLIENT_DISCONNECTED
                )
                await scope.close(abort=True)
                if not producer_task.done():
                    producer_task.cancel()
                await asyncio.gather(producer_task, return_exceptions=True)

    async def stream_coordinated_agent_text(
        self,
        agent_id: str,
        query: str,
        *,
        run_id: str | None = None,
        budget: RunBudget | None = None,
        persist: bool = True,
    ) -> AsyncIterator[str]:
        """通过唯一 Transport Adapter 输出当前自定义纯文本分块协议。"""
        adapter = ChatStreamCompatibilityAdapter()
        events = self.stream_coordinated_agent_events(
            agent_id,
            query,
            run_id=run_id,
            budget=budget,
            persist=persist,
        )
        try:
            async for event in events:
                try:
                    chunk = adapter.adapt(event)
                except ChatStreamProtocolError as exc:
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

    def get_history(self, agent_id: str, limit: int, offset: int) -> list[dict]:
        """返回按显示顺序排列的一页历史消息。"""
        records = self.router.memory_manager.get_chat_history(
            agent_id=agent_id,
            limit=limit,
            offset=offset,
            ascending=False,
        )
        return list(reversed(records))

    def search_memory(self, keyword: str) -> list[dict]:
        """搜索持久化消息。"""
        return self.router.memory_manager.search_messages(
            keyword,
            memory_scope=self.router.DIRECT_MEMORY_SCOPE,
        )

    def get_all_memory(self) -> dict[str, list[dict[str, Any]]]:
        """返回记忆管理界面使用的完整记忆快照。"""
        return {
            "messages": self.router.memory_manager.get_all_messages(),
            "summaries": self.router.memory_manager.get_all_summaries(),
        }

    def delete_memory(
        self,
        message_ids: Optional[list[int]] = None,
        delete_all: bool = False,
    ) -> dict[str, Any]:
        """删除指定消息或清空全部记忆。"""
        if delete_all:
            self.router.memory_manager.clear_all_memory()
            return {
                "status": "success",
                "affected_agent_ids": list(self.router.agents_config.keys()),
                "refresh_agent_ids": list(self.router.agents_config.keys()),
                "delete_all": True,
            }
        result = self.router.memory_manager.delete_messages(message_ids or [])
        result["status"] = "success"
        result["delete_all"] = False
        return result
