#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""MCP 集成组件（APPLICATION_SCOPE，``server.py::lifespan()`` 拥有）。

Owner 合同（WP0 Decision §6）：

- ``MCP_CLIENT_OWNER = APPLICATION_SCOPE_MCP_INTEGRATION_COMPONENT_
  CREATED_BY_SERVER_LIFESPAN``：startup 创建、shutdown close、startup
  failure 由 ``RuntimeInitializationStack`` rollback。
- ``MCP_SESSION_OWNER = APPLICATION_SCOPE_PER_CONFIGURED_SERVER_SESSION_
  OWNED_BY_MCP_INTEGRATION_COMPONENT``：AVAILABLE server 的 client/session
  由本组件持有整个 application 生命周期；Run 终止不关闭它。
- 不把 client/session 状态加入 ``RunContext``、``AgentState``、Snapshot、
  Recovery 或 approval state；不承诺自动重连/session restore。

Discovery 模式为 ``STARTUP_SNAPSHOT_ONLY``：``start()`` 一次性完成全部
enabled server 的 initialize + tools/list，之后快照不可变。
"""

from __future__ import annotations

import asyncio

from mcp.client import StdioMcpClient
from mcp.config import McpServerConfig
from mcp.discovery import discover_server
from mcp.models import (
    McpDiscoverySnapshot,
    McpServerDiscoveryResult,
    McpServerDiscoveryStatus,
)

_DEFAULT_COMPONENT_CLOSE_TIMEOUT_SECONDS = 3.0
_MIN_CLIENT_CLOSE_TIMEOUT_SECONDS = 0.05


class McpIntegrationComponent:
    """application-scope MCP client/session owner 与 discovery snapshot owner。"""

    def __init__(
        self,
        server_configs: tuple[McpServerConfig, ...],
        *,
        connect_timeout_seconds: float,
        request_timeout_seconds: float,
    ) -> None:
        self._server_configs = tuple(server_configs)
        self._connect_timeout_seconds = float(connect_timeout_seconds)
        self._request_timeout_seconds = float(request_timeout_seconds)
        self._clients: dict[str, StdioMcpClient] = {}
        self._snapshot: McpDiscoverySnapshot | None = None
        self._started = False
        self._closed = False

    async def start(self) -> None:
        """startup discovery：逐个 enabled server 建立 session 并发现工具。

        单 server 失败按显式降级策略记为 ``DISCOVERY_FAILED``；本方法只对
        组件 invariant 违反或意外内部错误抛出异常（由 initialization stack
        统一 rollback）。
        """
        if self._started:
            raise RuntimeError("mcp integration component already started")
        if self._closed:
            raise RuntimeError("mcp integration component already closed")
        results = []
        for server_config in self._server_configs:
            if not server_config.enabled:
                # per-server 显式 disabled：不启动子进程，仅保留 provenance。
                results.append(
                    McpServerDiscoveryResult(
                        server_id=server_config.server_id,
                        status=McpServerDiscoveryStatus.DISABLED,
                    )
                )
                continue
            outcome = await discover_server(
                server_config,
                connect_timeout_seconds=self._connect_timeout_seconds,
                request_timeout_seconds=self._request_timeout_seconds,
            )
            results.append(outcome.result)
            if outcome.client is not None:
                self._clients[server_config.server_id] = outcome.client
        self._snapshot = McpDiscoverySnapshot(results=tuple(results))
        self._started = True

    @property
    def discovery_snapshot(self) -> McpDiscoverySnapshot:
        """startup discovery 快照；``start()`` 前为 ``None``。"""
        return self._snapshot

    def session_for(self, server_id: str) -> StdioMcpClient | None:
        """读取指定 server 保留的 application-scope session（WP2 seam）。"""
        return self._clients.get(server_id)

    @property
    def started(self) -> bool:
        return self._started

    @property
    def closed(self) -> bool:
        return self._closed

    async def close(
        self, timeout: float = _DEFAULT_COMPONENT_CLOSE_TIMEOUT_SECONDS
    ) -> bool:
        """关闭全部保留 session（at most once，逐个 bounded，best-effort）。

        返回 ``False`` 表示存在未能在预算内完成关闭的 session，由
        ``ApplicationRuntimeServices.close`` 映射为 component close 失败
        事实；本方法自身不抛出传输/协议错误。
        """
        if self._closed:
            return True
        self._closed = True
        clients = list(self._clients.values())
        self._clients.clear()
        if not clients:
            return True
        budget = max(
            float(timeout) / len(clients), _MIN_CLIENT_CLOSE_TIMEOUT_SECONDS
        )
        all_closed = True
        for client in clients:
            try:
                client_closed = await client.close(timeout=budget)
                if not client_closed:
                    all_closed = False
            except asyncio.CancelledError:
                raise
            except Exception:
                all_closed = False
        return all_closed


__all__ = [
    "McpIntegrationComponent",
]
