#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""聊天应用服务层。"""

import logging
from collections.abc import Callable
from typing import Any, Generator, Optional

from core.agent_router import AgentRouter
from core.runtime import (
    AgentState,
    LEGACY_DEFAULT_SESSION_ID,
    RunCancelledError,
    RunDeadlineExceededError,
    StopReason,
    create_run_context,
)

LEGACY_AGENT_ROUTER_STEP_ID = "legacy-agent-router"
LEGACY_AGENT_ROUTER_STEP_NAME = "Legacy AgentRouter execution"

logger = logging.getLogger(__name__)


class ChatService:
    """对外暴露聊天、历史和记忆管理操作。"""

    def __init__(
        self,
        router: AgentRouter,
        state_observer: Callable[[AgentState], None] | None = None,
    ) -> None:
        """初始化应用服务。

        Args:
            router: 负责路由、工具和记忆协调的核心对象。
            state_observer: Optional test/diagnostic callback for ephemeral AgentState snapshots.
        """
        self.router = router
        self._state_observer = state_observer

    def stream_chat(self, agent_id: str, query: str, file_path: str = "") -> Generator[str, None, None]:
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
        )
        # Keep the source in this generator frame so cancellation authority is not lost.
        _cancellation_source = cancellation_source
        agent_state = AgentState.for_run_context(run_context.run_id)
        agent_state.assert_matches_run_context(run_context.run_id)
        agent_state.add_step(LEGACY_AGENT_ROUTER_STEP_ID, LEGACY_AGENT_ROUTER_STEP_NAME)
        agent_state.mark_running()
        agent_state.start_step(LEGACY_AGENT_ROUTER_STEP_ID)
        self._observe_state(agent_state)
        output_chunks: list[str] = []
        try:
            for chunk in self.router.chat_stream(
                user_query=final_query,
                agent_id=agent_id,
                run_context=run_context,
            ):
                output_chunks.append(chunk)
                yield chunk
            final_output = "".join(output_chunks) if output_chunks else None
            agent_state.succeed_step(LEGACY_AGENT_ROUTER_STEP_ID)
            agent_state.mark_succeeded(final_output=final_output)
            self._observe_state(agent_state)
            logger.info("AgentState final", extra={"agent_state": agent_state.to_dict()})
        except RunDeadlineExceededError:
            agent_state.fail_step(
                LEGACY_AGENT_ROUTER_STEP_ID,
                error_code="DEADLINE_EXCEEDED",
                error_message="Run deadline exceeded",
            )
            agent_state.mark_failed(
                stop_reason=StopReason.DEADLINE_EXCEEDED,
                error_code="DEADLINE_EXCEEDED",
                error_message="Run deadline exceeded",
            )
            self._observe_state(agent_state)
            logger.info("AgentState final", extra={"agent_state": agent_state.to_dict()})
            raise
        except RunCancelledError:
            agent_state.cancel_step(
                LEGACY_AGENT_ROUTER_STEP_ID,
                error_code="USER_CANCELLED",
                error_message="Run cancelled",
            )
            agent_state.mark_cancelled(
                stop_reason=StopReason.USER_CANCELLED,
                error_code="USER_CANCELLED",
                error_message="Run cancelled",
            )
            self._observe_state(agent_state)
            logger.info("AgentState final", extra={"agent_state": agent_state.to_dict()})
            raise
        except Exception:
            agent_state.fail_step(
                LEGACY_AGENT_ROUTER_STEP_ID,
                error_code="UNHANDLED_ERROR",
                error_message="Agent execution failed",
            )
            agent_state.mark_failed(
                stop_reason=StopReason.UNHANDLED_ERROR,
                error_code="UNHANDLED_ERROR",
                error_message="Agent execution failed",
            )
            self._observe_state(agent_state)
            logger.exception("AgentRouter execution failed")
            logger.info("AgentState final", extra={"agent_state": agent_state.to_dict()})
            raise
        finally:
            _ = _cancellation_source

    def _observe_state(self, agent_state: AgentState) -> None:
        """Notify an optional observer without storing AgentState on the service."""
        if self._state_observer is not None:
            self._state_observer(agent_state)

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

