#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""MCP server 静态配置加载（operator 控制的本地 JSON 文件，fail closed）。

配置来源遵循项目 Settings 模式：env ``LOCAL_AGENT_MCP_CONFIG_PATH`` 指向
一个本地 JSON 文件（类似 evaluation generation pin 的路径型配置先例），
由本模块在 application startup 一次性加载与校验；运行中不 reload。

安全边界：

- server identity、command、arguments、environment 全部来自 operator
  显式配置；模型输出、Tool 参数与请求输入不得以任何方式提供 MCP command
  或启动 MCP server。
- environment 值通常包含 secret；本模块不产生任何日志或异常文本携带其
  内容，配置文件本身不得提交进仓库。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from pathlib import Path

from mcp.errors import McpConfigError

MCP_CONFIG_SCHEMA_VERSION = "localagent-mcp-config.v1"

_SERVER_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SAFE_LOCAL_TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_COMMAND_MAX_CHARS = 1024
_ARGUMENT_MAX_CHARS = 4096
_ENV_KEY_MAX_CHARS = 256
_ENV_VALUE_MAX_CHARS = 4096
_MAX_SERVERS = 16
_MAX_ARGUMENTS_PER_SERVER = 32
_MAX_ENV_KEYS_PER_SERVER = 32
_MAX_TOOL_MAPPINGS_PER_SERVER = 128
_REMOTE_NAME_MAX_CHARS = 128
_TOOL_RISK_FACTS_MAX = 8
_TIMEOUT_MAX_SECONDS = 3600.0
_MAX_OUTPUT_BYTES_MAX = 1_048_576
_MAX_CONCURRENCY_MAX = 64

# WP2：每个将注册进生产 ToolRegistry 的 remote MCP tool 必须拥有 operator
# 显式配置的本地映射（local canonical name + 本地 policy 输入）。没有该映射
# 的 tool 在 registration 阶段 fail closed（不注册），绝不使用默认 LOW/ALLOW，
# 也绝不信任 MCP annotations。side_effect_kind 只接受现有 Governance
# full-combination allowlist 可分类的取值；idempotency 同理（UNKNOWN 不可分类）。
_ALLOWED_SIDE_EFFECT_KINDS = frozenset({"NONE", "LOCAL_STATE_MUTATION"})
_ALLOWED_IDEMPOTENCY_KINDS = frozenset(
    {"READ_ONLY", "IDEMPOTENT", "IDEMPOTENT_WITH_KEY", "NON_IDEMPOTENT"}
)
_ALLOWED_RISK_FACTS = frozenset(
    {
        "ARBITRARY_LOCAL_FILESYSTEM_READ",
        "SYSTEM_INFORMATION_READ",
        "RESTRICTED_WORKSPACE_READ",
    }
)
_ALLOWED_RISK_LEVELS = frozenset({"LOW", "MEDIUM", "HIGH"})

_ALLOWED_SERVER_FIELDS = frozenset(
    {"server_id", "command", "arguments", "environment", "enabled", "tools"}
)
_ALLOWED_TOOL_MAPPING_FIELDS = frozenset(
    {
        "local_name",
        "side_effect_kind",
        "idempotency",
        "risk_facts",
        "approval_required_threshold",
        "default_timeout_seconds",
        "max_output_bytes",
        "max_concurrency",
    }
)
_ALLOWED_TOP_LEVEL_FIELDS = frozenset({"schema_version", "servers"})


@dataclass(frozen=True)
class McpToolPolicyMapping:
    """单个 remote MCP tool 的 operator 本地映射（WP2 Local Policy Mapping）。

    字段是 ``ToolPolicyCatalog`` / ``ToolExecutionSpec`` 的配置输入，不是
    Runtime safety fact 本身；最终 safety fact 仍由本地 ``ToolPolicy`` 与
    ``ToolAdapter.spec_for`` 产生。MCP provider metadata 不参与本映射。
    """

    remote_name: str
    local_name: str
    side_effect_kind: str
    idempotency: str
    risk_facts: tuple[str, ...] = ()
    approval_required_threshold: str = "HIGH"
    default_timeout_seconds: float = 30.0
    max_output_bytes: int = 16_384
    max_concurrency: int = 1


@dataclass(frozen=True)
class McpServerConfig:
    """单个 configured MCP server 的 immutable 身份与启动配置。"""

    server_id: str
    command: str
    arguments: tuple[str, ...] = ()
    environment: tuple[tuple[str, str], ...] = ()
    enabled: bool = True
    tools: tuple[McpToolPolicyMapping, ...] = ()

    def environment_dict(self) -> dict[str, str]:
        return dict(self.environment)

    def tool_mapping_for(self, remote_name: str) -> McpToolPolicyMapping | None:
        """按 remote tool name 查找 operator 本地映射；缺失由调用方 fail closed。"""
        for mapping in self.tools:
            if mapping.remote_name == remote_name:
                return mapping
        return None


def _reject_control_chars(value: str) -> bool:
    return any(ord(ch) < 0x20 for ch in value)


def _parse_tool_mappings(raw_tools: object) -> tuple[McpToolPolicyMapping, ...]:
    """校验 per-server ``tools`` 映射；任何非法输入 fail closed（startup fatal）。"""
    if not isinstance(raw_tools, dict) or len(raw_tools) > _MAX_TOOL_MAPPINGS_PER_SERVER:
        raise McpConfigError("tools_invalid")
    mappings: list[McpToolPolicyMapping] = []
    seen_local_names: set[str] = set()
    for remote_name, raw_mapping in raw_tools.items():
        if (
            not isinstance(remote_name, str)
            or not remote_name
            or len(remote_name) > _REMOTE_NAME_MAX_CHARS
            or _reject_control_chars(remote_name)
        ):
            raise McpConfigError("tools_invalid")
        if not isinstance(raw_mapping, dict):
            raise McpConfigError("tools_invalid")
        unknown_fields = set(raw_mapping) - _ALLOWED_TOOL_MAPPING_FIELDS
        if unknown_fields:
            raise McpConfigError("unknown_field")
        local_name = raw_mapping.get("local_name")
        if (
            not isinstance(local_name, str)
            or _SAFE_LOCAL_TOOL_NAME_PATTERN.fullmatch(local_name) is None
        ):
            raise McpConfigError("tool_local_name_invalid")
        if local_name in seen_local_names:
            raise McpConfigError("tool_local_name_duplicate")
        seen_local_names.add(local_name)
        side_effect_kind = raw_mapping.get("side_effect_kind")
        if side_effect_kind not in _ALLOWED_SIDE_EFFECT_KINDS:
            raise McpConfigError("tool_side_effect_kind_invalid")
        idempotency = raw_mapping.get("idempotency")
        if idempotency not in _ALLOWED_IDEMPOTENCY_KINDS:
            raise McpConfigError("tool_idempotency_invalid")
        raw_risk_facts = raw_mapping.get("risk_facts", [])
        if (
            not isinstance(raw_risk_facts, list)
            or len(raw_risk_facts) > _TOOL_RISK_FACTS_MAX
            or any(fact not in _ALLOWED_RISK_FACTS for fact in raw_risk_facts)
        ):
            raise McpConfigError("tool_risk_facts_invalid")
        threshold = raw_mapping.get("approval_required_threshold", "HIGH")
        if threshold not in _ALLOWED_RISK_LEVELS:
            raise McpConfigError("tool_approval_threshold_invalid")
        default_timeout_seconds = raw_mapping.get("default_timeout_seconds", 30.0)
        if (
            isinstance(default_timeout_seconds, bool)
            or not isinstance(default_timeout_seconds, (int, float))
            or not default_timeout_seconds > 0
            or default_timeout_seconds > _TIMEOUT_MAX_SECONDS
        ):
            raise McpConfigError("tool_timeout_invalid")
        max_output_bytes = raw_mapping.get("max_output_bytes", 16_384)
        if (
            isinstance(max_output_bytes, bool)
            or not isinstance(max_output_bytes, int)
            or not 0 < max_output_bytes <= _MAX_OUTPUT_BYTES_MAX
        ):
            raise McpConfigError("tool_max_output_bytes_invalid")
        max_concurrency = raw_mapping.get("max_concurrency", 1)
        if (
            isinstance(max_concurrency, bool)
            or not isinstance(max_concurrency, int)
            or not 0 < max_concurrency <= _MAX_CONCURRENCY_MAX
        ):
            raise McpConfigError("tool_max_concurrency_invalid")
        mappings.append(
            McpToolPolicyMapping(
                remote_name=remote_name,
                local_name=local_name,
                side_effect_kind=side_effect_kind,
                idempotency=idempotency,
                risk_facts=tuple(raw_risk_facts),
                approval_required_threshold=threshold,
                default_timeout_seconds=float(default_timeout_seconds),
                max_output_bytes=max_output_bytes,
                max_concurrency=max_concurrency,
            )
        )
    return tuple(mappings)


def _parse_server(server_id: object) -> McpServerConfig:
    if not isinstance(server_id, dict):
        raise McpConfigError("server_entry_not_object")
    unknown_fields = set(server_id) - _ALLOWED_SERVER_FIELDS
    if unknown_fields:
        raise McpConfigError("unknown_field")
    server_id_value = server_id.get("server_id")
    if (
        not isinstance(server_id_value, str)
        or _SERVER_ID_PATTERN.fullmatch(server_id_value) is None
    ):
        raise McpConfigError("server_id_invalid")
    command = server_id.get("command")
    if (
        not isinstance(command, str)
        or not command.strip()
        or len(command) > _COMMAND_MAX_CHARS
        or _reject_control_chars(command)
    ):
        raise McpConfigError("command_invalid")
    raw_arguments = server_id.get("arguments", [])
    if not isinstance(raw_arguments, list) or len(raw_arguments) > _MAX_ARGUMENTS_PER_SERVER:
        raise McpConfigError("arguments_invalid")
    arguments: list[str] = []
    for argument in raw_arguments:
        if (
            not isinstance(argument, str)
            or len(argument) > _ARGUMENT_MAX_CHARS
            or "\x00" in argument
        ):
            raise McpConfigError("arguments_invalid")
        arguments.append(argument)
    raw_environment = server_id.get("environment", {})
    if not isinstance(raw_environment, dict) or len(raw_environment) > _MAX_ENV_KEYS_PER_SERVER:
        raise McpConfigError("environment_invalid")
    environment: list[tuple[str, str]] = []
    for key, value in raw_environment.items():
        if (
            not isinstance(key, str)
            or not key
            or len(key) > _ENV_KEY_MAX_CHARS
            or "=" in key
            or "\x00" in key
        ):
            raise McpConfigError("environment_invalid")
        if (
            not isinstance(value, str)
            or len(value) > _ENV_VALUE_MAX_CHARS
            or "\x00" in value
        ):
            raise McpConfigError("environment_invalid")
        environment.append((key, value))
    enabled = server_id.get("enabled", True)
    if not isinstance(enabled, bool):
        raise McpConfigError("enabled_invalid")
    tools = (
        _parse_tool_mappings(server_id["tools"])
        if "tools" in server_id
        else ()
    )
    return McpServerConfig(
        server_id=server_id_value,
        command=command,
        arguments=tuple(arguments),
        environment=tuple(environment),
        enabled=enabled,
        tools=tools,
    )


def load_mcp_server_configs(path: str | Path) -> tuple[McpServerConfig, ...]:
    """加载并完整校验 MCP server 配置文件；任何非法输入 fail closed。

    Args:
        path: operator 配置的本地 JSON 文件路径（不提交进仓库）。

    Returns:
        immutable ``McpServerConfig`` 元组（保持文件声明顺序）。

    Raises:
        McpConfigError: 文件缺失、JSON 非法、schema 不匹配或任一字段校验
            失败；异常只携带 safe reason，不携带文件内容。
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except OSError:
        raise McpConfigError("config_file_unavailable") from None
    except ValueError:
        raise McpConfigError("config_not_json") from None
    if not isinstance(payload, dict):
        raise McpConfigError("config_not_object")
    if set(payload) - _ALLOWED_TOP_LEVEL_FIELDS:
        raise McpConfigError("unknown_field")
    if payload.get("schema_version") != MCP_CONFIG_SCHEMA_VERSION:
        raise McpConfigError("schema_version_mismatch")
    raw_servers = payload.get("servers")
    if not isinstance(raw_servers, list) or len(raw_servers) > _MAX_SERVERS:
        raise McpConfigError("servers_invalid")
    configs: list[McpServerConfig] = []
    seen_ids: set[str] = set()
    for raw_server in raw_servers:
        config = _parse_server(raw_server)
        if config.server_id in seen_ids:
            raise McpConfigError("server_id_duplicate")
        seen_ids.add(config.server_id)
        configs.append(config)
    return tuple(configs)


__all__ = [
    "MCP_CONFIG_SCHEMA_VERSION",
    "McpServerConfig",
    "McpToolPolicyMapping",
    "load_mcp_server_configs",
]
