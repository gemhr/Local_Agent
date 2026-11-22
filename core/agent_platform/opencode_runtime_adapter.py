"""OpenCode 1.18.26 Legacy Session 的执行、取消与重读。"""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import replace
from pathlib import PurePosixPath, PureWindowsPath
import re
from secrets import token_hex
from time import time_ns
from uuid import uuid4

import httpx

from core.agent_platform.opencode_events import OpenCodeEventProjection, legacy_events
from core.agent_platform.runtime_adapter import (
    ExecutionResult,
    RuntimeAdapterError,
    RuntimeRequest,
    RuntimeUpdate,
)
from core.persistence.models import ExternalRunRow
from core.persistence.repositories.external_runs import ExternalRunConflict, ExternalRunRepository
from core.runtime.cancellation import CancellationReason, CancellationSource
from core.runtime.execution_aggregate import canonical_payload_digest
from core.runtime.run_registry import ActiveRunControlHandle, RunRegistry
from core.runtime.state import RunStatus, StopReason
from core.settings import OpenCodeInstanceSettings


_SESSION_ID = re.compile(r"ses_[A-Za-z0-9]+")
_MESSAGE_ID = re.compile(r"msg_[A-Za-z0-9]+")
_RESULT_UNKNOWN = ("OPENCODE_RESULT_UNKNOWN", "无法确认 OpenCode 任务的最终结果")
_DEADLINE = CancellationReason.REQUEST_DEADLINE_EXCEEDED


def _user_message_id() -> str:
    # OpenCode 按 ID 排序消息。每次新建 Session，使用该版本的毫秒前缀和随机后缀。
    timestamp = ((time_ns() // 1_000_000) << 12) & ((1 << 48) - 1)
    return f"msg_{timestamp:012x}{token_hex(7)}"


class OpenCodeRuntimeAdapter:
    def __init__(
        self,
        instance: OpenCodeInstanceSettings,
        *,
        runs: ExternalRunRepository,
        run_registry: RunRegistry,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._instance = instance
        self._runs = runs
        self._run_registry = run_registry
        self.target_digest = canonical_payload_digest({
            "origin": str(httpx.URL(instance.base_url)),
            "workspace": instance.workspace,
            "deployment": instance.deployment,
        })
        self._client = httpx.AsyncClient(
            base_url=instance.base_url,
            auth=httpx.BasicAuth(instance.username, instance.password),
            params={"directory": instance.workspace},
            timeout=httpx.Timeout(30, connect=5),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _post(self, path: str, payload: dict, *, prompt_timeout: float | None = None) -> dict:
        is_prompt = prompt_timeout is not None
        try:
            if is_prompt:
                response = await self._client.post(
                    path, json=payload, timeout=httpx.Timeout(prompt_timeout, connect=min(5, prompt_timeout))
                )
            else:
                response = await self._client.post(path, json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise RuntimeAdapterError("OPENCODE_CONNECTION_FAILED", "无法连接 OpenCode 实例") from None
        except httpx.RequestError:
            if is_prompt:
                raise RuntimeAdapterError(*_RESULT_UNKNOWN) from None
            raise RuntimeAdapterError("OPENCODE_CONNECTION_FAILED", "无法连接 OpenCode 实例") from None

        if response.status_code in {401, 403}:
            raise RuntimeAdapterError("OPENCODE_AUTH_FAILED", "OpenCode 实例拒绝 HTTP 认证")
        if is_prompt and response.status_code == 404:
            raise RuntimeAdapterError("OPENCODE_SESSION_NOT_FOUND", "OpenCode 会话不存在")
        if is_prompt and response.status_code in {400, 409, 422}:
            raise RuntimeAdapterError("OPENCODE_PROMPT_REJECTED", "OpenCode 拒绝 Prompt")
        incomplete = _RESULT_UNKNOWN if is_prompt else (
            "OPENCODE_SESSION_CREATE_FAILED", "无法创建或确认 OpenCode 会话"
        )
        if not response.is_success:
            raise RuntimeAdapterError(*incomplete)
        try:
            body = response.json()
        except ValueError:
            raise RuntimeAdapterError(*incomplete) from None
        if not isinstance(body, dict):
            raise RuntimeAdapterError(*incomplete)
        return body

    async def _create_session(self) -> str:
        # 使用固定标题，避免 OpenCode 额外调用模型为会话生成标题。
        body = await self._post("/session", {"title": "AgentCore Run"})
        session_id, directory = body.get("id"), body.get("directory")
        workspace = self._instance.workspace
        path_type = PureWindowsPath if PureWindowsPath(workspace).is_absolute() else PurePosixPath
        if (
            not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id)
            or not isinstance(directory, str) or path_type(directory) != path_type(workspace)
        ):
            raise RuntimeAdapterError("OPENCODE_SESSION_CREATE_FAILED", "无法创建或确认 OpenCode 会话")
        return session_id

    async def _prompt(self, request: RuntimeRequest, session_id: str, user_message_id: str, timeout: float) -> dict:
        prompt = {
            "messageID": user_message_id,
            "parts": [{"type": "text", "text": request.input}],
            "tools": {"*": False},
        }
        if self._instance.model_id is not None:
            prompt["model"] = {
                "providerID": self._instance.provider_id,
                "modelID": self._instance.model_id,
            }
        if self._instance.agent is not None:
            prompt["agent"] = self._instance.agent
        return await self._post(
            f"/session/{session_id}/message", prompt, prompt_timeout=timeout,
        )

    @staticmethod
    def _interpret(
        response: dict, result: ExecutionResult, session_id: str, user_message_id: str,
        cancel_reason: CancellationReason | str | None = None,
    ) -> ExecutionResult:
        info = response.get("info")
        if (
            not isinstance(info, dict) or info.get("role") != "assistant"
            or info.get("sessionID") != session_id or info.get("parentID") != user_message_id
        ):
            raise RuntimeAdapterError(*_RESULT_UNKNOWN)
        assistant_id = info.get("id")
        clock = info.get("time")
        if (
            not isinstance(assistant_id, str) or not _MESSAGE_ID.fullmatch(assistant_id)
            or assistant_id == user_message_id
            or not isinstance(clock, dict) or type(clock.get("completed")) is not int
            or clock["completed"] <= 0
        ):
            raise RuntimeAdapterError(*_RESULT_UNKNOWN)
        error = info.get("error")
        if error is not None and (not isinstance(error, dict) or not isinstance(error.get("name"), str)):
            raise RuntimeAdapterError(*_RESULT_UNKNOWN)
        finish = info.get("finish")
        result = replace(result, external_assistant_id=assistant_id)
        if error is not None and error["name"] == "MessageAbortedError":
            if cancel_reason is not None:
                return OpenCodeRuntimeAdapter._cancelled(result, cancel_reason)
            return replace(result, error_code="OPENCODE_TASK_FAILED", safe_message="OpenCode 报告任务中止")
        if error is not None or finish in ("length", "content-filter", "error"):
            return replace(result, error_code="OPENCODE_TASK_FAILED", safe_message="OpenCode 报告任务失败")

        parts = response.get("parts")
        if finish != "stop" or not isinstance(parts, list):
            raise RuntimeAdapterError(*_RESULT_UNKNOWN)
        text = []
        for part in parts:
            if (
                not isinstance(part, dict) or part.get("sessionID") != session_id
                or part.get("messageID") != assistant_id or part.get("type") == "tool"
            ):
                raise RuntimeAdapterError(*_RESULT_UNKNOWN)
            if part.get("type") == "text" and not (part.get("ignored") or part.get("synthetic")):
                if not isinstance(part.get("text"), str):
                    raise RuntimeAdapterError(*_RESULT_UNKNOWN)
                text.append(part["text"])
        output = "".join(text)
        if not output.strip():
            raise RuntimeAdapterError(*_RESULT_UNKNOWN)
        return replace(result, status=RunStatus.SUCCEEDED, stop_reason=StopReason.COMPLETED, output=output)

    @staticmethod
    def _cancelled(result: ExecutionResult, reason: CancellationReason | str) -> ExecutionResult:
        if reason == _DEADLINE:
            return replace(
                result, status=RunStatus.FAILED, stop_reason=StopReason.DEADLINE_EXCEEDED,
                error_code="DEADLINE_EXCEEDED", safe_message="执行超过截止时间",
            )
        stop_reason = {
            CancellationReason.CLIENT_DISCONNECTED: StopReason.CLIENT_DISCONNECTED,
            CancellationReason.SERVER_SHUTDOWN: StopReason.SYSTEM_SHUTDOWN,
        }.get(reason, StopReason.USER_CANCELLED)
        return replace(
            result, status=RunStatus.CANCELLED, stop_reason=stop_reason,
            error_code="RUN_CANCELLED", safe_message="执行已取消",
        )

    async def _message_result(
        self, result: ExecutionResult, session_id: str, user_message_id: str,
        *, assistant_id: str | None = None, cancel_reason: str | None = None,
    ) -> ExecutionResult:
        path = f"/session/{session_id}/message"
        if assistant_id is not None:
            path += f"/{assistant_id}"
        try:
            reply = await self._client.get(path, timeout=3)
            reply.raise_for_status()
            payload = reply.json()
        except (httpx.HTTPError, ValueError):
            raise RuntimeAdapterError(*_RESULT_UNKNOWN) from None
        messages = [payload] if assistant_id is not None else payload
        if not isinstance(messages, list):
            raise RuntimeAdapterError(*_RESULT_UNKNOWN)
        candidates = []
        for message in messages:
            info = message.get("info") if isinstance(message, dict) else None
            if (
                isinstance(info, dict) and info.get("role") == "assistant"
                and info.get("sessionID") == session_id and info.get("parentID") == user_message_id
                and (assistant_id is None or info.get("id") == assistant_id)
            ):
                candidates.append(message)
        if len(candidates) != 1:
            raise RuntimeAdapterError(*_RESULT_UNKNOWN)
        return self._interpret(candidates[0], result, session_id, user_message_id, cancel_reason)

    async def reread(self, row: ExternalRunRow) -> ExecutionResult | None:
        if row.terminal_status is None and self._run_registry.get(row.run_id) is not None:
            return None
        identity = self._bound_identity(row)
        return await self._message_result(
            identity, row.external_session_id, row.user_message_id,
            assistant_id=row.assistant_message_id, cancel_reason=row.cancel_reason,
        )

    def _bound_identity(self, row: ExternalRunRow) -> ExecutionResult:
        if (
            row.instance_id != self._instance.instance_id or row.target_digest != self.target_digest
            or not _SESSION_ID.fullmatch(row.external_session_id or "")
            or not _MESSAGE_ID.fullmatch(row.user_message_id or "")
            or (row.assistant_message_id is not None and not _MESSAGE_ID.fullmatch(row.assistant_message_id))
        ):
            raise RuntimeAdapterError(*_RESULT_UNKNOWN)
        return ExecutionResult(
            run_id=row.run_id, trace_id=row.trace_id, session_id=row.session_id, agent_id=row.agent_id,
            agent_version=row.agent_version, workflow_id=None, workflow_version=None,
            status=RunStatus.FAILED, stop_reason=StopReason.UNHANDLED_ERROR, error_code=None,
            safe_message="", output=None,
        )

    async def cancel_detached(self, row: ExternalRunRow) -> ExecutionResult | None:
        if row.terminal_status is not None or self._run_registry.get(row.run_id) is not None:
            return None
        identity = self._bound_identity(row)
        try:
            return await self._message_result(
                identity, row.external_session_id, row.user_message_id, cancel_reason=row.cancel_reason,
            )
        except RuntimeAdapterError:
            pass
        return await self._abort_and_confirm(
            identity, row.external_session_id, row.user_message_id,
            row.cancel_reason or CancellationReason.REQUEST_CANCELLED,
        )

    async def _listen(self, projection: OpenCodeEventProjection, queue: asyncio.Queue, ready: asyncio.Event) -> None:
        try:
            async with self._client.stream("GET", "/event", timeout=httpx.Timeout(20, connect=3)) as response:
                response.raise_for_status()
                if not response.headers.get("content-type", "").startswith("text/event-stream"):
                    return
                ready.set()
                async for event in legacy_events(response):
                    update = projection.project(event)
                    if update is not None:
                        try:
                            queue.put_nowait(update)
                        except asyncio.QueueFull:
                            # 丢中间更新时停止流式投影，保留可用前缀供最终补齐。
                            return
        except httpx.HTTPError:
            pass
        finally:
            ready.set()

    async def _abort_and_confirm(
        self, result: ExecutionResult, session_id: str, user_message_id: str, reason: CancellationReason | str,
        prompt: asyncio.Task | None = None,
    ) -> ExecutionResult:
        await self._runs.remember_cancel(result.run_id, str(reason.value if isinstance(reason, CancellationReason) else reason))
        for attempt in range(2):
            if prompt is not None and prompt.done():
                try:
                    return self._interpret(prompt.result(), result, session_id, user_message_id, reason)
                except RuntimeAdapterError:
                    pass
            if attempt == 0:
                try:
                    await self._client.post(f"/session/{session_id}/abort", timeout=3)
                except httpx.HTTPError:
                    pass
            try:
                return await self._message_result(result, session_id, user_message_id, cancel_reason=reason)
            except RuntimeAdapterError:
                if attempt == 0:
                    await asyncio.sleep(0.15)
        raise RuntimeAdapterError(*_RESULT_UNKNOWN)

    async def _perform(
        self, request: RuntimeRequest, result: ExecutionResult, source: CancellationSource,
        queue: asyncio.Queue | None, deadline: float,
    ) -> ExecutionResult:
        listener = prompt = cancellation = reread = None
        clock = asyncio.get_running_loop().time
        try:
            if source.token.reason is not None:
                return self._cancelled(result, source.token.reason)
            try:
                async with asyncio.timeout_at(deadline):
                    session_id = await self._create_session()
                    user_message_id = _user_message_id()
                    await self._runs.bind_session(result.run_id, session_id, user_message_id)
                    if queue is not None and source.token.reason is None:
                        ready = asyncio.Event()
                        projection = OpenCodeEventProjection(result.run_id, result.trace_id, session_id, user_message_id)
                        listener = asyncio.create_task(self._listen(projection, queue, ready))
                        try:
                            await asyncio.wait_for(ready.wait(), timeout=3)
                        except TimeoutError:
                            listener.cancel()
            except TimeoutError:
                source.cancel(_DEADLINE)
                return self._cancelled(result, source.token.reason)
            except RuntimeAdapterError:
                if source.token.reason is not None:
                    return self._cancelled(result, source.token.reason)
                raise
            if source.token.reason is not None:
                return self._cancelled(result, source.token.reason)
            durable_reason = await self._runs.dispatch(result.run_id)
            if durable_reason is not None:
                source.cancel(durable_reason)
            remaining = deadline - clock()
            if remaining <= 0:
                source.cancel(_DEADLINE)
            if source.token.reason is not None:
                return self._cancelled(result, source.token.reason)

            prompt = asyncio.create_task(self._prompt(request, session_id, user_message_id, remaining))
            cancellation = asyncio.create_task(source.token.wait_cancelled())
            await asyncio.wait((prompt, cancellation), timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            if prompt.done():
                try:
                    return self._interpret(prompt.result(), result, session_id, user_message_id, source.token.reason)
                except RuntimeAdapterError as exc:
                    if exc.error_code != _RESULT_UNKNOWN[0]:
                        raise
                    if source.token.reason is None and clock() < deadline:
                        reread = asyncio.create_task(self._message_result(result, session_id, user_message_id))
                        await asyncio.wait((reread, cancellation), timeout=max(0, deadline - clock()),
                                           return_when=asyncio.FIRST_COMPLETED)
                        if reread.done():
                            try:
                                return reread.result()
                            except RuntimeAdapterError:
                                if source.token.reason is None and clock() < deadline:
                                    raise
            if source.token.reason is None:
                source.cancel(_DEADLINE)
            return await self._abort_and_confirm(result, session_id, user_message_id, source.token.reason, prompt)
        finally:
            tasks = [task for task in (prompt, cancellation, listener, reread) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _result_identity(self, request: RuntimeRequest) -> ExecutionResult:
        if request.runtime_instance_id != self._instance.instance_id:
            raise RuntimeAdapterError("RUNTIME_INSTANCE_UNAVAILABLE", "Runtime 实例未启用或不可用于此 Agent")
        return ExecutionResult(
            run_id=request.run_id or str(uuid4()),
            trace_id=str(uuid4()),
            session_id=request.session_id or "default",
            agent_id=request.agent_id,
            agent_version=request.agent_version,
            workflow_id=request.workflow_id,
            workflow_version=request.workflow_version,
            status=RunStatus.FAILED,
            stop_reason=StopReason.UNHANDLED_ERROR,
            error_code=None,
            safe_message="",
            output=None,
        )

    async def _iterate(
        self, request: RuntimeRequest, *, observe: bool,
    ) -> AsyncGenerator[RuntimeUpdate | ExecutionResult, None]:
        identity = self._result_identity(request)
        if self._run_registry.get(identity.run_id) is not None:
            raise RuntimeAdapterError("RUN_ALREADY_ACCEPTED", "Run ID 已被执行占用")
        source = CancellationSource()
        deadline = asyncio.get_running_loop().time() + (request.timeout_seconds or 120)
        try:
            cancel_reason = await self._runs.claim(request, identity, self.target_digest)
        except ExternalRunConflict:
            raise RuntimeAdapterError("RUN_ALREADY_ACCEPTED", "Run ID 已被执行占用") from None
        if cancel_reason is not None:
            source.cancel(cancel_reason)
        handle = ActiveRunControlHandle(
            run_id=identity.run_id, runtime_mode="OPENCODE", cancellation_source=source, owner="application",
        )
        self._run_registry.register(handle)
        worker = next_update = None
        delivered = False
        try:
            if observe:
                yield RuntimeUpdate("started", identity.run_id, identity.trace_id)
            queue = asyncio.Queue(maxsize=64) if observe else None
            worker = asyncio.create_task(self._perform(request, identity, source, queue, deadline))
            emitted: list[str] = []
            if queue is not None:
                while not worker.done() or not queue.empty():
                    if queue.empty():
                        next_update = asyncio.create_task(queue.get())
                        await asyncio.wait((worker, next_update), return_when=asyncio.FIRST_COMPLETED)
                        if not next_update.done():
                            next_update.cancel()
                            await asyncio.gather(next_update, return_exceptions=True)
                            break
                        update = next_update.result()
                    else:
                        update = queue.get_nowait()
                    if update.kind == "output_delta":
                        emitted.append(update.text)
                    yield update
            result = await asyncio.shield(worker)
            if observe and result.output is not None:
                prefix = "".join(emitted)
                if result.output.startswith(prefix):
                    tail = result.output[len(prefix):]
                    if tail:
                        yield RuntimeUpdate("output_delta", result.run_id, result.trace_id, tail)
                else:
                    yield RuntimeUpdate("control_delta", result.run_id, result.trace_id, "output.reread")
            delivered = True
            yield result
        except RuntimeAdapterError as exc:
            if exc.error_code == _RESULT_UNKNOWN[0]:
                await self._runs.observe_unknown(identity.run_id)
            else:
                await self._runs.finish(replace(identity, error_code=exc.error_code, safe_message=exc.safe_message))
            delivered = True
            raise
        finally:
            try:
                if next_update is not None and not next_update.done():
                    next_update.cancel()
                    await asyncio.gather(next_update, return_exceptions=True)
                if not delivered:
                    source.cancel(CancellationReason.CLIENT_DISCONNECTED)
                    if worker is None:
                        await self._runs.finish(self._cancelled(identity, source.token.reason))
                    else:
                        outcome, = await asyncio.gather(worker, return_exceptions=True)
                        if isinstance(outcome, ExecutionResult):
                            await self._runs.finish(outcome)
                        else:
                            await self._runs.observe_unknown(identity.run_id)
            finally:
                handle.mark_completed()
                self._run_registry.unregister(identity.run_id)

    async def execute(self, request: RuntimeRequest) -> ExecutionResult:
        async with aclosing(self._iterate(request, observe=False)) as updates:
            return await anext(updates)

    async def stream(self, request: RuntimeRequest) -> AsyncGenerator[RuntimeUpdate | ExecutionResult, None]:
        async with aclosing(self._iterate(request, observe=True)) as updates:
            async for update in updates:
                yield update
