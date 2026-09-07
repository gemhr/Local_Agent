#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""MCP boundary error taxonomy（WP1：只覆盖 MCP provider boundary）。

边界约束：

- 只表达 MCP boundary 失败（config / transport / protocol / discovery）；
  不得映射成 Runtime 执行错误（ToolExecutionError / GovernanceDenied /
  ApprovalRequired）——最终映射由 WP2 决定。
- 异常文本只保存 content-free 的 ``safe_error_code`` 与固定 reason 字符串；
  原始 server payload、stderr、command、参数、路径与 secret 不得进入异常。
"""

from __future__ import annotations


class McpBoundaryError(Exception):
    """MCP 边界错误共同基类。"""

    safe_error_code = "MCP_BOUNDARY_ERROR"

    def __init__(self, detail: str | None = None) -> None:
        self.safe_error_code = type(self).safe_error_code
        suffix = f" ({detail})" if detail else ""
        super().__init__(f"{self.safe_error_code}{suffix}")


class McpConfigError(McpBoundaryError, ValueError):
    """MCP 静态配置文件非法（fail closed，startup fatal）。"""

    safe_error_code = "MCP_CONFIG_INVALID"


class McpTransportError(McpBoundaryError):
    """subprocess spawn / stdin/stdout IO / bounded IO timeout / premature exit。"""

    safe_error_code = "MCP_TRANSPORT_ERROR"


class McpServerUnavailableError(McpTransportError):
    """configured command 无法启动（spawn 失败）。"""

    safe_error_code = "MCP_SERVER_UNAVAILABLE"


class McpTransportTimeoutError(McpTransportError):
    """底层 bounded IO timeout（非 Runtime timeout authority）。"""

    safe_error_code = "MCP_TRANSPORT_TIMEOUT"


class McpTransportClosedError(McpTransportError):
    """stdout 提前关闭 / 进程退出 / 连接已断开或已关闭。"""

    safe_error_code = "MCP_TRANSPORT_CLOSED"


class McpProtocolError(McpBoundaryError):
    """JSON-RPC / MCP 协议层失败（initialize failed、invalid JSON-RPC 等）。"""

    safe_error_code = "MCP_PROTOCOL_ERROR"


class McpProtocolVersionError(McpProtocolError):
    """server 返回的 protocolVersion 与冻结单一版本不一致。"""

    safe_error_code = "MCP_PROTOCOL_VERSION_UNSUPPORTED"


class McpCapabilityError(McpProtocolError):
    """server capabilities 缺少 WP1 所需的 tools capability。"""

    safe_error_code = "MCP_CAPABILITY_MISSING"


class McpDiscoveryError(McpProtocolError):
    """tools/list 响应或 tool entry 校验失败（invalid/duplicate/oversize）。"""

    safe_error_code = "MCP_DISCOVERY_INVALID"


__all__ = [
    "McpBoundaryError",
    "McpCapabilityError",
    "McpConfigError",
    "McpDiscoveryError",
    "McpProtocolError",
    "McpProtocolVersionError",
    "McpServerUnavailableError",
    "McpTransportClosedError",
    "McpTransportError",
    "McpTransportTimeoutError",
]
