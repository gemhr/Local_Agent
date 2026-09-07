#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""MCP stdio client（WP1：initialize / capability negotiation / tools/list / close）。

传输边界（``MCP_TRANSPORT = STDIO_FIRST``，WP0 Decision §8）：

- 启动 configured 本地 server 子进程，仅在 stdin/stdout 上收发
  newline-delimited JSON-RPC；stderr 是非权威诊断输出，只 drain 丢弃。
- 子进程环境 = 固定 code-level inherit allowlist + 配置 environment；
  不继承宿主完整环境，避免把宿主 secret 无边界泄露给 MCP server 进程。
- 底层只有 subprocess lifecycle 与 bounded IO timeout（connect/request），
  不拥有 Runtime timeout/cancellation authority；WP2 的 MCP-backed
  ToolAdapter 必须在 Runtime deadline/cancellation 内包裹本 client。
- 不实现 Streamable HTTP / SSE / 多 transport 框架，不做重连或 fallback。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

from mcp.config import McpServerConfig
from mcp.errors import (
    McpBoundaryError,
    McpCapabilityError,
    McpDiscoveryError,
    McpProtocolError,
    McpProtocolVersionError,
    McpServerUnavailableError,
    McpTransportClosedError,
    McpTransportTimeoutError,
)
from mcp.models import (
    MCP_PROTOCOL_VERSION,
    MAX_TOOLS_LIST_PAGES,
    MAX_TOOLS_PER_SERVER,
    SERVER_INFO_TEXT_MAX_CHARS,
    McpInitializeInfo,
    McpToolCallResult,
    McpToolDescriptor,
)

CLIENT_NAME = "localagent"
CLIENT_VERSION = "1.0.0"

_STDIO_LINE_LIMIT_BYTES = 1_048_576
_DEFAULT_CLOSE_TIMEOUT_SECONDS = 3.0

# Windows 本地子进程所需的最小宿主环境 allowlist；固定于代码，不配置。
_STDIO_INHERITED_ENV_KEYS = (
    "COMSPEC",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "WINDIR",
)


def _consume_exception(future: "asyncio.Future") -> None:
    """消费未被 await 的 future 异常，避免 orphan warning。"""
    if not future.cancelled():
        future.exception()


class StdioMcpClient:
    """单个 configured stdio MCP server 的 application-scope client/session。

    生命周期：``initialize()`` -> ``list_tools()`` -> ``call_tool()``* ->
    ``close()``。``call_tool`` 由 WP2 的 MCP-backed ToolAdapter 在既有
    Tool Runtime 执行路径内调用；本 client 不拥有 timeout/cancellation
    authority，也不做重试。
    """

    def __init__(
        self,
        *,
        server_config: McpServerConfig,
        connect_timeout_seconds: float,
        request_timeout_seconds: float,
        client_name: str = CLIENT_NAME,
        client_version: str = CLIENT_VERSION,
    ) -> None:
        self._server_config = server_config
        self._connect_timeout_seconds = float(connect_timeout_seconds)
        self._request_timeout_seconds = float(request_timeout_seconds)
        self._client_name = client_name
        self._client_version = client_version
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._background_tasks: set[asyncio.Task] = set()
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._broken = False
        self._closed = False
        self._initialized = False
        self._initialize_info: McpInitializeInfo | None = None

    # ---- lifecycle ----

    async def initialize(self) -> McpInitializeInfo:
        """启动子进程并完成 initialize handshake + capability negotiation。"""
        if self._initialized:
            raise McpProtocolError("already_initialized")
        if self._closed:
            raise McpTransportClosedError("client_closed")
        await self._spawn()
        request_id = self._next_request_id()
        future = self._register_pending(request_id)
        try:
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {
                            "name": self._client_name,
                            "version": self._client_version,
                        },
                    },
                }
            )
            response = await asyncio.wait_for(
                future, timeout=self._connect_timeout_seconds
            )
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            error = McpTransportTimeoutError("initialize_timeout")
            self._break(error)
            raise error from None
        except asyncio.CancelledError:
            self._pending.pop(request_id, None)
            raise
        result = self._validate_response(response, request_id)
        self._validate_initialize_result(result)
        # initialized notification：无需响应；不等待、不重试。
        await self._send(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        self._initialized = True
        assert self._initialize_info is not None
        return self._initialize_info

    async def list_tools(self) -> tuple[McpToolDescriptor, ...]:
        """分页拉取 tools/list 并产出 bounded discovery 快照。

        任一 entry 校验失败（invalid/duplicate/oversize/超限）视为该 server
        的协议违规并整体 fail closed（WP1 显式策略：不部分采纳）。
        """
        if not self._initialized:
            raise McpProtocolError("not_initialized")
        tools: list[McpToolDescriptor] = []
        seen_names: set[str] = set()
        cursor: str | None = None
        for _page in range(MAX_TOOLS_LIST_PAGES):
            params = {} if cursor is None else {"cursor": cursor}
            result = await self._request(
                "tools/list", params, self._request_timeout_seconds
            )
            page_tools = result.get("tools")
            if not isinstance(page_tools, list):
                raise McpDiscoveryError("tools_field_invalid")
            for payload in page_tools:
                descriptor = McpToolDescriptor.from_protocol_payload(
                    self._server_config.server_id, payload
                )
                if descriptor.remote_name in seen_names:
                    raise McpDiscoveryError("duplicate_tool_name")
                seen_names.add(descriptor.remote_name)
                tools.append(descriptor)
                if len(tools) > MAX_TOOLS_PER_SERVER:
                    raise McpDiscoveryError("tool_count_exceeds_limit")
            next_cursor = result.get("nextCursor")
            if next_cursor is None:
                return tuple(tools)
            if not isinstance(next_cursor, str) or not next_cursor:
                raise McpDiscoveryError("next_cursor_invalid")
            cursor = next_cursor
        raise McpDiscoveryError("page_limit_exceeded")

    async def call_tool(
        self,
        remote_name: str,
        arguments: dict,
        timeout: float,
    ) -> McpToolCallResult:
        """执行一次 ``tools/call`` 并返回 bounded 归一化结果（WP2）。

        - ``timeout`` 由调用方（MCP-backed ToolAdapter）按 Runtime effective
          deadline 计算；本方法只是 lower-level bounded IO 机制，不拥有
          Runtime timeout/cancellation authority。
        - Runtime 取消（task cancel）传播到本方法时：移除 pending 等待、
          best-effort 发送 ``notifications/cancelled``、再重新抛出
          ``CancelledError``。迟到的 response 因 pending 已移除而被
          ``_handle_line`` 忽略，不会产生第二次完成。
        - 不做重试/重连；请求级超时按 WP1 语义视为 transport 失败
          （连接不可用），由 adapter 映射为 typed tool failure。
        """
        if not self._initialized:
            raise McpProtocolError("not_initialized")
        if self._broken:
            raise McpTransportClosedError("connection_broken")
        if self._closed:
            raise McpTransportClosedError("client_closed")
        if not isinstance(remote_name, str) or not remote_name:
            raise McpProtocolError("remote_name_invalid")
        if not isinstance(arguments, dict):
            raise McpProtocolError("arguments_invalid")
        bounded_timeout = max(float(timeout), 0.001)
        request_id = self._next_request_id()
        future = self._register_pending(request_id)
        try:
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {"name": remote_name, "arguments": arguments},
                }
            )
            response = await asyncio.wait_for(future, timeout=bounded_timeout)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            error = McpTransportTimeoutError("request_timeout")
            self._break(error)
            raise error from None
        except asyncio.CancelledError:
            self._pending.pop(request_id, None)
            await self._send_cancelled_notification(request_id)
            raise
        return McpToolCallResult.from_protocol_payload(
            self._validate_response(response, request_id)
        )

    async def close(self, timeout: float = _DEFAULT_CLOSE_TIMEOUT_SECONDS) -> bool:
        """graceful close：关 stdin -> bounded 等待退出 -> terminate -> kill。

        幂等；不抛出协议/传输错误，返回是否已确认进程退出，
        供 ``RuntimeInitializationStack`` / ``ApplicationRuntimeServices``
        的 bounded close 直接调用。
        """
        bounded_timeout = max(float(timeout), 0.05)
        phase_timeout = bounded_timeout / 3
        if self._closed:
            return True
        self._closed = True
        self._fail_pending(McpTransportClosedError("client_closed"))
        process = self._process
        if process is None:
            await self._cancel_tasks()
            return True
        process_exited = False
        try:
            if process.stdin is not None and not process.stdin.is_closing():
                process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=phase_timeout)
            process_exited = True
        except asyncio.TimeoutError:
            try:
                process.terminate()
            except (OSError, ValueError):
                pass
            try:
                await asyncio.wait_for(
                    process.wait(), timeout=phase_timeout
                )
                process_exited = True
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except (OSError, ValueError):
                    pass
                try:
                    await asyncio.wait_for(
                        process.wait(), timeout=phase_timeout
                    )
                    process_exited = True
                except asyncio.TimeoutError:
                    pass
        except (OSError, ValueError):
            pass
        await self._cancel_tasks()
        return process_exited

    # ---- read-only facts ----

    @property
    def server_id(self) -> str:
        return self._server_config.server_id

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def broken(self) -> bool:
        return self._broken

    @property
    def exit_code(self) -> int | None:
        if self._process is None:
            return None
        return self._process.returncode

    @property
    def initialize_info(self) -> McpInitializeInfo | None:
        return self._initialize_info

    # ---- subprocess / transport internals ----

    async def _spawn(self) -> None:
        child_env = {
            key: os.environ[key]
            for key in _STDIO_INHERITED_ENV_KEYS
            if key in os.environ
        }
        child_env.update(self._server_config.environment_dict())
        creationflags = (
            subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        )
        try:
            self._process = await asyncio.create_subprocess_exec(
                self._server_config.command,
                *self._server_config.arguments,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=child_env,
                limit=_STDIO_LINE_LIMIT_BYTES,
                creationflags=creationflags,
            )
        except (OSError, ValueError):
            raise McpServerUnavailableError("spawn_failed") from None
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _read_loop(self) -> None:
        process = self._process
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                self._handle_line(line)
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError):
            self._break(McpTransportClosedError("stdout_read_failed"))
        finally:
            self._fail_pending(McpTransportClosedError("stdout_closed"))

    async def _drain_stderr(self) -> None:
        try:
            while True:
                chunk = await self._process.stderr.read(4096)
                if not chunk:
                    return
                # stderr 是非权威诊断输出：只 drain 防止管道写满阻塞，不解析。
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError):
            return

    def _handle_line(self, raw: bytes) -> None:
        if self._broken:
            return
        try:
            message = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            self._break(McpProtocolError("malformed_json_line"))
            return
        if not isinstance(message, dict):
            self._break(McpProtocolError("message_not_object"))
            return
        if "method" in message:
            request_id = message.get("id")
            if request_id is not None:
                # server->client request：WP1 不支持任何 server request，
                # 按协议返回 method-not-found，避免 server 无限等待。
                self._respond_method_not_found(request_id)
            return
        request_id = message.get("id")
        if not isinstance(request_id, int):
            return
        future = self._pending.pop(request_id, None)
        if future is None or future.done():
            # 未知 id / 迟到响应：忽略，不复活已完成的等待。
            return
        future.set_result(message)

    def _respond_method_not_found(self, request_id: object) -> None:
        async def _respond() -> None:
            try:
                await self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {
                            "code": -32601,
                            "message": "method not found",
                        },
                    }
                )
            except McpBoundaryError:
                return

        task = asyncio.create_task(_respond())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _request(
        self, method: str, params: dict, timeout: float
    ) -> dict:
        if self._broken:
            raise McpTransportClosedError("connection_broken")
        request_id = self._next_request_id()
        future = self._register_pending(request_id)
        try:
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )
            response = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            # WP1：请求级超时按 transport 失败处理，连接视为不可用；
            # 不重试、不重连（retry safety 属本地 ToolExecutionSpec，WP2）。
            error = McpTransportTimeoutError("request_timeout")
            self._break(error)
            raise error from None
        except asyncio.CancelledError:
            self._pending.pop(request_id, None)
            raise
        return self._validate_response(response, request_id)

    async def _send_cancelled_notification(self, request_id: int) -> None:
        """best-effort 发送 ``notifications/cancelled``；失败只静默放弃。

        底层 mechanism：Authority 仍是 RunContext / RunCoordinator /
        ToolExecutionService；发送失败不影响 Runtime cancellation 结果。
        """
        try:
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": request_id},
                }
            )
        except (McpBoundaryError, asyncio.CancelledError):
            return

    async def _send(self, message: dict) -> None:
        if self._broken or self._closed:
            raise McpTransportClosedError("connection_not_open")
        process = self._process
        if process is None or process.stdin is None:
            raise McpTransportClosedError("connection_not_open")
        data = json.dumps(
            message, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if b"\n" in data:
            raise McpProtocolError("embedded_newline")
        try:
            process.stdin.write(data + b"\n")
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            self._broken = True
            raise McpTransportClosedError("stdin_write_failed") from None

    def _next_request_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _register_pending(self, request_id: int) -> asyncio.Future:
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(_consume_exception)
        self._pending[request_id] = future
        return future

    def _validate_response(
        self, response: object, request_id: int
    ) -> dict:
        if not isinstance(response, dict):
            raise McpProtocolError("response_not_object")
        if response.get("jsonrpc") != "2.0":
            raise McpProtocolError("jsonrpc_version_invalid")
        if response.get("id") != request_id:
            raise McpProtocolError("response_id_mismatch")
        if "error" in response:
            # 只保留 content-free 事实；server error body 不进入异常文本。
            raise McpProtocolError("error_response")
        result = response.get("result")
        if not isinstance(result, dict):
            raise McpProtocolError("result_not_object")
        return result

    def _validate_initialize_result(self, result: dict) -> None:
        version = result.get("protocolVersion")
        if not isinstance(version, str) or version != MCP_PROTOCOL_VERSION:
            raise McpProtocolVersionError("unsupported_protocol_version")
        capabilities = result.get("capabilities")
        if not isinstance(capabilities, dict) or not isinstance(
            capabilities.get("tools"), dict
        ):
            raise McpCapabilityError("tools_capability_missing")
        server_info = result.get("serverInfo")
        if not isinstance(server_info, dict):
            raise McpProtocolError("server_info_missing")
        # initialize 之后再不发送协议请求时不会用到；这里只保留 bounded
        # provider metadata 供 discovery snapshot 记录 provenance。
        self._initialize_info = McpInitializeInfo(
            protocol_version=version,
            server_name=self._bounded_server_info(server_info.get("name")),
            server_version=self._bounded_server_info(
                server_info.get("version")
            ),
        )

    @staticmethod
    def _bounded_server_info(value: object) -> str:
        if not isinstance(value, str):
            return ""
        return value.replace("\x00", "")[:SERVER_INFO_TEXT_MAX_CHARS]

    def _break(self, error: McpBoundaryError) -> None:
        self._broken = True
        self._fail_pending(error)

    def _fail_pending(self, error: McpBoundaryError) -> None:
        while self._pending:
            _request_id, future = self._pending.popitem()
            if not future.done():
                future.set_exception(error)

    async def _cancel_tasks(self) -> None:
        for task in (
            self._reader_task,
            self._stderr_task,
            *self._background_tasks,
        ):
            if task is not None and not task.done():
                task.cancel()
        pending = [
            task
            for task in (
                self._reader_task,
                self._stderr_task,
                *self._background_tasks,
            )
            if task is not None
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._reader_task = None
        self._stderr_task = None
        self._background_tasks.clear()


__all__ = [
    "CLIENT_NAME",
    "CLIENT_VERSION",
    "StdioMcpClient",
]
