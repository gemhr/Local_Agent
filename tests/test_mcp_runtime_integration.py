"""Phase9-WP2：MCP-backed ToolAdapter + Existing Runtime Integration 测试。

全部为 ``DETERMINISTIC_TEST``：通过本地确定性 fake stdio MCP server 子进程
驱动真实 subprocess/JSON-RPC/tools/call 路径；不存在真实外部 MCP server，
本文件不构成也不声称 ``REAL_MCP_E2E``。

覆盖任务矩阵：

- A Registration（discovery -> mapping -> registration -> Registry/Policy coverage）
- B Collision（跨 server 同名 remote tool / local canonical 冲突 fail closed）
- C Missing Policy（fail closed，零注册）
- D Metadata spoofing（readOnlyHint=true 不能压过本地 policy/spec）
- E MCP tools/call success（经真实 ToolExecutionService）
- F isError -> Existing typed failure
- G Unsupported content -> safe failure
- H Timeout（Runtime bounded）
- I Cancellation（notifications/cancelled + late response 无第二次完成）
- J No bypass（MCP execution 只能发生在 ToolExecutionService 路径之后）
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import sys
import time

import pytest

from core.runtime import (
    BudgetLedger,
    RunBudget,
    ToolExecutionError,
    ToolExecutionService,
    ToolExecutionStatus,
    ToolSideEffectState,
    create_run_context,
)
from core.runtime.retry import RetryExecutor, RetryPolicy
from core.runtime.agent_registry import DEFAULT_AGENT_REGISTRY
from core.runtime.retry import OperationIdempotency
from core.runtime.tool_adapters import ToolAdapterInvocationError
from core.runtime.tool_contract import (
    ToolErrorCategory,
    ToolExecutionPhase,
    ToolExecutionSpec,
    ToolSideEffectKind,
)
from core.runtime.tool_governance import (
    ToolGovernanceContext,
    ToolGovernanceOutcome,
    ToolGovernanceService,
    ToolPolicyCatalog,
    ToolRiskLevel,
    register_default_tool_policies,
)
from core.runtime.tool_registry import ToolRegistry
from mcp.adapter import McpBackedToolAdapter
from mcp.client import StdioMcpClient
from mcp.config import McpServerConfig, McpToolPolicyMapping
from mcp.errors import McpProtocolError
from mcp.models import (
    MCP_PROTOCOL_VERSION,
    McpDiscoverySnapshot,
    McpServerDiscoveryResult,
    McpServerDiscoveryStatus,
    McpToolDescriptor,
)
from mcp.registration import (
    McpServerRegistrationStatus,
    build_mcp_registrations,
)
from tools.registry import build_builtin_tool_registrations

BUILTIN_TOOL_NAMES = frozenset(
    registration.descriptor.name
    for registration in build_builtin_tool_registrations()
)

FAKE_SERVER_SOURCE = '''
import json
import os
import sys
import time

MODE = os.environ.get("FAKE_MCP_MODE", "call_success")
CANCEL_MARKER = os.environ.get("FAKE_MCP_CANCEL_MARKER", "")


def _send(payload):
    sys.stdout.write(json.dumps(payload) + "\\n")
    sys.stdout.flush()


def _read():
    line = sys.stdin.readline()
    if not line:
        return None
    return json.loads(line)


def _tool(name):
    return {
        "name": name,
        "description": "fake tool " + name,
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        # untrusted provider annotation：本地 policy 必须完全压过它（测试 D）。
        "annotations": {"readOnlyHint": True},
    }


while True:
    message = _read()
    if message is None:
        break
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        _send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "PROTOCOL_VERSION",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-mcp", "version": "1.0.0"},
            },
        })
        continue
    if method == "tools/list":
        _send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": [_tool("echo")]},
        })
        continue
    if method == "tools/call":
        if MODE == "call_success":
            _send({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [
                        {"type": "text", "text": "ECHO:" + json.dumps(
                            (message.get("params") or {}).get("arguments"))}
                    ]
                },
            })
        elif MODE == "call_is_error":
            _send({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": "SECRET-REMOTE-STACK"}],
                    "isError": True,
                },
            })
        elif MODE == "call_image_only":
            _send({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [
                        {"type": "image", "data": "x", "mimeType": "image/png"}
                    ]
                },
            })
        elif MODE == "call_embedded_mixed":
            _send({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [
                    {"type": "text", "text": "status"},
                    {"type": "resource", "resource": {
                        "uri": "repo://private/metadata-sentinel",
                        "mimeType": "application/octet-stream",
                        "text": "body 中"}},
                    {"type": "text", "text": "tail"},
                ]},
            })
        elif MODE == "call_embedded_blob":
            _send({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "resource", "resource": {
                    "uri": "repo://blob", "blob": "AA=="}}]},
            })
        elif MODE == "call_is_error_embedded":
            _send({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "resource", "resource": {
                        "uri": "repo://error", "text": "SECRET-RESOURCE-BODY"}}],
                    "isError": True,
                },
            })
        elif MODE == "call_structured_only":
            _send({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [],
                    "structuredContent": {"answer": 42},
                },
            })
        elif MODE == "call_rpc_error":
            _send({
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32600, "message": "refused"},
            })
        elif MODE == "call_content_not_list":
            _send({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": "oops"},
            })
        elif MODE == "call_hang":
            time.sleep(3600)
        elif MODE == "call_cancelable":
            # 等待 notifications/cancelled；收到后写 marker 并补发迟到响应。
            while True:
                cancel_message = _read()
                if cancel_message is None:
                    sys.exit(0)
                if cancel_message.get("method") == "notifications/cancelled":
                    if CANCEL_MARKER:
                        with open(CANCEL_MARKER, "w", encoding="utf-8") as f:
                            f.write("cancelled")
                    _send({
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"content": [
                            {"type": "text", "text": "LATE-RESPONSE"}]},
                    })
                    break
        continue
    if request_id is not None:
        _send({"jsonrpc": "2.0", "id": request_id,
               "error": {"code": -32601, "message": "method not found"}})
'''.replace("PROTOCOL_VERSION", MCP_PROTOCOL_VERSION)


def _write_fake_server(tmp_path: Path) -> Path:
    script = tmp_path / "fake_mcp_call_server.py"
    script.write_text(FAKE_SERVER_SOURCE, encoding="utf-8")
    return script


def _server_config(tmp_path: Path, mode: str, **kwargs) -> McpServerConfig:
    script = _write_fake_server(tmp_path)
    environment = [("FAKE_MCP_MODE", mode)]
    marker = kwargs.pop("cancel_marker", None)
    if marker is not None:
        environment.append(("FAKE_MCP_CANCEL_MARKER", str(marker)))
    return McpServerConfig(
        server_id=kwargs.pop("server_id", "demo"),
        command=sys.executable,
        arguments=(str(script),),
        environment=tuple(environment),
        tools=kwargs.pop(
            "tools",
            (
                McpToolPolicyMapping(
                    remote_name="echo",
                    local_name="mcp_echo",
                    side_effect_kind="NONE",
                    idempotency="READ_ONLY",
                ),
            ),
        ),
    )


async def _make_initialized_client(tmp_path: Path, mode: str, **kwargs):
    client = StdioMcpClient(
        server_config=_server_config(tmp_path, mode, **kwargs),
        connect_timeout_seconds=10.0,
        request_timeout_seconds=kwargs.pop("request_timeout_seconds", 10.0),
    )
    await client.initialize()
    return client


def _descriptor(
    remote_name: str = "echo",
    annotations: dict | None = None,
) -> McpToolDescriptor:
    payload: dict = {
        "name": remote_name,
        "description": "fake tool",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    }
    if annotations is not None:
        payload["annotations"] = annotations
    return McpToolDescriptor.from_protocol_payload("demo", payload)


def _available_result(
    server_id: str = "demo",
    tools: tuple[McpToolDescriptor, ...] | None = None,
) -> McpServerDiscoveryResult:
    return McpServerDiscoveryResult(
        server_id=server_id,
        status=McpServerDiscoveryStatus.AVAILABLE,
        tools=tools if tools is not None else (_descriptor(),),
    )


def _read_only_mapping(local_name: str = "mcp_echo", remote_name: str = "echo"):
    return McpToolPolicyMapping(
        remote_name=remote_name,
        local_name=local_name,
        side_effect_kind="NONE",
        idempotency="READ_ONLY",
    )


def _mutation_mapping(local_name: str = "mcp_echo", remote_name: str = "echo"):
    return McpToolPolicyMapping(
        remote_name=remote_name,
        local_name=local_name,
        side_effect_kind="LOCAL_STATE_MUTATION",
        idempotency="NON_IDEMPOTENT",
    )


def _build(snapshot_results, configs, existing=None):
    return build_mcp_registrations(
        snapshot=McpDiscoverySnapshot(results=tuple(snapshot_results)),
        configs=tuple(configs),
        session_resolver_factory=lambda server_id: (lambda: None),
        existing_tool_names=BUILTIN_TOOL_NAMES if existing is None else existing,
        request_timeout_seconds=10.0,
    )


class _FakeAdapterContext:
    """最小 ToolAdapterContext seam（raise_if_cancelled/before_side_effect/
    remaining_seconds），供 adapter 级测试使用；完整 Run 路径由 E/H 覆盖。"""

    def __init__(self, budget_seconds: float = 30.0) -> None:
        self.side_effect_calls = 0
        self._deadline = time.monotonic() + budget_seconds

    def raise_if_cancelled(self) -> None:
        return None

    def before_side_effect(self) -> None:
        self.side_effect_calls += 1

    def remaining_seconds(self) -> float:
        return max(0.0, self._deadline - time.monotonic())


def _make_run_context(timeout_seconds: float = 5.0):
    context, _ = create_run_context(
        entry_agent_id="integration", timeout_seconds=timeout_seconds
    )
    context.attach_budget_ledger(
        BudgetLedger(RunBudget(max_tool_calls=4, max_retries=2))
    )
    return context


# ---- A. Registration ----


def test_registration_maps_snapshot_into_frozen_registry_with_policy_coverage():
    snapshot_results = (
        _available_result("server_a", (_descriptor("echo"),)),
        _available_result(
            "server_b", (_descriptor("emit"),)
        ),
    )
    configs = (
        McpServerConfig(
            server_id="server_a",
            command="unused",
            tools=(_read_only_mapping(local_name="mcp_a_echo", remote_name="echo"),),
        ),
        McpServerConfig(
            server_id="server_b",
            command="unused",
            tools=(_mutation_mapping(local_name="mcp_b_emit", remote_name="emit"),),
        ),
    )
    results = _build(snapshot_results, configs)
    assert [result.status for result in results] == [
        McpServerRegistrationStatus.REGISTERED,
        McpServerRegistrationStatus.REGISTERED,
    ]

    registry = ToolRegistry()
    for registration in build_builtin_tool_registrations():
        registry.register(registration)
    policies = []
    for result in results:
        for registration in result.registrations:
            registry.register(registration)
        policies.extend(result.policies)
    registry.freeze()

    catalog = ToolPolicyCatalog(
        tool_registry=registry, agent_registry=DEFAULT_AGENT_REGISTRY
    )
    register_default_tool_policies(catalog)
    for policy in policies:
        catalog.register(policy)
    catalog.freeze()

    assert registry.contains("mcp_a_echo")
    assert registry.contains("mcp_b_emit")
    for registration in registry.registrations():
        assert catalog.contains(registration.descriptor.name)

    read_only_registration = registry.resolve("mcp_a_echo")
    mutation_registration = registry.resolve("mcp_b_emit")
    read_spec = read_only_registration.adapter.spec
    mutation_spec = mutation_registration.adapter.spec
    assert read_spec.side_effect_kind is ToolSideEffectKind.NONE
    assert read_spec.idempotency is OperationIdempotency.READ_ONLY
    assert mutation_spec.side_effect_kind is ToolSideEffectKind.LOCAL_STATE_MUTATION
    assert mutation_spec.idempotency is OperationIdempotency.NON_IDEMPOTENT
    assert mutation_spec.supports_side_effect_checkpoint is True

    # Model-facing descriptor：模型只看到一个正常 LocalAgent Tool。
    definition = read_only_registration.native_function_definition()
    assert definition["type"] == "function"
    assert definition["function"]["name"] == "mcp_a_echo"
    assert definition["function"]["parameters"] == {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }


def test_registration_skips_unavailable_servers_with_provenance():
    results = _build(
        (
            McpServerDiscoveryResult(
                server_id="broken",
                status=McpServerDiscoveryStatus.DISCOVERY_FAILED,
                safe_error_code="MCP_SERVER_UNAVAILABLE",
            ),
        ),
        (
            McpServerConfig(
                server_id="broken",
                command="unused",
                tools=(_read_only_mapping(),),
            ),
        ),
    )
    assert results[0].status is McpServerRegistrationStatus.SKIPPED
    assert results[0].safe_error_code == "MCP_SERVER_UNAVAILABLE"
    assert results[0].registrations == ()
    assert results[0].policies == ()


# ---- B. Collision ----


def test_same_remote_name_from_different_servers_gets_distinct_local_names():
    results = _build(
        (
            _available_result("server_a", (_descriptor("echo"),)),
            _available_result("server_b", (_descriptor("echo"),)),
        ),
        (
            McpServerConfig(
                server_id="server_a",
                command="unused",
                tools=(_read_only_mapping(local_name="mcp_a_echo"),),
            ),
            McpServerConfig(
                server_id="server_b",
                command="unused",
                tools=(_read_only_mapping(local_name="mcp_b_echo"),),
            ),
        ),
    )
    assert all(
        result.status is McpServerRegistrationStatus.REGISTERED
        for result in results
    )
    assert results[0].tool_names == ("mcp_a_echo",)
    assert results[1].tool_names == ("mcp_b_echo",)


def test_local_canonical_collision_fails_second_server_closed():
    results = _build(
        (
            _available_result("server_a", (_descriptor("echo"),)),
            _available_result("server_b", (_descriptor("echo"),)),
        ),
        (
            McpServerConfig(
                server_id="server_a",
                command="unused",
                tools=(_read_only_mapping(local_name="mcp_echo"),),
            ),
            McpServerConfig(
                server_id="server_b",
                command="unused",
                tools=(_read_only_mapping(local_name="mcp_echo"),),
            ),
        ),
    )
    assert results[0].status is McpServerRegistrationStatus.REGISTERED
    assert results[1].status is McpServerRegistrationStatus.REGISTRATION_FAILED
    assert results[1].safe_error_code == "MCP_TOOL_NAME_COLLISION"
    assert results[1].registrations == ()
    assert results[1].policies == ()


def test_collision_with_builtin_tool_fails_closed():
    results = _build(
        (_available_result("demo", (_descriptor("echo"),)),),
        (
            McpServerConfig(
                server_id="demo",
                command="unused",
                tools=(_read_only_mapping(local_name="workspace_read_file"),),
            ),
        ),
        existing=BUILTIN_TOOL_NAMES,
    )
    assert results[0].status is McpServerRegistrationStatus.REGISTRATION_FAILED
    assert results[0].safe_error_code == "MCP_TOOL_NAME_COLLISION"
    assert results[0].registrations == ()


# ---- C. Missing Policy ----


def test_missing_policy_mapping_fails_server_closed_with_zero_registration():
    results = _build(
        (
            _available_result(
                "demo", (_descriptor("echo"), _descriptor("unmapped"))
            ),
        ),
        (
            McpServerConfig(
                server_id="demo",
                command="unused",
                tools=(_read_only_mapping(),),
            ),
        ),
    )
    assert results[0].status is McpServerRegistrationStatus.REGISTRATION_FAILED
    assert results[0].safe_error_code == "MCP_TOOL_POLICY_MISSING"
    assert results[0].registrations == ()
    assert results[0].policies == ()


def test_unclassifiable_policy_combination_fails_closed():
    # (ARBITRARY_LOCAL_FILESYSTEM_READ, LOCAL_STATE_MUTATION, ...) 不在
    # Governance full-combination allowlist 内 -> registration fail closed。
    results = _build(
        (_available_result("demo", (_descriptor("echo"),)),),
        (
            McpServerConfig(
                server_id="demo",
                command="unused",
                tools=(
                    McpToolPolicyMapping(
                        remote_name="echo",
                        local_name="mcp_echo",
                        side_effect_kind="LOCAL_STATE_MUTATION",
                        idempotency="NON_IDEMPOTENT",
                        risk_facts=("ARBITRARY_LOCAL_FILESYSTEM_READ",),
                    ),
                ),
            ),
        ),
    )
    assert results[0].status is McpServerRegistrationStatus.REGISTRATION_FAILED
    assert results[0].safe_error_code == "MCP_TOOL_RISK_UNCLASSIFIED"
    assert results[0].registrations == ()


# ---- D. Metadata spoofing ----


def test_read_only_hint_cannot_override_local_policy_or_spec():
    # Provider 宣称 readOnlyHint=true；本地 policy/spec 判定 side-effect +
    # approval required：Local policy wins。
    results = _build(
        (
            _available_result(
                "demo",
                (_descriptor("echo", annotations={"readOnlyHint": True}),),
            ),
        ),
        (
            McpServerConfig(
                server_id="demo",
                command="unused",
                tools=(_mutation_mapping(),),
            ),
        ),
    )
    assert results[0].status is McpServerRegistrationStatus.REGISTERED
    registration = results[0].registrations[0]
    policy = results[0].policies[0]

    registry = ToolRegistry()
    for builtin in build_builtin_tool_registrations():
        registry.register(builtin)
    registry.register(registration)
    registry.freeze()
    catalog = ToolPolicyCatalog(
        tool_registry=registry, agent_registry=DEFAULT_AGENT_REGISTRY
    )
    register_default_tool_policies(catalog)
    catalog.register(policy)
    catalog.freeze()
    governance = ToolGovernanceService(catalog, DEFAULT_AGENT_REGISTRY)

    context = ToolGovernanceContext(
        principal_agent_id="core_router", run_id="run-1", step_id="step-1"
    )
    authorize_decision = governance.authorize_tool(context, registration)
    assert authorize_decision.outcome is ToolGovernanceOutcome.ALLOW

    invocation = registration.adapter.build_invocation('{"path": "a.txt"}')
    spec = registration.adapter.spec_for(invocation)
    decision = governance.evaluate_invocation(
        context, registration, invocation, spec
    )
    # 本地 spec（NON_IDEMPOTENT mutation -> HIGH）压过 provider readOnlyHint。
    assert decision.outcome is ToolGovernanceOutcome.APPROVAL_REQUIRED
    assert decision.risk_level is ToolRiskLevel.HIGH
    assert spec.side_effect_kind is ToolSideEffectKind.LOCAL_STATE_MUTATION


# ---- E. MCP tools/call success（经真实 ToolExecutionService）----


@pytest.mark.asyncio
async def test_tools_call_success_through_tool_execution_service(tmp_path):
    client = await _make_initialized_client(tmp_path, "call_success")
    try:
        registration = build_mcp_registrations(
            snapshot=McpDiscoverySnapshot(
                results=(_available_result("demo", tuple(await client.list_tools())),)
            ),
            configs=(_server_config(tmp_path, "call_success"),),
            session_resolver_factory=lambda server_id: (lambda: client),
            existing_tool_names=BUILTIN_TOOL_NAMES,
            request_timeout_seconds=10.0,
        )[0].registrations[0]
        adapter = registration.adapter
        invocation = adapter.build_invocation('{"path": "notes/a.txt"}')
        result = await ToolExecutionService().execute(
            invocation=invocation,
            adapter=adapter,
            run_context=_make_run_context(),
            step_id="step",
        )
        assert result.status is ToolExecutionStatus.SUCCEEDED
        assert result.output.content_type == "text/plain"
        assert "ECHO:" in result.output.content
        assert "notes/a.txt" in result.output.content
        assert result.safe_summary == "MCP Tool 调用已完成。"
        assert result.side_effect_state is ToolSideEffectState.NOT_STARTED
        assert result.idempotency_replayed is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_embedded_mixed_result_uses_existing_output_quota(tmp_path):
    mapping = McpToolPolicyMapping(
        remote_name="echo",
        local_name="mcp_echo",
        side_effect_kind="NONE",
        idempotency="READ_ONLY",
        max_output_bytes=14,
    )
    client = await _make_initialized_client(
        tmp_path, "call_embedded_mixed", tools=(mapping,)
    )
    try:
        registration = build_mcp_registrations(
            snapshot=McpDiscoverySnapshot(
                results=(_available_result("demo", tuple(await client.list_tools())),)
            ),
            configs=(_server_config(tmp_path, "call_embedded_mixed", tools=(mapping,)),),
            session_resolver_factory=lambda server_id: (lambda: client),
            existing_tool_names=BUILTIN_TOOL_NAMES,
            request_timeout_seconds=10.0,
        )[0].registrations[0]
        result = await ToolExecutionService().execute(
            invocation=registration.adapter.build_invocation('{"path": "a.txt"}'),
            adapter=registration.adapter,
            run_context=_make_run_context(),
            step_id="step",
        )
        normalized = "status\nbody 中\ntail"
        assert result.status is ToolExecutionStatus.SUCCEEDED
        assert result.output.content == "status\nbody "
        assert result.output.original_size_bytes == len(normalized.encode("utf-8"))
        assert result.output.returned_size_bytes == 12
        assert result.output.truncated is True
        assert result.output.digest == hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        assert "repo://" not in result.output.content
        assert "application/octet-stream" not in result.output.content
    finally:
        await client.close()


# ---- F/G. isError / unsupported content（adapter 级 + service 级）----


@pytest.mark.asyncio
async def test_is_error_maps_to_typed_failure_without_remote_payload(tmp_path):
    client = await _make_initialized_client(tmp_path, "call_is_error")
    try:
        registration = build_mcp_registrations(
            snapshot=McpDiscoverySnapshot(
                results=(_available_result("demo", tuple(await client.list_tools())),)
            ),
            configs=(_server_config(tmp_path, "call_is_error"),),
            session_resolver_factory=lambda server_id: (lambda: client),
            existing_tool_names=BUILTIN_TOOL_NAMES,
            request_timeout_seconds=10.0,
        )[0].registrations[0]
        adapter = registration.adapter
        invocation = adapter.build_invocation('{"path": "a.txt"}')
        with pytest.raises(ToolAdapterInvocationError) as excinfo:
            await adapter.invoke_once(invocation, _FakeAdapterContext())
        assert excinfo.value.category is ToolErrorCategory.OUTPUT_INVALID
        assert excinfo.value.safe_error_code == "MCP_TOOL_REPORTED_ERROR"
        assert excinfo.value.phase is ToolExecutionPhase.OUTPUT
        # raw server body 不得进入错误文本。
        assert "SECRET-REMOTE-STACK" not in str(excinfo.value)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_is_error_through_service_maps_to_tool_execution_error(tmp_path):
    client = await _make_initialized_client(tmp_path, "call_is_error")
    try:
        registration = build_mcp_registrations(
            snapshot=McpDiscoverySnapshot(
                results=(_available_result("demo", tuple(await client.list_tools())),)
            ),
            configs=(_server_config(tmp_path, "call_is_error"),),
            session_resolver_factory=lambda server_id: (lambda: client),
            existing_tool_names=BUILTIN_TOOL_NAMES,
            request_timeout_seconds=10.0,
        )[0].registrations[0]
        adapter = registration.adapter
        invocation = adapter.build_invocation('{"path": "a.txt"}')
        result = await ToolExecutionService().execute(
            invocation=invocation,
            adapter=adapter,
            run_context=_make_run_context(),
            step_id="step",
        )
        assert isinstance(result, ToolExecutionError)
        assert result.category is ToolErrorCategory.OUTPUT_INVALID
        assert result.safe_error_code == "MCP_TOOL_REPORTED_ERROR"
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["call_image_only", "call_structured_only"])
async def test_unsupported_content_fails_safely(tmp_path, mode):
    client = await _make_initialized_client(tmp_path, mode)
    try:
        registration = build_mcp_registrations(
            snapshot=McpDiscoverySnapshot(
                results=(_available_result("demo", tuple(await client.list_tools())),)
            ),
            configs=(_server_config(tmp_path, mode),),
            session_resolver_factory=lambda server_id: (lambda: client),
            existing_tool_names=BUILTIN_TOOL_NAMES,
            request_timeout_seconds=10.0,
        )[0].registrations[0]
        adapter = registration.adapter
        invocation = adapter.build_invocation('{"path": "a.txt"}')
        with pytest.raises(ToolAdapterInvocationError) as excinfo:
            await adapter.invoke_once(invocation, _FakeAdapterContext())
        assert excinfo.value.category is ToolErrorCategory.OUTPUT_INVALID
        assert excinfo.value.safe_error_code == "MCP_TOOL_RESULT_UNSUPPORTED"
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "mode",
        "mutation",
        "expected_category",
        "expected_state",
        "expected_authoritative",
    ),
    [
        (
            "call_embedded_blob",
            True,
            ToolErrorCategory.OUTPUT_INVALID,
            ToolSideEffectState.COMMITTED,
            True,
        ),
        (
            "call_is_error_embedded",
            False,
            ToolErrorCategory.OUTPUT_INVALID,
            ToolSideEffectState.NOT_STARTED,
            True,
        ),
        (
            "call_is_error_embedded",
            True,
            ToolErrorCategory.SIDE_EFFECT_UNKNOWN,
            ToolSideEffectState.NOT_STARTED,
            False,
        ),
    ],
)
async def test_embedded_failure_preserves_is_error_and_side_effect_semantics(
    tmp_path,
    mode,
    mutation,
    expected_category,
    expected_state,
    expected_authoritative,
):
    mapping = _mutation_mapping() if mutation else _read_only_mapping()
    client = await _make_initialized_client(tmp_path, mode, tools=(mapping,))
    try:
        registration = build_mcp_registrations(
            snapshot=McpDiscoverySnapshot(
                results=(_available_result("demo", tuple(await client.list_tools())),)
            ),
            configs=(_server_config(tmp_path, mode, tools=(mapping,)),),
            session_resolver_factory=lambda server_id: (lambda: client),
            existing_tool_names=BUILTIN_TOOL_NAMES,
            request_timeout_seconds=10.0,
        )[0].registrations[0]
        with pytest.raises(ToolAdapterInvocationError) as excinfo:
            await registration.adapter.invoke_once(
                registration.adapter.build_invocation('{"path": "a.txt"}'),
                _FakeAdapterContext(),
            )
        assert excinfo.value.category is expected_category
        assert excinfo.value.side_effect_state is expected_state
        assert excinfo.value.side_effect_state_authoritative is expected_authoritative
        assert "SECRET-RESOURCE-BODY" not in str(excinfo.value)
        if mode == "call_embedded_blob":
            assert excinfo.value.safe_error_code == "MCP_TOOL_RESULT_UNSUPPORTED"
        else:
            assert excinfo.value.safe_error_code == "MCP_TOOL_REPORTED_ERROR"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_rpc_error_and_malformed_result_fail_closed(tmp_path):
    for mode in ("call_rpc_error", "call_content_not_list"):
        client = await _make_initialized_client(tmp_path, mode)
        try:
            registration = build_mcp_registrations(
                snapshot=McpDiscoverySnapshot(
                    results=(
                        _available_result("demo", tuple(await client.list_tools())),
                    )
                ),
                configs=(_server_config(tmp_path, mode),),
                session_resolver_factory=lambda server_id: (lambda: client),
                existing_tool_names=BUILTIN_TOOL_NAMES,
                request_timeout_seconds=10.0,
            )[0].registrations[0]
            adapter = registration.adapter
            invocation = adapter.build_invocation('{"path": "a.txt"}')
            with pytest.raises(ToolAdapterInvocationError) as excinfo:
                await adapter.invoke_once(invocation, _FakeAdapterContext())
            assert excinfo.value.category is ToolErrorCategory.INTERNAL
            assert excinfo.value.safe_error_code.startswith("MCP_")
        finally:
            await client.close()


# ---- H. Timeout ----


@pytest.mark.asyncio
async def test_runtime_bounded_timeout_fails_typed(tmp_path):
    client = await _make_initialized_client(
        tmp_path, "call_hang", request_timeout_seconds=0.3
    )
    try:
        registration = build_mcp_registrations(
            snapshot=McpDiscoverySnapshot(
                results=(_available_result("demo", tuple(await client.list_tools())),)
            ),
            configs=(_server_config(tmp_path, "call_hang"),),
            session_resolver_factory=lambda server_id: (lambda: client),
            existing_tool_names=BUILTIN_TOOL_NAMES,
            # adapter 侧底层机制 timeout < Runtime deadline：确定性触发。
            request_timeout_seconds=0.3,
        )[0].registrations[0]
        adapter = registration.adapter
        invocation = adapter.build_invocation('{"path": "a.txt"}')
        started = time.monotonic()
        # max_attempts=1：隔离既有 Runtime retry（retry owner 未变化），
        # 确定性断言 adapter 把底层 timeout 映射为 typed TIMEOUT failure。
        service = ToolExecutionService(
            retry_executor=RetryExecutor(
                RetryPolicy(max_attempts=1, base_delay_seconds=0, max_delay_seconds=0)
            )
        )
        result = await service.execute(
            invocation=invocation,
            adapter=adapter,
            run_context=_make_run_context(timeout_seconds=10.0),
            step_id="step",
        )
        elapsed = time.monotonic() - started
        assert isinstance(result, ToolExecutionError)
        assert result.category is ToolErrorCategory.TIMEOUT
        assert result.safe_error_code == "MCP_TRANSPORT_TIMEOUT"
        assert elapsed < 5.0
    finally:
        await client.close(timeout=2.0)


# ---- I. Cancellation ----


@pytest.mark.asyncio
async def test_cancellation_sends_notification_and_ignores_late_response(tmp_path):
    marker = tmp_path / "cancel_marker.txt"
    client = await _make_initialized_client(
        tmp_path, "call_cancelable", cancel_marker=marker
    )
    try:
        registration = build_mcp_registrations(
            snapshot=McpDiscoverySnapshot(
                results=(_available_result("demo", tuple(await client.list_tools())),)
            ),
            configs=(
                _server_config(tmp_path, "call_cancelable", cancel_marker=marker),
            ),
            session_resolver_factory=lambda server_id: (lambda: client),
            existing_tool_names=BUILTIN_TOOL_NAMES,
            request_timeout_seconds=10.0,
        )[0].registrations[0]
        adapter = registration.adapter
        invocation = adapter.build_invocation('{"path": "a.txt"}')
        task = asyncio.create_task(
            adapter.invoke_once(invocation, _FakeAdapterContext())
        )
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # notifications/cancelled 已被 fake server 接收并写 marker。
        for _ in range(50):
            if marker.exists():
                break
            await asyncio.sleep(0.05)
        assert marker.exists()
        # 迟到响应不得破坏 client，也不得产生第二次完成。
        await asyncio.sleep(0.2)
        assert client.broken is False
        assert not client._pending
    finally:
        await client.close(timeout=2.0)
        assert client.exit_code is not None


@pytest.mark.asyncio
async def test_mutation_tool_calls_side_effect_checkpoint_and_reports_committed(
    tmp_path,
):
    client = await _make_initialized_client(tmp_path, "call_success")
    try:
        registration = build_mcp_registrations(
            snapshot=McpDiscoverySnapshot(
                results=(_available_result("demo", tuple(await client.list_tools())),)
            ),
            configs=(
                McpServerConfig(
                    server_id="demo",
                    command=sys.executable,
                    arguments=(
                        str(tmp_path / "fake_mcp_call_server.py"),
                    ),
                    environment=(("FAKE_MCP_MODE", "call_success"),),
                    tools=(_mutation_mapping(),),
                ),
            ),
            session_resolver_factory=lambda server_id: (lambda: client),
            existing_tool_names=BUILTIN_TOOL_NAMES,
            request_timeout_seconds=10.0,
        )[0].registrations[0]
        adapter = registration.adapter
        context = _FakeAdapterContext()
        invocation = adapter.build_invocation('{"path": "a.txt"}')
        response = await adapter.invoke_once(invocation, context)
        assert context.side_effect_calls == 1
        assert response.side_effect_state is ToolSideEffectState.COMMITTED
        assert response.side_effect_state_authoritative is True
        assert response.status is ToolExecutionStatus.SUCCEEDED
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mutation_embedded_success_reports_committed(tmp_path):
    mapping = _mutation_mapping()
    client = await _make_initialized_client(
        tmp_path, "call_embedded_mixed", tools=(mapping,)
    )
    try:
        registration = build_mcp_registrations(
            snapshot=McpDiscoverySnapshot(
                results=(_available_result("demo", tuple(await client.list_tools())),)
            ),
            configs=(_server_config(tmp_path, "call_embedded_mixed", tools=(mapping,)),),
            session_resolver_factory=lambda server_id: (lambda: client),
            existing_tool_names=BUILTIN_TOOL_NAMES,
            request_timeout_seconds=10.0,
        )[0].registrations[0]
        context = _FakeAdapterContext()
        response = await registration.adapter.invoke_once(
            registration.adapter.build_invocation('{"path": "a.txt"}'), context
        )
        assert context.side_effect_calls == 1
        assert response.content == "status\nbody 中\ntail"
        assert response.side_effect_state is ToolSideEffectState.COMMITTED
        assert response.side_effect_state_authoritative is True
    finally:
        await client.close()


# ---- J. No bypass ----


def test_mcp_execution_only_reachable_through_existing_runtime_path():
    """结构证明：MCP execution 只能发生在 ToolExecutionService -> adapter
    .invoke_once 之后；Governance/Registry/ExecutionService/AgentRouter 不
    import mcp，也不直接调用 MCP client。"""
    repo_root = Path(__file__).resolve().parents[1]
    runtime_files = (
        repo_root / "core" / "agent_router.py",
        repo_root / "core" / "runtime" / "tool_execution.py",
        repo_root / "core" / "runtime" / "tool_registry.py",
        repo_root / "core" / "runtime" / "tool_governance.py",
        repo_root / "core" / "runtime" / "tool_contract.py",
        repo_root / "core" / "runtime" / "tool_adapters.py",
    )
    for source_path in runtime_files:
        source = source_path.read_text(encoding="utf-8")
        assert "import mcp" not in source, source_path
        assert "from mcp" not in source, source_path
        assert "call_tool" not in source, source_path
        assert "session_for" not in source, source_path

    adapter_source = (repo_root / "mcp" / "adapter.py").read_text(encoding="utf-8")
    # call_tool 只出现在 adapter.invoke_once 的执行路径内。
    assert adapter_source.count(".call_tool(") == 1
    assert "self._session_resolver()" in adapter_source
    mcp_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((repo_root / "mcp").glob("*.py"))
    )
    assert '"resources/read"' not in mcp_sources
    assert '"resources/list"' not in mcp_sources


# ---- build_invocation validation boundary ----


@pytest.mark.asyncio
async def test_build_invocation_validates_arguments_against_declared_schema(tmp_path):
    client = await _make_initialized_client(tmp_path, "call_success")
    try:
        registration = build_mcp_registrations(
            snapshot=McpDiscoverySnapshot(
                results=(_available_result("demo", tuple(await client.list_tools())),)
            ),
            configs=(_server_config(tmp_path, "call_success"),),
            session_resolver_factory=lambda server_id: (lambda: client),
            existing_tool_names=BUILTIN_TOOL_NAMES,
            request_timeout_seconds=10.0,
        )[0].registrations[0]
        adapter = registration.adapter

        invocation = adapter.build_invocation('{"path": "a.txt"}')
        assert invocation.tool_name == "mcp_echo"
        assert invocation.arguments["path"] == "a.txt"

        for bad_arguments in (
            "not-json",
            "[1, 2]",
            '"scalar"',
            "{}",
            '{"path": 1}',
            '{"path": ["a.txt"]}',
        ):
            with pytest.raises(ToolAdapterInvocationError) as excinfo:
                adapter.build_invocation(bad_arguments)
            assert excinfo.value.category is ToolErrorCategory.VALIDATION
            assert excinfo.value.safe_error_code == "TOOL_VALIDATION_ERROR"
    finally:
        await client.close()
