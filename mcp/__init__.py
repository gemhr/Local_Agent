#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LocalAgent MCP Integration（Phase9）。

MCP 是外部 Tool Provider Boundary，不进入 Runtime Core。当前提供：

- stdio MCP client（initialize / capability negotiation / tools/list /
  ``tools/call`` / close）
- startup discovery snapshot（MCP Tool Descriptor，untrusted provider metadata）
- application-scope 集成组件（由 ``server.py::lifespan()`` 的
  ``RuntimeInitializationStack`` 构造与关闭）
- MCP-backed ToolAdapter + freeze 前 Registration（WP2：MCP Tool 成为既有
  Tool Runtime 中的一个 Tool）

不实现 Streamable HTTP / SSE、第二套 Tool Runtime、provider dispatcher、
MCP-specific approval/retry、Resources / Prompts / listChanged / hot reload；
Runtime timeout/cancellation authority 仍归既有 RunContext /
ToolExecutionService / RunCoordinator。
"""

from mcp.adapter import McpBackedToolAdapter
from mcp.config import McpServerConfig, McpToolPolicyMapping, load_mcp_server_configs
from mcp.errors import (
    McpBoundaryError,
    McpCapabilityError,
    McpConfigError,
    McpDiscoveryError,
    McpProtocolError,
    McpProtocolVersionError,
    McpServerUnavailableError,
    McpTransportClosedError,
    McpTransportError,
    McpTransportTimeoutError,
)
from mcp.lifecycle import McpIntegrationComponent
from mcp.models import (
    MCP_PROTOCOL_VERSION,
    McpDiscoverySnapshot,
    McpInitializeInfo,
    McpServerDiscoveryResult,
    McpServerDiscoveryStatus,
    McpToolCallResult,
    McpToolDescriptor,
)
from mcp.registration import (
    McpServerRegistrationResult,
    McpServerRegistrationStatus,
    build_mcp_registrations,
)

__all__ = [
    "MCP_PROTOCOL_VERSION",
    "McpBackedToolAdapter",
    "McpBoundaryError",
    "McpCapabilityError",
    "McpConfigError",
    "McpDiscoveryError",
    "McpDiscoverySnapshot",
    "McpInitializeInfo",
    "McpIntegrationComponent",
    "McpProtocolError",
    "McpProtocolVersionError",
    "McpServerConfig",
    "McpServerDiscoveryResult",
    "McpServerDiscoveryStatus",
    "McpServerRegistrationResult",
    "McpServerRegistrationStatus",
    "McpServerUnavailableError",
    "McpToolCallResult",
    "McpToolDescriptor",
    "McpToolPolicyMapping",
    "McpTransportClosedError",
    "McpTransportError",
    "McpTransportTimeoutError",
    "build_mcp_registrations",
    "load_mcp_server_configs",
]
