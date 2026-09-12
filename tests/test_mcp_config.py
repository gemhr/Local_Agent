"""Phase9-WP1：MCP 静态配置加载的 fail-closed 测试（DETERMINISTIC_TEST）。

只覆盖 ``mcp.config.load_mcp_server_configs`` 的输入校验边界；不涉及
Runtime 执行/Governance/HITL。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp.config import (
    MCP_CONFIG_SCHEMA_VERSION,
    McpServerConfig,
    load_mcp_server_configs,
)
from mcp.errors import McpConfigError


def _write_config(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "mcp_config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _server_payload(**overrides: object) -> dict:
    server = {
        "server_id": "demo",
        "command": "python",
        "arguments": ["-u", "server.py"],
        "environment": {"FAKE_KEY": "value"},
        "enabled": True,
    }
    server.update(overrides)
    return server


def test_valid_config_loads_frozen_configs(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        {
            "schema_version": MCP_CONFIG_SCHEMA_VERSION,
            "servers": [
                _server_payload(),
                _server_payload(server_id="other", enabled=False),
            ],
        },
    )
    configs = load_mcp_server_configs(path)
    assert isinstance(configs, tuple)
    assert all(isinstance(config, McpServerConfig) for config in configs)
    assert [config.server_id for config in configs] == ["demo", "other"]
    assert configs[0].command == "python"
    assert configs[0].arguments == ("-u", "server.py")
    assert configs[0].environment == (("FAKE_KEY", "value"),)
    assert configs[0].enabled is True
    assert configs[1].enabled is False


def test_operator_can_explicitly_map_network_and_egress_risk(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        {
            "schema_version": MCP_CONFIG_SCHEMA_VERSION,
            "servers": [_server_payload(tools={
                "remote_echo": {
                    "local_name": "remote_echo",
                    "side_effect_kind": "NONE",
                    "idempotency": "READ_ONLY",
                    "risk_facts": ["EXTERNAL_NETWORK", "DATA_EGRESS"],
                }
            })],
        },
    )
    mapping = load_mcp_server_configs(path)[0].tool_mapping_for("remote_echo")
    assert mapping is not None
    assert mapping.risk_facts == ("EXTERNAL_NETWORK", "DATA_EGRESS")


def test_defaults_are_applied(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        {
            "schema_version": MCP_CONFIG_SCHEMA_VERSION,
            "servers": [{"server_id": "demo", "command": "demo-cmd"}],
        },
    )
    configs = load_mcp_server_configs(path)
    assert configs[0].arguments == ()
    assert configs[0].environment == ()
    assert configs[0].enabled is True


def test_environment_dict_helper_round_trip(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        {
            "schema_version": MCP_CONFIG_SCHEMA_VERSION,
            "servers": [_server_payload()],
        },
    )
    config = load_mcp_server_configs(path)[0]
    assert config.environment_dict() == {"FAKE_KEY": "value"}


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"schema_version": "other-version", "servers": []},
        {"servers": []},
        {
            "schema_version": MCP_CONFIG_SCHEMA_VERSION,
            "servers": [],
            "unexpected": True,
        },
        {"schema_version": MCP_CONFIG_SCHEMA_VERSION, "servers": {}},
        {
            "schema_version": MCP_CONFIG_SCHEMA_VERSION,
            "servers": [_server_payload(unexpected_field=1)],
        },
    ],
)
def test_invalid_top_level_payload_fails_closed(
    tmp_path: Path, payload: object
) -> None:
    path = _write_config(tmp_path, payload)
    with pytest.raises(McpConfigError) as excinfo:
        load_mcp_server_configs(path)
    assert excinfo.value.safe_error_code == "MCP_CONFIG_INVALID"


def test_missing_file_and_broken_json_fail_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    with pytest.raises(McpConfigError):
        load_mcp_server_configs(tmp_path / "missing.json")
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(McpConfigError):
        load_mcp_server_configs(broken)
    captured = capsys.readouterr()
    # 异常不携带文件内容/路径细节之外的信息；这里仅确保无 secret 泄漏通道。
    assert "FAKE_KEY" not in captured.err


def test_server_id_validation(tmp_path: Path) -> None:
    for invalid in ("Demo", "", "1abc", "a b", "x" * 65, None):
        path = _write_config(
            tmp_path,
            {
                "schema_version": MCP_CONFIG_SCHEMA_VERSION,
                "servers": [_server_payload(server_id=invalid)],
            },
        )
        with pytest.raises(McpConfigError):
            load_mcp_server_configs(path)


def test_duplicate_server_id_fails_closed(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        {
            "schema_version": MCP_CONFIG_SCHEMA_VERSION,
            "servers": [_server_payload(), _server_payload()],
        },
    )
    with pytest.raises(McpConfigError):
        load_mcp_server_configs(path)


@pytest.mark.parametrize("command", ["", "   ", None, 123, "x" * 2000, "bad\x01cmd"])
def test_command_validation(tmp_path: Path, command: object) -> None:
    path = _write_config(
        tmp_path,
        {
            "schema_version": MCP_CONFIG_SCHEMA_VERSION,
            "servers": [_server_payload(command=command)],
        },
    )
    with pytest.raises(McpConfigError):
        load_mcp_server_configs(path)


@pytest.mark.parametrize(
    "arguments",
    [
        ["ok", 123],
        ["x" * 5000],
        ["with\x00nul"],
        "not-a-list",
    ],
)
def test_arguments_validation(tmp_path: Path, arguments: object) -> None:
    path = _write_config(
        tmp_path,
        {
            "schema_version": MCP_CONFIG_SCHEMA_VERSION,
            "servers": [_server_payload(arguments=arguments)],
        },
    )
    with pytest.raises(McpConfigError):
        load_mcp_server_configs(path)


@pytest.mark.parametrize(
    "environment",
    [
        {"KEY": 123},
        {"": "value"},
        {"A=B": "value"},
        {"KEY": None},
        "not-a-dict",
    ],
)
def test_environment_validation(tmp_path: Path, environment: object) -> None:
    path = _write_config(
        tmp_path,
        {
            "schema_version": MCP_CONFIG_SCHEMA_VERSION,
            "servers": [_server_payload(environment=environment)],
        },
    )
    with pytest.raises(McpConfigError):
        load_mcp_server_configs(path)


@pytest.mark.parametrize("enabled", ["true", 1, None])
def test_enabled_must_be_bool(tmp_path: Path, enabled: object) -> None:
    path = _write_config(
        tmp_path,
        {
            "schema_version": MCP_CONFIG_SCHEMA_VERSION,
            "servers": [_server_payload(enabled=enabled)],
        },
    )
    with pytest.raises(McpConfigError):
        load_mcp_server_configs(path)


def test_server_count_limit(tmp_path: Path) -> None:
    servers = [_server_payload(server_id=f"server-{index}") for index in range(17)]
    path = _write_config(
        tmp_path,
        {"schema_version": MCP_CONFIG_SCHEMA_VERSION, "servers": servers},
    )
    with pytest.raises(McpConfigError):
        load_mcp_server_configs(path)


# ---- WP2：per-tool operator policy mapping（local canonical name + policy 输入）----


def _tool_mapping(**overrides: object) -> dict:
    mapping = {
        "local_name": "mcp_echo",
        "side_effect_kind": "NONE",
        "idempotency": "READ_ONLY",
    }
    mapping.update(overrides)
    return mapping


def test_tool_mappings_load(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        {
            "schema_version": MCP_CONFIG_SCHEMA_VERSION,
            "servers": [
                _server_payload(
                    tools={
                        "echo": _tool_mapping(),
                        "write": _tool_mapping(
                            local_name="mcp_write",
                            side_effect_kind="LOCAL_STATE_MUTATION",
                            idempotency="NON_IDEMPOTENT",
                            risk_facts=[],
                            approval_required_threshold="HIGH",
                            default_timeout_seconds=12.5,
                            max_output_bytes=2048,
                            max_concurrency=3,
                        ),
                    }
                )
            ],
        },
    )
    configs = load_mcp_server_configs(path)
    tools = configs[0].tools
    assert [tool.remote_name for tool in tools] == ["echo", "write"]
    assert tools[0].local_name == "mcp_echo"
    assert tools[0].risk_facts == ()
    assert tools[0].approval_required_threshold == "HIGH"
    assert tools[0].default_timeout_seconds == 30.0
    assert tools[0].max_output_bytes == 16_384
    assert tools[0].max_concurrency == 1
    assert tools[1].side_effect_kind == "LOCAL_STATE_MUTATION"
    assert tools[1].default_timeout_seconds == 12.5
    assert tools[1].max_output_bytes == 2048
    assert tools[1].max_concurrency == 3
    assert configs[0].tool_mapping_for("echo") is tools[0]
    assert configs[0].tool_mapping_for("missing") is None


def test_tools_absent_defaults_to_empty(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        {
            "schema_version": MCP_CONFIG_SCHEMA_VERSION,
            "servers": [_server_payload()],
        },
    )
    assert load_mcp_server_configs(path)[0].tools == ()


@pytest.mark.parametrize(
    "tools",
    [
        "not-a-dict",
        {"echo": "not-a-dict"},
        {"": _tool_mapping()},
        {"e" * 129: _tool_mapping()},
        {"echo\n": _tool_mapping()},
        {"echo": {"local_name": "mcp_echo"}},
        {"echo": _tool_mapping(extra_field=1)},
        {"echo": _tool_mapping(local_name="Invalid_Name")},
        {"echo": _tool_mapping(local_name="1leading_digit")},
        {"echo": _tool_mapping(local_name="x" * 65)},
        {"echo": _tool_mapping(side_effect_kind="EXTERNAL_STATE_MUTATION")},
        {"echo": _tool_mapping(idempotency="UNKNOWN")},
        {"echo": _tool_mapping(risk_facts=["NOT_A_FACT"])},
        {"echo": _tool_mapping(approval_required_threshold="CRITICAL")},
        {"echo": _tool_mapping(default_timeout_seconds=0)},
        {"echo": _tool_mapping(default_timeout_seconds=-1.0)},
        {"echo": _tool_mapping(max_output_bytes=0)},
        {"echo": _tool_mapping(max_output_bytes=2_000_000)},
        {"echo": _tool_mapping(max_concurrency=0)},
        {"echo": _tool_mapping(max_concurrency=65)},
        # 同一 server 内 local_name 冲突：fail closed。
        {"a": _tool_mapping(local_name="same_name"), "b": _tool_mapping(local_name="same_name")},
    ],
)
def test_tool_mapping_validation(tmp_path: Path, tools: object) -> None:
    path = _write_config(
        tmp_path,
        {
            "schema_version": MCP_CONFIG_SCHEMA_VERSION,
            "servers": [_server_payload(tools=tools)],
        },
    )
    with pytest.raises(McpConfigError):
        load_mcp_server_configs(path)
