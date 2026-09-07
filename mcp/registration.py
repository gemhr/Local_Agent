#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""MCP discovery snapshot -> Existing ToolRegistry Registration（WP2）。

在既有 ``ToolRegistry`` / ``ToolPolicyCatalog`` freeze 之前，把 startup
discovery 快照映射为 ``ToolRegistration`` + ``ToolPolicy``：

- Canonical identity：operator 配置的 ``local_name`` 是唯一 local canonical
  tool name（``LOCAL_CANONICAL_TOOL_NAME_WITH_ADAPTER_LOCAL_PROVENANCE``）；
  server_id / remote_name 只保留在 adapter provenance，不进入
  ``ToolInvocation`` / Journal / PolicyCatalog key。
- Local policy mapping：每个将注册的 tool 必须拥有 operator 显式配置的
  policy 输入；缺失、非法或无法被 Governance full-combination allowlist
  分类的映射都使该 configured server 的 registration fail closed
  （零注册），绝不使用默认 LOW/ALLOW，也不信任 MCP annotations。
- 不创建第二个 Registry，不做运行期 mutation；本模块只在 freeze 前由
  ``server.py::lifespan()`` 调用一次。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import unicodedata
from typing import Callable

from core.runtime.retry import OperationIdempotency
from core.runtime.tool_contract import ToolExecutionSpec, ToolSideEffectKind
from core.runtime.tool_governance import (
    PRODUCTION_AGENT_IDS,
    ToolPolicy,
    ToolRiskFact,
    ToolRiskLevel,
    classify_full_risk_combination,
)
from core.runtime.tool_registry import (
    ToolDescriptor,
    ToolRegistration,
    ToolRegistryError,
)
from mcp.client import StdioMcpClient
from mcp.config import McpServerConfig
from mcp.models import McpDiscoverySnapshot, McpServerDiscoveryStatus
from mcp.adapter import McpBackedToolAdapter

_DESCRIPTION_MAX_CHARS = 2048


class McpServerRegistrationStatus(str, Enum):
    """per-server registration 状态（freeze 前一次性产出）。"""

    REGISTERED = "REGISTERED"
    REGISTRATION_FAILED = "REGISTRATION_FAILED"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class McpServerRegistrationResult:
    """单个 configured server 的 registration 结果（content-free 事实）。"""

    server_id: str
    status: McpServerRegistrationStatus
    safe_error_code: str | None = None
    registrations: tuple[ToolRegistration, ...] = ()
    policies: tuple[ToolPolicy, ...] = ()
    tool_names: tuple[str, ...] = ()


SessionResolverFactory = Callable[[str], Callable[[], StdioMcpClient | None]]


def build_mcp_registrations(
    *,
    snapshot: McpDiscoverySnapshot,
    configs: tuple[McpServerConfig, ...],
    session_resolver_factory: SessionResolverFactory,
    existing_tool_names: frozenset[str],
    request_timeout_seconds: float,
) -> tuple[McpServerRegistrationResult, ...]:
    """把 discovery 快照映射为 per-server registration 结果。

    任何单 tool 映射失败都使整个 server 的 registration fail closed
    （不部分采纳），与 WP1 discovery 的 per-server 边界一致；失败不阻止
    application startup，也不影响其它 server 或内置 Tool。
    """
    configs_by_id = {config.server_id: config for config in configs}
    used_names: set[str] = set(existing_tool_names)
    results: list[McpServerRegistrationResult] = []
    for discovery_result in snapshot.results:
        config = configs_by_id.get(discovery_result.server_id)
        if config is None:
            results.append(
                McpServerRegistrationResult(
                    server_id=discovery_result.server_id,
                    status=McpServerRegistrationStatus.REGISTRATION_FAILED,
                    safe_error_code="MCP_TOOL_POLICY_MISSING",
                )
            )
            continue
        if discovery_result.status is not McpServerDiscoveryStatus.AVAILABLE:
            # disabled / discovery failed server：不注册任何 tool，保留降级事实。
            results.append(
                McpServerRegistrationResult(
                    server_id=discovery_result.server_id,
                    status=McpServerRegistrationStatus.SKIPPED,
                    safe_error_code=discovery_result.safe_error_code,
                )
            )
            continue
        results.append(
            _register_server(
                discovery_result=discovery_result,
                config=config,
                session_resolver_factory=session_resolver_factory,
                used_names=used_names,
                request_timeout_seconds=request_timeout_seconds,
            )
        )
    return tuple(results)


def _register_server(
    *,
    discovery_result,
    config: McpServerConfig,
    session_resolver_factory: SessionResolverFactory,
    used_names: set[str],
    request_timeout_seconds: float,
) -> McpServerRegistrationResult:
    registrations: list[ToolRegistration] = []
    policies: list[ToolPolicy] = []
    tool_names: list[str] = []
    for descriptor in discovery_result.tools:
        mapping = config.tool_mapping_for(descriptor.remote_name)
        if mapping is None:
            return _failed(
                discovery_result.server_id, "MCP_TOOL_POLICY_MISSING"
            )
        try:
            side_effect_kind = ToolSideEffectKind(mapping.side_effect_kind)
            idempotency = OperationIdempotency(mapping.idempotency)
            risk_facts = tuple(
                ToolRiskFact(fact) for fact in mapping.risk_facts
            )
            threshold = ToolRiskLevel[mapping.approval_required_threshold]
        except (KeyError, ValueError):
            return _failed(
                discovery_result.server_id, "MCP_TOOL_POLICY_INVALID"
            )
        if (
            classify_full_risk_combination(
                frozenset(risk_facts), side_effect_kind, idempotency
            )
            is None
        ):
            return _failed(
                discovery_result.server_id, "MCP_TOOL_RISK_UNCLASSIFIED"
            )
        local_name = mapping.local_name
        if local_name in used_names:
            return _failed(
                discovery_result.server_id, "MCP_TOOL_NAME_COLLISION"
            )
        try:
            input_schema = json.loads(descriptor.input_schema_json)
        except ValueError:
            return _failed(
                discovery_result.server_id, "MCP_TOOL_INPUT_SCHEMA_INVALID"
            )
        if not isinstance(input_schema, dict):
            return _failed(
                discovery_result.server_id, "MCP_TOOL_INPUT_SCHEMA_INVALID"
            )
        schema_type = input_schema.get("type")
        if schema_type is not None and schema_type != "object":
            return _failed(
                discovery_result.server_id, "MCP_TOOL_INPUT_SCHEMA_UNSUPPORTED"
            )
        spec = ToolExecutionSpec(
            tool_name=local_name,
            side_effect_kind=side_effect_kind,
            idempotency=idempotency,
            requires_resource_key=False,
            supports_cooperative_cancellation=True,
            supports_side_effect_checkpoint=(
                side_effect_kind is not ToolSideEffectKind.NONE
            ),
            default_timeout_seconds=mapping.default_timeout_seconds,
            max_output_bytes=mapping.max_output_bytes,
            max_concurrency=mapping.max_concurrency,
        )
        adapter = McpBackedToolAdapter(
            spec=spec,
            server_id=discovery_result.server_id,
            remote_name=descriptor.remote_name,
            input_schema=input_schema,
            session_resolver=session_resolver_factory(
                discovery_result.server_id
            ),
            request_timeout_seconds=request_timeout_seconds,
        )
        try:
            tool_descriptor = ToolDescriptor(
                name=local_name,
                description=_sanitize_description(
                    descriptor.description, descriptor.remote_name
                ),
            )
        except ToolRegistryError:
            return _failed(
                discovery_result.server_id, "MCP_TOOL_DESCRIPTOR_INVALID"
            )
        registrations.append(
            ToolRegistration(descriptor=tool_descriptor, adapter=adapter)
        )
        policies.append(
            ToolPolicy(
                tool_name=local_name,
                allowed_agent_ids=PRODUCTION_AGENT_IDS,
                risk_facts=risk_facts,
                approval_required_threshold=threshold,
            )
        )
        tool_names.append(local_name)
    # 整个 server 全部 tool 映射成功后才提交占用，保证 fail-closed 原子性。
    used_names.update(tool_names)
    return McpServerRegistrationResult(
        server_id=discovery_result.server_id,
        status=McpServerRegistrationStatus.REGISTERED,
        registrations=tuple(registrations),
        policies=tuple(policies),
        tool_names=tuple(tool_names),
    )


def _failed(server_id: str, safe_error_code: str) -> McpServerRegistrationResult:
    return McpServerRegistrationResult(
        server_id=server_id,
        status=McpServerRegistrationStatus.REGISTRATION_FAILED,
        safe_error_code=safe_error_code,
    )


def _sanitize_description(description: str, remote_name: str) -> str:
    """把 untrusted provider description 规范为 Descriptor 安全文本。

    控制字符替换为空格并压缩空白；清空后回退到 content-free 默认描述，
    保证 ToolDescriptor 合同（非空、无控制字符）在 freeze 前成立。
    """
    sanitized = "".join(
        " " if unicodedata.category(char) == "Cc" else char
        for char in description
    )
    sanitized = " ".join(sanitized.split())[:_DESCRIPTION_MAX_CHARS].strip()
    if not sanitized:
        return (
            f"MCP tool '{remote_name}' provided by a configured "
            f"MCP server."
        )
    return sanitized


__all__ = [
    "McpServerRegistrationResult",
    "McpServerRegistrationStatus",
    "build_mcp_registrations",
]
