#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""聊天应用服务层。"""

from typing import Generator, Optional

from core.agent_router import AgentRouter


class ChatService:
    """对外暴露聊天、历史和记忆管理操作。"""

    def __init__(self, router: AgentRouter) -> None:
        """初始化应用服务。

        Args:
            router: 负责路由、工具和记忆协调的核心对象。
        """
        self.router = router

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
        yield from self.router.chat_stream(user_query=final_query, agent_id=agent_id)

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
        return self.router.memory_manager.search_messages(keyword)

    def get_all_memory(self) -> list[dict]:
        """返回记忆管理界面使用的消息集合。"""
        return self.router.memory_manager.get_all_messages()

    def delete_memory(self, message_ids: Optional[list[int]] = None, delete_all: bool = False) -> None:
        """删除指定消息或清空全部记忆。"""
        if delete_all:
            self.router.memory_manager.clear_all_memory()
            return
        self.router.memory_manager.delete_messages(message_ids or [])
