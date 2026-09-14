#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""MCP application-scoped resilient session lifecycle。

``McpIntegrationComponent`` 是唯一的 session/reconnect owner。它只发布已完成
initialize、identity 与 frozen tool/schema 校验的 session generation；Registry、
Governance 与 Tool Runtime 仍由既有模块拥有。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
import logging
import random
from typing import Awaitable, Callable

from mcp.client import StdioMcpClient
from mcp.config import McpServerConfig
from mcp.discovery import discover_server
from mcp.errors import McpBoundaryError
from mcp.models import (
    McpDiscoverySnapshot,
    McpServerDiscoveryResult,
    McpServerDiscoveryStatus,
)

logger = logging.getLogger(__name__)

_DEFAULT_COMPONENT_CLOSE_TIMEOUT_SECONDS = 3.0
_MIN_CLIENT_CLOSE_TIMEOUT_SECONDS = 0.05
_DEFAULT_RECONNECT_INITIAL_DELAY_SECONDS = 0.1
_DEFAULT_RECONNECT_MAX_DELAY_SECONDS = 2.0
_DEFAULT_RECONNECT_MAX_ATTEMPTS = 3
_DEFAULT_RECONNECT_JITTER_RATIO = 0.1


class McpSessionLifecycleState(str, Enum):
    STARTING = "STARTING"
    AVAILABLE = "AVAILABLE"
    DEGRADED = "DEGRADED"
    RECONNECTING = "RECONNECTING"
    DISABLED = "DISABLED"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class McpSessionHandle:
    """不可变 invocation 绑定；不会在 generation swap 后换底层 session。"""

    server_id: str
    session_generation: int
    client: StdioMcpClient
    provider_identity: tuple[str, str]

    @property
    def generation(self) -> int:
        return self.session_generation


@dataclass(frozen=True, slots=True)
class _SessionBinding:
    client: StdioMcpClient
    generation: int
    provider_identity: tuple[str, str]


class McpIntegrationComponent:
    """application-scope MCP client/session owner 与 resilient lifecycle owner。"""

    def __init__(
        self,
        server_configs: tuple[McpServerConfig, ...],
        *,
        connect_timeout_seconds: float,
        request_timeout_seconds: float,
        reconnect_initial_delay_seconds: float = _DEFAULT_RECONNECT_INITIAL_DELAY_SECONDS,
        reconnect_max_delay_seconds: float = _DEFAULT_RECONNECT_MAX_DELAY_SECONDS,
        reconnect_max_attempts: int = _DEFAULT_RECONNECT_MAX_ATTEMPTS,
        reconnect_jitter_ratio: float = _DEFAULT_RECONNECT_JITTER_RATIO,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter_source: Callable[[], float] = random.random,
    ) -> None:
        if reconnect_initial_delay_seconds < 0 or reconnect_max_delay_seconds < 0:
            raise ValueError("reconnect delay 必须是非负数")
        if reconnect_max_delay_seconds < reconnect_initial_delay_seconds:
            raise ValueError("reconnect_max_delay_seconds 不能小于初始 delay")
        if isinstance(reconnect_max_attempts, bool) or reconnect_max_attempts <= 0:
            raise ValueError("reconnect_max_attempts 必须是正整数")
        if not 0 <= reconnect_jitter_ratio <= 1:
            raise ValueError("reconnect_jitter_ratio 必须在 [0, 1] 内")
        self._server_configs = tuple(server_configs)
        self._configs_by_id = {config.server_id: config for config in self._server_configs}
        self._connect_timeout_seconds = float(connect_timeout_seconds)
        self._request_timeout_seconds = float(request_timeout_seconds)
        self._reconnect_initial_delay = float(reconnect_initial_delay_seconds)
        self._reconnect_max_delay = float(reconnect_max_delay_seconds)
        self._reconnect_max_attempts = reconnect_max_attempts
        self._reconnect_jitter_ratio = float(reconnect_jitter_ratio)
        self._sleep = sleep
        self._jitter_source = jitter_source
        self._sessions: dict[str, _SessionBinding] = {}
        self._frozen_results: dict[str, McpServerDiscoveryResult] = {}
        self._states: dict[str, McpSessionLifecycleState] = {}
        self._reconnect_tasks: dict[str, asyncio.Task] = {}
        # broken callback 可能来自 client reader/process watcher task，而非
        # reconnect coroutine。活跃 owner 存在时只登记 intent，待 owner done
        # 后再启动下一轮，保证单 server 始终 singleflight。
        self._pending_reconnects: dict[str, int] = {}
        self._background_tasks: set[asyncio.Task] = set()
        # 所有由 application component 创建/接收的 client 都属于 component，
        # 即使 broken 后从 current session binding 移除，也必须在 shutdown
        # 最终 close，避免 cleanup task 被取消后遗留 child。
        self._owned_clients: set[StdioMcpClient] = set()
        self._snapshot: McpDiscoverySnapshot | None = None
        self._events: list[dict[str, object]] = []
        self._started = False
        self._closed = False

    async def start(self) -> None:
        """完成初始 initialize + tools/list；失败 server 进入 DEGRADED。"""
        if self._started:
            raise RuntimeError("mcp integration component already started")
        if self._closed:
            raise RuntimeError("mcp integration component already closed")
        results: list[McpServerDiscoveryResult] = []
        for server_config in self._server_configs:
            if not server_config.enabled:
                self._states[server_config.server_id] = McpSessionLifecycleState.DISABLED
                results.append(McpServerDiscoveryResult(
                    server_id=server_config.server_id,
                    status=McpServerDiscoveryStatus.DISABLED,
                ))
                continue
            self._states[server_config.server_id] = McpSessionLifecycleState.STARTING
            outcome = await discover_server(
                server_config,
                connect_timeout_seconds=self._connect_timeout_seconds,
                request_timeout_seconds=self._request_timeout_seconds,
            )
            result = outcome.result
            results.append(result)
            if outcome.client is None:
                self._states[server_config.server_id] = McpSessionLifecycleState.DEGRADED
                self._record("startup_failed", server_config.server_id, result.safe_error_code)
                continue
            self._owned_clients.add(outcome.client)
            if not self._publish_session(server_config, outcome.client, result, generation=1):
                await outcome.client.close(timeout=_DEFAULT_COMPONENT_CLOSE_TIMEOUT_SECONDS)
                results[-1] = McpServerDiscoveryResult(
                    server_id=server_config.server_id,
                    status=McpServerDiscoveryStatus.DISCOVERY_FAILED,
                    safe_error_code="MCP_TRANSPORT_CLOSED",
                )
                self._states[server_config.server_id] = McpSessionLifecycleState.DEGRADED
                continue
        self._snapshot = McpDiscoverySnapshot(results=tuple(results))
        self._frozen_results = {
            result.server_id: result
            for result in results
            if result.status is McpServerDiscoveryStatus.AVAILABLE
        }
        self._started = True
        # 连接可能在 discovery 返回与 component 标记 started 之间已经退出。
        for binding in tuple(self._sessions.values()):
            if binding.client.broken:
                self._on_client_broken(
                    binding.client,
                    binding.client.broken_error
                    or McpBoundaryError("MCP session broken"),
                )

    @property
    def discovery_snapshot(self) -> McpDiscoverySnapshot | None:
        return self._snapshot

    def session_for(self, server_id: str) -> StdioMcpClient | None:
        """既有 Adapter seam：只返回当前 AVAILABLE session。"""
        handle = self.acquire_session(server_id)
        return handle.client if handle is not None else None

    def acquire_session(self, server_id: str) -> McpSessionHandle | None:
        """获取当前 generation；不可用状态 fail closed，不隐式等待重连。"""
        binding = self._sessions.get(server_id)
        if (
            self._states.get(server_id) is not McpSessionLifecycleState.AVAILABLE
            or binding is None
            or binding.client.closed
            or binding.client.broken
        ):
            return None
        return McpSessionHandle(
            server_id=server_id,
            session_generation=binding.generation,
            client=binding.client,
            provider_identity=binding.provider_identity,
        )

    async def wait_for_available(self, server_id: str, timeout: float) -> McpSessionHandle | None:
        """仅提供 caller-bounded 等待 seam，绝不拥有 Run deadline。"""
        deadline = asyncio.get_running_loop().time() + max(float(timeout), 0.0)
        while True:
            handle = self.acquire_session(server_id)
            if handle is not None:
                return handle
            if self._closed or asyncio.get_running_loop().time() >= deadline:
                return None
            await asyncio.sleep(min(0.01, max(0.0, deadline - asyncio.get_running_loop().time())))

    def session_generation_for(self, server_id: str) -> int | None:
        binding = self._sessions.get(server_id)
        return binding.generation if binding is not None else None

    def state_for(self, server_id: str) -> McpSessionLifecycleState | None:
        return self._states.get(server_id)

    @property
    def lifecycle_states(self) -> dict[str, McpSessionLifecycleState]:
        return dict(self._states)

    @property
    def lifecycle_events(self) -> tuple[dict[str, object], ...]:
        return tuple(dict(event) for event in self._events)

    def reconnect_task_for(self, server_id: str) -> asyncio.Task | None:
        task = self._reconnect_tasks.get(server_id)
        return task if task is not None and not task.done() else None

    @property
    def started(self) -> bool:
        return self._started

    @property
    def closed(self) -> bool:
        return self._closed

    def _publish_session(
        self,
        config: McpServerConfig,
        client: StdioMcpClient,
        result: McpServerDiscoveryResult,
        *,
        generation: int,
    ) -> bool:
        # 这是 publication 的最后一道 fail-closed 检查；候选必须完成
        # initialize/list/compatibility 且仍未 broken/closed 才能成为 AVAILABLE。
        if (
            self._closed
            or result.status is not McpServerDiscoveryStatus.AVAILABLE
            or client.closed
            or client.broken
        ):
            return False
        identity = self._remote_identity(result)
        client.set_session_generation(generation)
        self._sessions[config.server_id] = _SessionBinding(client, generation, identity)
        self._states[config.server_id] = McpSessionLifecycleState.AVAILABLE
        # 先建立 binding，再安装 callback；broken callback 可能在 setter 内
        # 同步触发，必须能看到当前 candidate 并登记 pending reconnect intent。
        client.set_broken_callback(self._on_client_broken)
        if client.closed or client.broken:
            # callback setter 或 publication 窗口内失效时撤销 binding；调用方
            # 会负责 bounded close，绝不把 dead candidate 留在 AVAILABLE。
            if self._sessions.get(config.server_id) is not None:
                self._sessions.pop(config.server_id, None)
            if not self._closed:
                self._states[config.server_id] = McpSessionLifecycleState.DEGRADED
            return False
        self._record("available", config.server_id, None, generation=generation)
        return True

    @staticmethod
    def _remote_identity(result: McpServerDiscoveryResult) -> tuple[str, str]:
        # server_id 是 LocalAgent canonical configured identity；remote name
        # 用于 reconnect compatibility。remote version 仅为 observability
        # metadata，不能造成 version drift 的精确 identity mismatch。
        return (result.server_id, result.server_info_name)

    def _on_client_broken(self, client: StdioMcpClient, error: object) -> None:
        if self._closed or not self._started:
            return
        server_id = client.server_id
        binding = self._sessions.get(server_id)
        if binding is None or binding.client is not client:
            return
        self._sessions.pop(server_id, None)
        self._states[server_id] = McpSessionLifecycleState.DEGRADED
        safe_code = getattr(error, "safe_error_code", "MCP_TRANSPORT_ERROR")
        fields: dict[str, object] = {"generation": binding.generation}
        if client.exit_code is not None:
            fields["exit_code"] = client.exit_code
        self._record("degraded", server_id, safe_code, **fields)
        cleanup = asyncio.create_task(self._close_broken_session(client))
        self._background_tasks.add(cleanup)
        cleanup.add_done_callback(self._background_tasks.discard)
        self._schedule_reconnect(server_id, binding.generation)

    @staticmethod
    async def _close_broken_session(client: StdioMcpClient) -> None:
        await client.close(timeout=_DEFAULT_COMPONENT_CLOSE_TIMEOUT_SECONDS)

    def _schedule_reconnect(self, server_id: str, old_generation: int) -> None:
        if self._closed:
            # queued done-callbacks can run after shutdown has marked the
            # component CLOSED; never create a post-shutdown reconnect task.
            self._pending_reconnects.pop(server_id, None)
            return
        current = self._reconnect_tasks.get(server_id)
        if current is not None and not current.done():
            # callback 通常来自 reader/process watcher task，不能依赖
            # asyncio.current_task() 识别 reconnect owner；保留 pending intent。
            self._pending_reconnects[server_id] = max(
                old_generation, self._pending_reconnects.get(server_id, 0)
            )
            return
        task = asyncio.create_task(self._reconnect_loop(server_id, old_generation))
        self._reconnect_tasks[server_id] = task
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _reconnect_loop(self, server_id: str, old_generation: int) -> None:
        config = self._configs_by_id.get(server_id)
        frozen = self._frozen_results.get(server_id)
        if config is None or frozen is None:
            return
        try:
            for attempt in range(1, self._reconnect_max_attempts + 1):
                if self._closed:
                    return
                self._states[server_id] = McpSessionLifecycleState.RECONNECTING
                delay = min(self._reconnect_max_delay, self._reconnect_initial_delay * (2 ** (attempt - 1)))
                jitter = delay * self._reconnect_jitter_ratio * max(
                    0.0, min(1.0, float(self._jitter_source()))
                )
                await self._sleep(delay + jitter)
                if self._closed:
                    return
                self._record("reconnect_attempt", server_id, None, attempt=attempt, backoff_seconds=delay + jitter)
                outcome = await discover_server(
                    config,
                    connect_timeout_seconds=self._connect_timeout_seconds,
                    request_timeout_seconds=self._request_timeout_seconds,
                )
                if outcome.client is None:
                    self._states[server_id] = McpSessionLifecycleState.DEGRADED
                    self._record("reconnect_failed", server_id, outcome.result.safe_error_code, attempt=attempt)
                    continue
                self._owned_clients.add(outcome.client)
                compatible, reason = self._is_compatible(frozen, outcome.result)
                if not compatible:
                    await outcome.client.close(timeout=_DEFAULT_COMPONENT_CLOSE_TIMEOUT_SECONDS)
                    self._states[server_id] = McpSessionLifecycleState.DEGRADED
                    self._record("reconnect_rejected", server_id, reason)
                    return
                generation = max(old_generation, self.session_generation_for(server_id) or 0) + 1
                if not self._publish_session(config, outcome.client, outcome.result, generation=generation):
                    await outcome.client.close(timeout=_DEFAULT_COMPONENT_CLOSE_TIMEOUT_SECONDS)
                    self._states[server_id] = McpSessionLifecycleState.DEGRADED
                    self._record("reconnect_rejected", server_id, "MCP_CANDIDATE_NOT_HEALTHY")
                    continue
                self._record("reconnected", server_id, None, generation=generation)
                return
            self._states[server_id] = McpSessionLifecycleState.DEGRADED
            self._record("reconnect_exhausted", server_id, "MCP_RECONNECT_ATTEMPTS_EXHAUSTED")
        except asyncio.CancelledError:
            raise
        except Exception:
            self._states[server_id] = McpSessionLifecycleState.DEGRADED
            self._record("reconnect_failed", server_id, "MCP_RECONNECT_FAILED")
            logger.exception("MCP reconnect failed", extra={"server_id": server_id})
        finally:
            current = asyncio.current_task()
            if self._reconnect_tasks.get(server_id) is current:
                self._reconnect_tasks.pop(server_id, None)
                pending_generation = self._pending_reconnects.pop(server_id, None)
                if pending_generation is not None and not self._closed:
                    # done callback 在当前 task 完成后运行，避免短暂双 owner。
                    current.add_done_callback(
                        lambda _done: self._schedule_reconnect(
                            server_id, pending_generation
                        )
                    )

    @staticmethod
    def _is_compatible(
        frozen: McpServerDiscoveryResult,
        candidate: McpServerDiscoveryResult,
    ) -> tuple[bool, str]:
        if McpIntegrationComponent._remote_identity(frozen) != McpIntegrationComponent._remote_identity(candidate):
            return False, "MCP_PROVIDER_IDENTITY_MISMATCH"
        frozen_tools = {tool.remote_name: tool for tool in frozen.tools}
        candidate_tools = {tool.remote_name: tool for tool in candidate.tools}
        if set(frozen_tools) - set(candidate_tools):
            return False, "MCP_REQUIRED_TOOL_MISSING"
        for name, frozen_tool in frozen_tools.items():
            if frozen_tool.input_schema_digest != candidate_tools[name].input_schema_digest:
                return False, "MCP_TOOL_SCHEMA_MISMATCH"
        return True, ""

    def _record(self, event: str, server_id: str, safe_error_code: str | None, **fields: object) -> None:
        self._events.append({
            "event": event,
            "provider_identity": server_id,
            "safe_error_code": safe_error_code,
            **fields,
        })

    async def close(self, timeout: float = _DEFAULT_COMPONENT_CLOSE_TIMEOUT_SECONDS) -> bool:
        """停止 reconnect、关闭 session 并 bounded reap 所有 owned child。"""
        if self._closed:
            return True
        self._closed = True
        for task in tuple(self._reconnect_tasks.values()) + tuple(self._background_tasks):
            if not task.done():
                task.cancel()
        tasks = [task for task in self._background_tasks if not task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reconnect_tasks.clear()
        self._pending_reconnects.clear()
        self._background_tasks.clear()
        bindings = list(self._sessions.values())
        self._sessions.clear()
        for server_id in tuple(self._states):
            self._states[server_id] = McpSessionLifecycleState.CLOSED
        clients = set(self._owned_clients)
        clients.update(binding.client for binding in bindings)
        if not clients:
            self._record("shutdown", "*", None)
            return True
        budget = max(float(timeout) / len(clients), _MIN_CLIENT_CLOSE_TIMEOUT_SECONDS)
        all_closed = True
        for client in clients:
            try:
                if not await client.close(timeout=budget):
                    all_closed = False
            except asyncio.CancelledError:
                raise
            except Exception:
                all_closed = False
        self._owned_clients.clear()
        self._record("shutdown", "*", None)
        return all_closed


__all__ = [
    "McpIntegrationComponent",
    "McpSessionHandle",
    "McpSessionLifecycleState",
]
