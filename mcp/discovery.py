#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""MCP discovery boundary：per-server startup discovery（snapshot only）。

把 ``MCP tools/list`` 转换为本地 ``McpToolDescriptor`` discovery 快照。
本模块不创建 ``ToolRegistration``，不做本地 name mapping，也不推导任何
Runtime safety fact（属 WP2 与本地 Governance authority）。

WP1 显式启动失败策略：单个 enabled server 的 transport/protocol/discovery
失败标记为 ``DISCOVERY_FAILED``（safe code），不阻止 startup、不创建第二
runtime、不放宽本地策略；非 MCP boundary 的意外异常向上抛出，由
``RuntimeInitializationStack`` 统一 rollback。
"""

from __future__ import annotations

from dataclasses import dataclass

from mcp.client import StdioMcpClient
from mcp.config import McpServerConfig
from mcp.errors import McpBoundaryError
from mcp.models import (
    McpInitializeInfo,
    McpServerDiscoveryResult,
    McpServerDiscoveryStatus,
    McpToolDescriptor,
)

_DISCOVERY_FAILURE_CLOSE_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class McpServerDiscoveryOutcome:
    """单 server discovery 输出：快照事实 + AVAILABLE 时保留的 session。"""

    result: McpServerDiscoveryResult
    client: StdioMcpClient | None


async def discover_server(
    server_config: McpServerConfig,
    *,
    connect_timeout_seconds: float,
    request_timeout_seconds: float,
) -> McpServerDiscoveryOutcome:
    """对一个 configured server 执行 initialize + tools/list。

    AVAILABLE 时返回仍打开的 client/session（由调用方组件持有并在
    application shutdown 时关闭）；失败时先 bounded 关闭子进程再返回
    ``DISCOVERY_FAILED`` 结果。
    """
    client = StdioMcpClient(
        server_config=server_config,
        connect_timeout_seconds=connect_timeout_seconds,
        request_timeout_seconds=request_timeout_seconds,
    )
    try:
        initialize_info = await client.initialize()
        tools = await client.list_tools()
    except McpBoundaryError as exc:
        try:
            await client.close(
                timeout=_DISCOVERY_FAILURE_CLOSE_TIMEOUT_SECONDS
            )
        except Exception:
            # close 内部按合同不抛出；此处防御性兜底，不覆盖原始失败事实。
            pass
        return McpServerDiscoveryOutcome(
            result=McpServerDiscoveryResult(
                server_id=server_config.server_id,
                status=McpServerDiscoveryStatus.DISCOVERY_FAILED,
                safe_error_code=exc.safe_error_code,
            ),
            client=None,
        )
    result = _available_result(server_config.server_id, initialize_info, tools)
    return McpServerDiscoveryOutcome(result=result, client=client)


def _available_result(
    server_id: str,
    initialize_info: McpInitializeInfo,
    tools: tuple[McpToolDescriptor, ...],
) -> McpServerDiscoveryResult:
    return McpServerDiscoveryResult(
        server_id=server_id,
        status=McpServerDiscoveryStatus.AVAILABLE,
        protocol_version=initialize_info.protocol_version,
        server_info_name=initialize_info.server_name,
        server_info_version=initialize_info.server_version,
        tools=tools,
    )


__all__ = [
    "McpServerDiscoveryOutcome",
    "discover_server",
]
