"""Stage7-WP6：真实 stdio 子进程的 MCP resilient lifecycle 回归。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

import pytest

from core.runtime.budget import BudgetLedger, RunBudget
from core.runtime.context import RunContext
from core.runtime.run_control import DurableRunControlService
from core.runtime.retry import OperationIdempotency
from core.runtime.tool_adapters import (
    ToolAdapterInvocationError,
    ToolAdapterResponse,
)
from core.runtime.tool_execution import ToolExecutionService
from core.runtime.tool_idempotency import DurableToolInvocationService, ToolInvocationState
from core.runtime.agent_registry import DEFAULT_AGENT_REGISTRY
from core.runtime.tool_governance import ToolPolicyCatalog
from core.runtime.tool_registry import ToolRegistry
from core.runtime.tool_contract import (
    ToolExecutionError,
    ToolExecutionSpec,
    ToolInvocation,
    ToolSideEffectKind,
)
from mcp.adapter import McpBackedToolAdapter
from mcp.client import StdioMcpClient
from mcp.config import McpServerConfig, McpToolPolicyMapping
from mcp.discovery import McpServerDiscoveryOutcome
from mcp.errors import McpTransportClosedError
from mcp.lifecycle import McpIntegrationComponent, McpSessionLifecycleState
from mcp.models import (
    MCP_PROTOCOL_VERSION,
    McpServerDiscoveryResult,
    McpServerDiscoveryStatus,
    McpToolCallResult,
    McpToolDescriptor,
)
from mcp.registration import build_mcp_registrations, McpServerRegistrationStatus


REAL_SERVER_SOURCE = r'''
import json
import os
import sys

STATE = os.environ["MCP_STATE_PATH"]
CALLS = os.environ.get("MCP_CALLS_PATH")
MODE = os.environ.get("MCP_MODE", "stable")
try:
    launch = int(open(STATE, "r", encoding="utf-8").read()) + 1
except (FileNotFoundError, ValueError):
    launch = 1
with open(STATE, "w", encoding="utf-8") as handle:
    handle.write(str(launch))

def send(value):
    sys.stdout.write(json.dumps(value) + "\n")
    sys.stdout.flush()

def tool(name, changed=False):
    schema = {"type": "object", "properties": {"path": {"type": "string"}}}
    if changed:
        schema["required"] = ["path"]
    return {"name": name, "description": "deterministic tool", "inputSchema": schema}

while True:
    line = sys.stdin.readline()
    if not line:
        break
    message = json.loads(line)
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        identity = "changed" if MODE == "identity_mismatch" and launch >= 2 else "stable"
        version = "2" if MODE == "version_drift" and launch >= 2 else "1"
        send({"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": "PROTOCOL_VERSION",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "provider-" + identity, "version": version},
        }})
    elif method == "tools/list":
        changed = MODE == "schema_mismatch" and launch >= 2
        tools = [tool("echo", changed)]
        if MODE != "missing_tool" or launch < 2:
            tools.append(tool("reverse", changed=False))
        if MODE == "extra_tool" and launch >= 2:
            tools.append(tool("new_unfrozen", changed=False))
        send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools}})
    elif method == "tools/call":
        if CALLS:
            try:
                call_count = int(open(CALLS, "r", encoding="utf-8").read()) + 1
            except (FileNotFoundError, ValueError):
                call_count = 1
            with open(CALLS, "w", encoding="utf-8") as handle:
                handle.write(str(call_count))
        if MODE == "disconnect_call":
            sys.exit(0)
        send({"jsonrpc": "2.0", "id": request_id,
              "result": {"content": [{"type": "text", "text": "wire-ok"}]}})
    elif request_id is not None:
        send({"jsonrpc": "2.0", "id": request_id,
              "error": {"code": -32601, "message": "unsupported"}})
'''.replace("PROTOCOL_VERSION", MCP_PROTOCOL_VERSION)


def _config(
    tmp_path: Path,
    mode: str,
    *,
    tools: tuple[McpToolPolicyMapping, ...] = (),
) -> McpServerConfig:
    script = tmp_path / "real_mcp_server.py"
    script.write_text(REAL_SERVER_SOURCE, encoding="utf-8")
    state = tmp_path / "launches.txt"
    calls = tmp_path / "calls.txt"
    return McpServerConfig(
        server_id="demo",
        command=sys.executable,
        arguments=(str(script),),
        environment=(
            ("MCP_MODE", mode),
            ("MCP_STATE_PATH", str(state)),
            ("MCP_CALLS_PATH", str(calls)),
        ),
        tools=tools,
    )


async def _wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not reached")
        await asyncio.sleep(0.01)


async def _force_exit(component: McpIntegrationComponent) -> StdioMcpClient:
    client = component.session_for("demo")
    assert isinstance(client, StdioMcpClient)
    assert client._process is not None
    client._process.terminate()
    await _wait_for(lambda: component.state_for("demo") is not McpSessionLifecycleState.AVAILABLE)
    return client


@pytest.mark.asyncio
async def test_real_stdio_startup_generation_and_successful_reconnect(tmp_path: Path):
    component = McpIntegrationComponent(
        (_config(tmp_path, "stable"),),
        connect_timeout_seconds=2,
        request_timeout_seconds=2,
        reconnect_initial_delay_seconds=0.01,
        reconnect_max_delay_seconds=0.05,
        reconnect_max_attempts=2,
        reconnect_jitter_ratio=0,
    )
    try:
        await component.start()
        assert component.state_for("demo") is McpSessionLifecycleState.AVAILABLE
        assert component.session_generation_for("demo") == 1
        old_handle = component.acquire_session("demo")
        assert old_handle is not None
        old = await _force_exit(component)
        await _wait_for(lambda: component.session_generation_for("demo") == 2)
        assert component.state_for("demo") is McpSessionLifecycleState.AVAILABLE
        assert component.session_for("demo") is not old
        assert old_handle.client is old
        assert component.acquire_session("demo").client is not old
        result = await component.session_for("demo").call_tool("echo", {}, timeout=1)
        assert result.text_parts == ("wire-ok",)
        await _wait_for(lambda: old.closed)
    finally:
        await component.close()


@pytest.mark.asyncio
async def test_remote_version_drift_is_accepted(tmp_path: Path):
    component = McpIntegrationComponent(
        (_config(tmp_path, "version_drift"),),
        connect_timeout_seconds=2,
        request_timeout_seconds=2,
        reconnect_initial_delay_seconds=0,
        reconnect_max_delay_seconds=0,
        reconnect_max_attempts=1,
        reconnect_jitter_ratio=0,
    )
    try:
        await component.start()
        await _force_exit(component)
        await _wait_for(lambda: component.session_generation_for("demo") == 2)
        assert component.state_for("demo") is McpSessionLifecycleState.AVAILABLE
        assert component.discovery_snapshot.result_for("demo").server_info_version == "1"
        assert any(event["event"] == "reconnected" for event in component.lifecycle_events)
    finally:
        await component.close()


@pytest.mark.asyncio
async def test_reconnect_preserves_frozen_registry_policy_and_spec_identity(tmp_path: Path):
    config = _config(
        tmp_path,
        "stable",
        tools=(
            McpToolPolicyMapping(
                remote_name="echo",
                local_name="mcp_echo",
                side_effect_kind="NONE",
                idempotency="READ_ONLY",
            ),
            McpToolPolicyMapping(
                remote_name="reverse",
                local_name="mcp_reverse",
                side_effect_kind="NONE",
                idempotency="READ_ONLY",
            ),
        ),
    )
    component = McpIntegrationComponent(
        (config,),
        connect_timeout_seconds=2,
        request_timeout_seconds=2,
        reconnect_initial_delay_seconds=0,
        reconnect_max_delay_seconds=0,
        reconnect_max_attempts=1,
        reconnect_jitter_ratio=0,
    )
    try:
        await component.start()
        registration_results = build_mcp_registrations(
            snapshot=component.discovery_snapshot,
            configs=(config,),
            session_resolver_factory=lambda server_id: (
                lambda: component.session_for(server_id)
            ),
            existing_tool_names=frozenset(),
            request_timeout_seconds=1,
        )
        assert registration_results[0].status is McpServerRegistrationStatus.REGISTERED
        registry = ToolRegistry()
        for registration in registration_results[0].registrations:
            registry.register(registration)
        registry.freeze()
        catalog = ToolPolicyCatalog(
            tool_registry=registry,
            agent_registry=DEFAULT_AGENT_REGISTRY,
        )
        for policy in registration_results[0].policies:
            catalog.register(policy)
        catalog.freeze()
        registrations_before = registry.registrations()
        policies_before = catalog.policies()
        specs_before = tuple(
            (item.adapter.spec.tool_name, id(item.adapter.spec))
            for item in registrations_before
        )
        old = await _force_exit(component)
        await _wait_for(lambda: component.session_generation_for("demo") == 2)
        assert component.session_for("demo") is not old
        assert registry.registrations() == registrations_before
        assert catalog.policies() == policies_before
        assert tuple(
            (item.adapter.spec.tool_name, id(item.adapter.spec))
            for item in registry.registrations()
        ) == specs_before
        assert all(
            registration.adapter._session_resolver() is component.session_for("demo")
            for registration in registry.registrations()
        )
        assert component.session_for("demo").session_generation == 2
    finally:
        await component.close()


@pytest.mark.asyncio
async def test_immediate_post_publish_break_schedules_one_pending_reconnect(monkeypatch):
    descriptor = McpToolDescriptor.from_protocol_payload(
        "demo", {"name": "echo", "inputSchema": {"type": "object"}}
    )
    result = McpServerDiscoveryResult(
        server_id="demo",
        status=McpServerDiscoveryStatus.AVAILABLE,
        protocol_version=MCP_PROTOCOL_VERSION,
        server_info_name="provider",
        server_info_version="1",
        tools=(descriptor,),
    )

    class Candidate:
        def __init__(self, *, break_on_callback=False):
            self.server_id = "demo"
            self.closed = False
            self.broken = False
            self.broken_error = None
            self.exit_code = None
            self.session_generation = 0
            self._break_on_callback = break_on_callback

        def set_session_generation(self, generation):
            self.session_generation = generation

        def set_broken_callback(self, callback):
            if self._break_on_callback:
                self.broken = True
                self.broken_error = McpTransportClosedError("connection_broken")
                callback(self, self.broken_error)

        async def close(self, timeout=3):
            self.closed = True
            return True

    initial = Candidate()
    replacement = Candidate(break_on_callback=True)
    final = Candidate()
    candidates = iter((initial, replacement, final))
    calls = 0
    active = 0
    max_active = 0

    async def fake_discover(*args, **kwargs):
        nonlocal calls, active, max_active
        calls += 1
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        active -= 1
        return McpServerDiscoveryOutcome(result=result, client=next(candidates))

    monkeypatch.setattr("mcp.lifecycle.discover_server", fake_discover)
    config = McpServerConfig(server_id="demo", command="unused")
    component = McpIntegrationComponent(
        (config,),
        connect_timeout_seconds=1,
        request_timeout_seconds=1,
        reconnect_initial_delay_seconds=0,
        reconnect_max_delay_seconds=0,
        reconnect_max_attempts=1,
        reconnect_jitter_ratio=0,
    )
    try:
        await component.start()
        component._on_client_broken(initial, McpTransportClosedError("eof"))
        await _wait_for(lambda: component.session_generation_for("demo") == 3)
        assert calls == 3
        assert max_active == 1
        assert component.state_for("demo") is McpSessionLifecycleState.AVAILABLE
        assert replacement.closed is True
    finally:
        await component.close()


@pytest.mark.asyncio
async def test_reconnect_backoff_is_bounded_exponential_with_jitter(monkeypatch):
    descriptor = McpToolDescriptor.from_protocol_payload(
        "demo", {"name": "echo", "inputSchema": {"type": "object"}}
    )
    frozen = McpServerDiscoveryResult(
        server_id="demo",
        status=McpServerDiscoveryStatus.AVAILABLE,
        protocol_version=MCP_PROTOCOL_VERSION,
        server_info_name="provider",
        server_info_version="1",
        tools=(descriptor,),
    )
    failed = McpServerDiscoveryResult(
        server_id="demo",
        status=McpServerDiscoveryStatus.DISCOVERY_FAILED,
        safe_error_code="MCP_SERVER_UNAVAILABLE",
    )
    sleeps = []

    async def record_sleep(delay):
        sleeps.append(delay)
        await asyncio.sleep(0)

    async def failed_discover(*args, **kwargs):
        return McpServerDiscoveryOutcome(result=failed, client=None)

    monkeypatch.setattr("mcp.lifecycle.discover_server", failed_discover)
    component = McpIntegrationComponent(
        (McpServerConfig(server_id="demo", command="unused"),),
        connect_timeout_seconds=1,
        request_timeout_seconds=1,
        reconnect_initial_delay_seconds=0.1,
        reconnect_max_delay_seconds=0.15,
        reconnect_max_attempts=3,
        reconnect_jitter_ratio=0.1,
        sleep=record_sleep,
        jitter_source=lambda: 1.0,
    )
    component._started = True
    component._frozen_results["demo"] = frozen
    component._states["demo"] = McpSessionLifecycleState.DEGRADED
    component._schedule_reconnect("demo", 1)
    await _wait_for(lambda: component.reconnect_task_for("demo") is None)
    assert sleeps == pytest.approx([0.11, 0.165, 0.165])
    assert any(event["event"] == "reconnect_exhausted" for event in component.lifecycle_events)
    await component.close()


@pytest.mark.asyncio
async def test_queued_pending_reconnect_callback_after_close_is_ignored(monkeypatch):
    discover_calls = 0

    async def should_not_discover(*args, **kwargs):
        nonlocal discover_calls
        discover_calls += 1
        raise AssertionError("closed component must not rediscover")

    monkeypatch.setattr("mcp.lifecycle.discover_server", should_not_discover)
    component = McpIntegrationComponent(
        (McpServerConfig(server_id="demo", command="unused"),),
        connect_timeout_seconds=1,
        request_timeout_seconds=1,
    )
    component._started = True
    component._states["demo"] = McpSessionLifecycleState.DEGRADED
    component._pending_reconnects["demo"] = 1
    # 这就是 reconnect task done callback 排队后、但尚未执行的最小等价
    # 调用；close 先把 component 置 CLOSED，再执行 callback。
    queued_done_callback = lambda _done: component._schedule_reconnect("demo", 1)
    assert await component.close() is True
    queued_done_callback(None)
    await asyncio.sleep(0)
    assert component.state_for("demo") is McpSessionLifecycleState.CLOSED
    assert component.reconnect_task_for("demo") is None
    assert component._pending_reconnects == {}
    assert component._background_tasks == set()
    assert discover_calls == 0


@pytest.mark.asyncio
async def test_durable_side_effect_disconnect_stays_unknown_after_reconnect(
    tmp_path: Path, clean_database
):
    config = _config(tmp_path, "disconnect_call")
    component = McpIntegrationComponent(
        (config,),
        connect_timeout_seconds=2,
        request_timeout_seconds=2,
        reconnect_initial_delay_seconds=0.01,
        reconnect_max_delay_seconds=0.05,
        reconnect_max_attempts=2,
        reconnect_jitter_ratio=0,
    )
    run = DurableRunControlService(clean_database)
    lease = await run.claim("wp6-mcp-run", "wp6-mcp-owner")
    context = RunContext.create(entry_agent_id="core_router", run_id=lease.run_id)
    context.attach_durable_lease(lease)
    context.attach_budget_ledger(BudgetLedger(RunBudget(max_tool_calls=2)))
    service = DurableToolInvocationService(clean_database)
    invocation = ToolInvocation.create(
        tool_name="mcp_write",
        idempotency_key="wp6-mcp-side-effect",
        resource_key="wp6-mcp-resource",
        arguments={},
    )
    adapter = McpBackedToolAdapter(
        spec=ToolExecutionSpec(
            tool_name="mcp_write",
            side_effect_kind=ToolSideEffectKind.EXTERNAL_STATE_MUTATION,
            idempotency=OperationIdempotency.NON_IDEMPOTENT,
            supports_side_effect_checkpoint=True,
        ),
        server_id="demo",
        remote_name="echo",
        input_schema={"type": "object"},
        session_resolver=lambda: component.session_for("demo"),
        request_timeout_seconds=1,
    )
    try:
        await component.start()
        outcome = await ToolExecutionService(
            durable_invocation_service=service
        ).execute(
            invocation=invocation,
            adapter=adapter,
            run_context=context,
            step_id="answer",
        )
        assert isinstance(outcome, ToolExecutionError)
        record = await service.get(invocation.invocation_id)
        assert record is not None
        assert record.state is ToolInvocationState.UNKNOWN
        assert adapter.replay_events[0]["event"] == "side_effect_replay_rejected"
        await _wait_for(lambda: component.session_generation_for("demo") == 2)
        assert int((tmp_path / "calls.txt").read_text(encoding="utf-8")) == 1
        assert (await service.get(invocation.invocation_id)).state is ToolInvocationState.UNKNOWN
    finally:
        await component.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "error_code"),
    [
        ("identity_mismatch", "MCP_PROVIDER_IDENTITY_MISMATCH"),
        ("schema_mismatch", "MCP_TOOL_SCHEMA_MISMATCH"),
        ("missing_tool", "MCP_REQUIRED_TOOL_MISSING"),
    ],
)
async def test_reconnect_incompatibility_fails_closed(tmp_path: Path, mode: str, error_code: str):
    component = McpIntegrationComponent(
        (_config(tmp_path, mode),),
        connect_timeout_seconds=2,
        request_timeout_seconds=2,
        reconnect_initial_delay_seconds=0,
        reconnect_max_delay_seconds=0,
        reconnect_max_attempts=1,
        reconnect_jitter_ratio=0,
    )
    try:
        await component.start()
        await _force_exit(component)
        await _wait_for(lambda: component.reconnect_task_for("demo") is None)
        assert component.state_for("demo") is McpSessionLifecycleState.DEGRADED
        assert component.session_for("demo") is None
        assert any(event["safe_error_code"] == error_code for event in component.lifecycle_events)
    finally:
        await component.close()


@pytest.mark.asyncio
async def test_extra_tool_is_ignored_and_snapshot_is_not_mutated(tmp_path: Path):
    component = McpIntegrationComponent(
        (_config(tmp_path, "extra_tool"),),
        connect_timeout_seconds=2,
        request_timeout_seconds=2,
        reconnect_initial_delay_seconds=0,
        reconnect_max_delay_seconds=0,
        reconnect_max_attempts=1,
        reconnect_jitter_ratio=0,
    )
    try:
        await component.start()
        original_tools = [tool.remote_name for tool in component.discovery_snapshot.available_tools()]
        await _force_exit(component)
        await _wait_for(lambda: component.session_generation_for("demo") == 2)
        assert [tool.remote_name for tool in component.discovery_snapshot.available_tools()] == original_tools
        assert all("new_unfrozen" not in json.dumps(event) for event in component.lifecycle_events)
    finally:
        await component.close()


@pytest.mark.asyncio
async def test_shutdown_cancels_reconnect_and_closes_process(tmp_path: Path):
    component = McpIntegrationComponent(
        (_config(tmp_path, "stable"),),
        connect_timeout_seconds=2,
        request_timeout_seconds=2,
        reconnect_initial_delay_seconds=0.5,
        reconnect_max_delay_seconds=0.5,
        reconnect_max_attempts=3,
        reconnect_jitter_ratio=0,
    )
    await component.start()
    old = await _force_exit(component)
    assert component.reconnect_task_for("demo") is not None
    assert await component.close() is True
    assert component.state_for("demo") is McpSessionLifecycleState.CLOSED
    await asyncio.sleep(0.1)
    assert component.reconnect_task_for("demo") is None
    assert component._background_tasks == set()
    assert component._owned_clients == set()
    assert old.closed is True


class _AdapterContext:
    def raise_if_cancelled(self) -> None:
        return None

    def before_side_effect(self) -> None:
        return None

    def remaining_seconds(self) -> float:
        return 2.0


class _ReplayClient:
    def __init__(self, generation: int, *, fail: bool = False) -> None:
        self.session_generation = generation
        self.closed = False
        self.broken = fail
        self.reconnect_managed = True
        self.calls = 0
        self._fail = fail

    async def call_tool(self, remote_name, arguments, timeout):
        self.calls += 1
        if self._fail:
            raise McpTransportClosedError("connection_broken")
        return McpToolCallResult(is_error=False, text_parts=("replayed",))


@pytest.mark.asyncio
async def test_read_only_disconnect_replays_once_on_new_generation():
    first = _ReplayClient(1)
    second = _ReplayClient(2)
    current = {"client": first}

    async def first_call(remote_name, arguments, timeout):
        current["client"] = second
        raise McpTransportClosedError("connection_broken")

    first.call_tool = first_call
    adapter = McpBackedToolAdapter(
        spec=ToolExecutionSpec(
            tool_name="mcp_read",
            side_effect_kind=ToolSideEffectKind.NONE,
            idempotency=OperationIdempotency.READ_ONLY,
        ),
        server_id="demo",
        remote_name="read",
        input_schema={"type": "object"},
        session_resolver=lambda: current["client"],
        request_timeout_seconds=1,
    )
    result = await adapter.invoke_once(
        ToolInvocation.create(tool_name="mcp_read", arguments={}), _AdapterContext()
    )
    assert isinstance(result, ToolAdapterResponse)
    assert result.content == "replayed"
    assert second.calls == 1
    assert adapter.replay_events[0]["replay_count"] == 1


@pytest.mark.asyncio
async def test_read_only_replacement_failure_has_no_third_attempt():
    first = _ReplayClient(1)
    second = _ReplayClient(2)
    current = {"client": first}

    async def first_call(remote_name, arguments, timeout):
        first.calls += 1
        current["client"] = second
        raise McpTransportClosedError("connection_broken")

    async def second_call(remote_name, arguments, timeout):
        second.calls += 1
        raise McpTransportClosedError("connection_broken")

    first.call_tool = first_call
    second.call_tool = second_call
    adapter = McpBackedToolAdapter(
        spec=ToolExecutionSpec(
            tool_name="mcp_read",
            side_effect_kind=ToolSideEffectKind.NONE,
            idempotency=OperationIdempotency.READ_ONLY,
        ),
        server_id="demo",
        remote_name="read",
        input_schema={"type": "object"},
        session_resolver=lambda: current["client"],
        request_timeout_seconds=1,
    )
    with pytest.raises(ToolAdapterInvocationError) as excinfo:
        await adapter.invoke_once(
            ToolInvocation.create(tool_name="mcp_read", arguments={}), _AdapterContext()
        )
    assert excinfo.value.safe_error_code == "MCP_READ_ONLY_REPLAY_EXHAUSTED"
    assert first.calls == 1
    assert second.calls == 1
    assert len(adapter.replay_events) == 1


@pytest.mark.asyncio
async def test_read_only_replay_deadline_and_cancellation_do_not_replay():
    first = _ReplayClient(1)
    current = {"client": first}

    async def fail_call(remote_name, arguments, timeout):
        first.calls += 1
        raise McpTransportClosedError("connection_broken")

    first.call_tool = fail_call
    adapter = McpBackedToolAdapter(
        spec=ToolExecutionSpec(
            tool_name="mcp_read",
            side_effect_kind=ToolSideEffectKind.NONE,
            idempotency=OperationIdempotency.READ_ONLY,
        ),
        server_id="demo",
        remote_name="read",
        input_schema={"type": "object"},
        session_resolver=lambda: current["client"],
        request_timeout_seconds=1,
    )

    class DeadlineContext(_AdapterContext):
        def remaining_seconds(self):
            return 0.0

    with pytest.raises(ToolAdapterInvocationError):
        await adapter.invoke_once(
            ToolInvocation.create(tool_name="mcp_read", arguments={}), DeadlineContext()
        )
    assert first.calls == 1

    cancelled = {"value": False}

    class CancelContext(_AdapterContext):
        def raise_if_cancelled(self):
            if cancelled["value"]:
                raise asyncio.CancelledError

    async def cancel_call(remote_name, arguments, timeout):
        first.calls += 1
        cancelled["value"] = True
        raise McpTransportClosedError("connection_broken")

    first.call_tool = cancel_call
    with pytest.raises(asyncio.CancelledError):
        await adapter.invoke_once(
            ToolInvocation.create(tool_name="mcp_read", arguments={}), CancelContext()
        )
    assert first.calls == 2
    assert len(adapter.replay_events) == 0


@pytest.mark.asyncio
async def test_side_effect_disconnect_is_not_replayed():
    first = _ReplayClient(1)
    calls = {"current": first}

    async def fail_call(remote_name, arguments, timeout):
        first.calls += 1
        raise McpTransportClosedError("connection_broken")

    first.call_tool = fail_call
    adapter = McpBackedToolAdapter(
        spec=ToolExecutionSpec(
            tool_name="mcp_write",
            side_effect_kind=ToolSideEffectKind.LOCAL_STATE_MUTATION,
            idempotency=OperationIdempotency.NON_IDEMPOTENT,
        ),
        server_id="demo",
        remote_name="write",
        input_schema={"type": "object", "annotations": {"readOnlyHint": True}},
        session_resolver=lambda: calls["current"],
        request_timeout_seconds=1,
    )
    with pytest.raises(ToolAdapterInvocationError):
        await adapter.invoke_once(
            ToolInvocation.create(tool_name="mcp_write", arguments={}), _AdapterContext()
        )
    assert first.calls == 1
    assert adapter.replay_events[0]["event"] == "side_effect_replay_rejected"
    assert adapter.replay_events[0]["server_id"] == "demo"
    assert adapter.replay_events[0]["generation"] == 1
    assert adapter.replay_events[0]["replay_count"] == 0
    assert adapter.replay_events[0]["reason"] == "side_effect_replay_forbidden"
