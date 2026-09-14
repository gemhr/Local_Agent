#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本地大语言模型推理封装。"""

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, Generator, List

import httpx

from core.runtime.model_invocation import ModelAdapterResponse, NativeToolCall


logger = logging.getLogger(__name__)


class RemoteLLMError(RuntimeError):
    """OpenAI-compatible Client 的安全错误，不保存 Provider 正文。"""

    def __init__(
        self,
        safe_message: str,
        *,
        status_code: int | None = None,
        model_failure_category: str | None = None,
        safe_error_code: str = "REMOTE_MODEL_FAILURE",
        provider_started: bool = True,
        provider_responded: bool | None = None,
        output_started: bool = False,
        deadline_exceeded: bool = False,
        cancellation_reason: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.model_failure_category = model_failure_category
        self.safe_error_code = safe_error_code
        self.provider_started = provider_started
        self.provider_responded = provider_responded
        self.output_started = output_started
        self.deadline_exceeded = deadline_exceeded
        self.cancellation_reason = cancellation_reason
        super().__init__(safe_message)


@dataclass(frozen=True, slots=True)
class TextDelta:
    """Provider-neutral text delta accepted by the Runtime boundary."""

    text: str


@dataclass(frozen=True, slots=True)
class ToolCallDelta:
    """Incremental, provider-neutral function-call delta."""

    index: int
    provider_tool_call_id: str | None = None
    tool_name: str | None = None
    arguments_fragment: str = ""


@dataclass(frozen=True, slots=True)
class UsageDelta:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class Finish:
    reason: str | None = None


ProviderDelta = TextDelta | ToolCallDelta | UsageDelta | Finish


class _SSEDecoder:
    """Parse SSE framing independently from HTTP/network chunk boundaries."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._data: list[str] = []

    def feed(self, chunk: bytes) -> Generator[str, None, None]:
        self._buffer.extend(chunk)
        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                break
            raw_line = bytes(self._buffer[:newline])
            del self._buffer[: newline + 1]
            yield from self._line(raw_line.rstrip(b"\r"))

    def finish(self) -> Generator[str, None, None]:
        if self._buffer:
            yield from self._line(bytes(self._buffer).rstrip(b"\r"))
            self._buffer.clear()
        yield from self._line(b"")

    def _line(self, raw_line: bytes) -> list[str]:
        if not raw_line:
            if not self._data:
                return []
            data = "\n".join(self._data)
            self._data.clear()
            return [data]
        if raw_line.startswith(b":"):
            return []
        field, separator, value = raw_line.partition(b":")
        if field != b"data" or not separator:
            return []
        if value.startswith(b" "):
            value = value[1:]
        self._data.append(value.decode("utf-8"))
        return []


class ScriptedEvaluationLLMEngine:
    """Layer1 专用、无网络的 target-owned deterministic model backend."""

    SCRIPT_ID = "EPISODIC_LAYER1_SCRIPT_V1"

    def generate(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        enable_thinking: bool | None = None,
    ) -> Generator[str, None, None]:
        system = "\n".join(
            message.get("content", "") for message in messages if message.get("role") == "system"
        )
        request = "\n".join(
            message.get("content", "") for message in messages if message.get("role") == "user"
        )
        if "长期记忆候选提取器" in system:
            yield '{"schema_version":1,"candidates":[]}'
        elif "遗忘目标提取器" in system:
            yield '{"schema_version":1,"logical_key":null,"source_excerpt":"","safe_reason":"EXPLICIT_FORGET"}'
        elif "无需工具时仅输出" in system:
            yield "NO_TOOL"
        elif "LocalAgent Planner" in system:
            request_lower = request.lower()
            if "安全审计" in request and "哪些" not in request:
                steps = ("audit_list", "rotation_review")
            elif "环境" in request and "状态" in request:
                steps = ("env_status",)
            else:
                rules = (
                (("发布清单", "release checklist"), ("release_list", "rollback_plan")),
                (("数据库迁移", "database migration"), ("migrate_plan",)),
                (("数据库配置", "备份", "database configuration", "backup"), ("config_check", "backup_review")),
                (("安全审计", "security audit"), ("audit_summary",)),
                (("api_key", "私钥", "权限", "access"), ("access_review",)),
                (("恢复摘要", "恢复方案", "恢复流程", "recovery summary"), ("recovery_summary",)),
                (("fixture_env_probe", "环境检查", "environment probe"), ("env_probe",)),
                (("环境状态", "环境的状态", "environment status"), ("env_status",)),
                (("部署方式", "deploy method"), ("deploy_probe",)),
                (("复制", "故障恢复", "replication"), ("replication_check",)),
                (("部署", "发布", "deploy"), ("deploy_answer",)),
                )
                steps = next(
                    (names for terms, names in rules if any(term.lower() in request_lower for term in terms)),
                    ("answer",),
                )
            tasks = ",".join(
                '{"task_id":"' + step + '","agent_id":"code_expert","instruction":"deterministic evaluation task"}'
                for step in steps
            )
            yield (
                '{"schema_version":1,"decision":"DELEGATE","tasks":['
                + tasks
                + '],"synthesis_required":true}'
            )
        else:
            yield "Layer1 deterministic completion."

    def get_token_count(self, text: str) -> int:
        return len(text.encode("utf-8"))


class LocalLLMEngine:
    """封装 llama-cpp 的模型加载与流式生成能力。"""

    def __init__(
        self,
        model_path: str,
        n_ctx: int = 4096,
        n_threads: int = 8,
        n_gpu_layers: int = 0,
    ) -> None:
        """初始化本地模型实例。

        Args:
            model_path: GGUF 模型文件路径。
            n_ctx: 上下文窗口大小。
            n_threads: CPU 推理线程数。
            n_gpu_layers: 卸载到 GPU 的层数；为 0 时表示纯 CPU。
        """
        if not os.path.exists(model_path):
            raise FileNotFoundError("Local model file is unavailable")

        from llama_cpp import Llama

        logger.info(
            "Local model initialization started",
            extra={
                "component": "llm_engine",
                "phase": "initialization",
                "status": "STARTED",
                "configured": True,
                "model_profile": "local",
            },
        )
        self.llm = Llama(
            model_path=model_path,
            n_ctx=n_ctx,
            n_threads=n_threads,
            n_gpu_layers=n_gpu_layers,
            verbose=False,
        )
        # llama.cpp 的 Python 封装不适合被多个请求并发复用，
        # 这里串行化生成流程，避免同一实例被同时推进。
        self._generate_lock = threading.Lock()
        logger.info(
            "Local model initialization completed",
            extra={
                "component": "llm_engine",
                "phase": "initialization",
                "status": "COMPLETED",
                "model_profile": "local",
            },
        )

    def generate(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        enable_thinking: bool | None = None,
    ) -> Generator[str, None, None]:
        """执行流式文本生成。

        Args:
            messages: 符合 OpenAI Chat 格式的消息列表。
            temperature: 采样温度。
            max_tokens: 最大生成长度。

        Yields:
            str: 增量文本片段。
        """
        with self._generate_lock:
            response_stream = self.llm.create_chat_completion(
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
            )

            for chunk in response_stream:
                delta = chunk["choices"][0]["delta"]
                if "content" in delta:
                    yield delta["content"]

    def get_token_count(self, text: str) -> int:
        """统计一段文本的 token 数量。

        Args:
            text: 待统计文本。

        Returns:
            int: Token 数量。
        """
        return len(self.llm.tokenize(text.encode("utf-8")))


class RemoteLLMEngine:
    """封装 OpenAI 兼容协议的远端推理能力。

    生产路径使用 application-scoped ``httpx.AsyncClient`` 和原生 SSE。
    同步方法仅是既有 blocking executor 调用方的收集 facade。
    """

    def __init__(
        self,
        api_base_url: str,
        model_name: str,
        *,
        api_key: str = "",
        timeout_seconds: int = 60,
        verify_tls: bool = False,
        enable_thinking: bool = False,
        provider_kind: str = "openai_compatible",
        client: httpx.AsyncClient | None = None,
        trust_env: bool = True,
    ) -> None:
        self.api_base_url = api_base_url.rstrip("/")
        self.model_name = model_name
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.verify_tls = verify_tls
        self.enable_thinking = enable_thinking
        self.provider_kind = provider_kind
        # trust_env 由 Settings 解析后显式注入：决定是否继承进程系统代理。
        self.trust_env = trust_env
        self._client = client
        self._client_owned = False
        if self._client is None:
            self._client = httpx.AsyncClient(
                verify=verify_tls,
                trust_env=trust_env,
                limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
            )
            self._client_owned = True
        self._session_lock = threading.Lock()
        self._closed = False

    def _build_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _chat_completions_url(self) -> str:
        """兼容传入 API 根地址、v1 地址或完整 Chat Completions 地址。"""
        if self.api_base_url.endswith("/chat/completions"):
            return self.api_base_url
        if self.api_base_url.endswith("/v1"):
            return f"{self.api_base_url}/chat/completions"
        return f"{self.api_base_url}/v1/chat/completions"

    def _supports_deepseek_thinking(self) -> bool:
        """只为显式声明的 DeepSeek Provider 发送专属参数。"""
        return self.provider_kind == "deepseek"

    def supports_native_tool_calling(self) -> bool:
        """声明当前实例可用的 provider native function calling 能力。"""
        return self.provider_kind == "deepseek"

    def supports_provider_structured_output(self) -> bool:
        """当前 Engine 未发送 response_format/json_schema，不能声明 provider guarantee。"""
        return False

    def _body(
        self,
        messages: List[Dict[str, object]],
        *,
        temperature: float,
        max_tokens: int,
        enable_thinking: bool,
        stream: bool,
        tools: list[dict[str, object]] | None = None,
        tool_choice: str = "auto",
    ) -> dict[str, object]:
        body: dict[str, object] = {
            "model": self.model_name,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        if self._supports_deepseek_thinking():
            body["thinking"] = {
                "type": "enabled" if enable_thinking else "disabled",
            }
            if enable_thinking:
                body["reasoning_effort"] = "high"
        else:
            body["chat_template_kwargs"] = {"enable_thinking": enable_thinking}
        if tools is not None:
            body["tools"] = tools
            body["tool_choice"] = tool_choice
        return body

    @staticmethod
    def _remaining_budget(
        run_context: object | None,
        invocation_deadline: float,
    ) -> float | None:
        values = [max(0.0, invocation_deadline - time.monotonic())]
        if run_context is not None:
            remaining = getattr(run_context, "remaining_seconds", lambda: None)()
            if remaining is not None:
                values.append(max(0.0, float(remaining)))
        return min(values)

    @staticmethod
    def _raise_cancel_or_deadline(
        run_context: object | None,
        cancellation_token: object | None,
    ) -> None:
        if run_context is not None:
            run_context.raise_if_inactive()
        if cancellation_token is not None:
            cancellation_token.raise_if_cancelled()

    async def _await_controlled(
        self,
        awaitable,
        *,
        run_context: object | None,
        cancellation_token: object | None,
        remaining: float | None,
        output_started: bool,
    ):
        operation_task = asyncio.create_task(awaitable)
        cancel_task = None
        deadline_task = None
        if cancellation_token is not None:
            cancel_task = asyncio.create_task(cancellation_token.wait_cancelled())
        if remaining is not None:
            deadline_task = asyncio.create_task(asyncio.sleep(remaining))
        controls = tuple(task for task in (cancel_task, deadline_task) if task is not None)
        try:
            done, _pending = await asyncio.wait(
                (operation_task, *controls), return_when=asyncio.FIRST_COMPLETED
            )
            if cancel_task is not None and cancel_task in done:
                operation_task.cancel()
                await asyncio.gather(operation_task, return_exceptions=True)
                cancellation_token.raise_if_cancelled()
            if deadline_task is not None and deadline_task in done:
                operation_task.cancel()
                await asyncio.gather(operation_task, return_exceptions=True)
                if run_context is not None:
                    run_context.raise_if_inactive()
                raise RemoteLLMError(
                    "Remote provider timeout",
                    model_failure_category="PROVIDER_TIMEOUT",
                    safe_error_code="PROVIDER_TIMEOUT",
                    output_started=output_started,
                )
            return operation_task.result()
        finally:
            for task in controls:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*controls, return_exceptions=True)

    async def _next_chunk(
        self,
        iterator,
        *,
        run_context: object | None,
        cancellation_token: object | None,
        remaining: float | None,
        output_started: bool,
    ):
        return await self._await_controlled(
            iterator.__anext__(),
            run_context=run_context,
            cancellation_token=cancellation_token,
            remaining=remaining,
            output_started=output_started,
        )

    async def agenerate(
        self,
        messages: List[Dict[str, object]],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        enable_thinking: bool | None = None,
        *,
        run_context: object | None = None,
        cancellation_token: object | None = None,
        timeout_seconds: float | None = None,
        tools: list[dict[str, object]] | None = None,
        tool_choice: str = "auto",
    ) -> AsyncIterator[ProviderDelta]:
        """Consume native SSE and yield provider-neutral typed deltas."""
        self._raise_cancel_or_deadline(run_context, cancellation_token)
        token = cancellation_token or getattr(run_context, "cancellation_token", None)
        invocation_cap = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        invocation_deadline = time.monotonic() + max(0.0, float(invocation_cap))
        remaining = self._remaining_budget(run_context, invocation_deadline)
        if remaining is not None and remaining <= 0:
            if run_context is not None:
                run_context.raise_if_inactive()
            raise RemoteLLMError(
                "Remote provider deadline exceeded",
                model_failure_category="DEADLINE_EXCEEDED",
                safe_error_code="DEADLINE_EXCEEDED",
                deadline_exceeded=True,
            )
        timeout = httpx.Timeout(
            connect=remaining,
            read=remaining,
            write=remaining,
            pool=remaining,
        )
        started = False
        output_started = False
        invocation_started = time.monotonic()
        first_delta_at: float | None = None
        finish_reason: str | None = None
        saw_finish = False
        decoder = _SSEDecoder()
        body = self._body(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            enable_thinking=self.enable_thinking
            if enable_thinking is None
            else enable_thinking,
            stream=True,
            tools=tools,
            tool_choice=tool_choice,
        )
        stream_context = None
        response_entered = False
        try:
            if self._client is None:
                raise RemoteLLMError(
                    "Remote async client is unavailable",
                    safe_error_code="REMOTE_CLIENT_UNAVAILABLE",
                    provider_started=False,
                    provider_responded=False,
                )
            stream_context = self._client.stream(
                "POST",
                self._chat_completions_url(),
                headers=self._build_headers(),
                json=body,
                timeout=timeout,
            )
            response = await self._await_controlled(
                stream_context.__aenter__(),
                run_context=run_context,
                cancellation_token=token,
                remaining=self._remaining_budget(run_context, invocation_deadline),
                output_started=False,
            )
            response_entered = True
            try:
                started = True
                if response.status_code >= 400:
                    raise RemoteLLMError(
                        f"Remote API request failed: status={response.status_code}",
                        status_code=response.status_code,
                        safe_error_code="REMOTE_HTTP_ERROR",
                        provider_responded=True,
                        output_started=output_started,
                    )
                iterator = response.aiter_bytes().__aiter__()
                while True:
                    current_remaining = self._remaining_budget(
                        run_context, invocation_deadline
                    )
                    try:
                        chunk = await self._next_chunk(
                            iterator,
                            run_context=run_context,
                            cancellation_token=token,
                            remaining=current_remaining,
                            output_started=output_started,
                        )
                    except StopAsyncIteration:
                        break
                    for data in decoder.feed(chunk):
                        for delta in self._normalize_sse_data(data):
                            self._raise_cancel_or_deadline(run_context, token)
                            if isinstance(delta, (TextDelta, ToolCallDelta)):
                                output_started = True
                                if first_delta_at is None:
                                    first_delta_at = time.monotonic()
                            if isinstance(delta, Finish):
                                if saw_finish:
                                    continue
                                finish_reason = delta.reason
                                saw_finish = True
                            yield delta
                for data in decoder.finish():
                    for delta in self._normalize_sse_data(data):
                        self._raise_cancel_or_deadline(run_context, token)
                        if isinstance(delta, (TextDelta, ToolCallDelta)):
                            output_started = True
                            if first_delta_at is None:
                                first_delta_at = time.monotonic()
                        if isinstance(delta, Finish):
                            if saw_finish:
                                continue
                            finish_reason = delta.reason
                            saw_finish = True
                        yield delta
                if not saw_finish:
                    raise RemoteLLMError(
                        "Remote provider stream ended before completion",
                        model_failure_category="TRANSIENT_PROVIDER_FAILURE",
                        safe_error_code="PROVIDER_STREAM_INCOMPLETE",
                        provider_started=started,
                        provider_responded=True,
                        output_started=output_started,
                    )
            finally:
                await stream_context.__aexit__(None, None, None)
                response_entered = False
        except RemoteLLMError as exc:
            exc.output_started = bool(exc.output_started or output_started)
            raise
        except asyncio.CancelledError:
            raise
        except UnicodeDecodeError:
            raise RemoteLLMError(
                "Remote provider returned invalid SSE encoding",
                model_failure_category="PROVIDER_PROTOCOL_ERROR",
                safe_error_code="PROVIDER_PROTOCOL_ERROR",
                provider_started=started,
                provider_responded=started,
                output_started=output_started,
            ) from None
        except httpx.TimeoutException as exc:
            raise RemoteLLMError(
                "Remote provider timeout",
                model_failure_category="PROVIDER_TIMEOUT",
                safe_error_code="PROVIDER_TIMEOUT",
                provider_started=started,
                provider_responded=started,
                output_started=output_started,
            ) from None
        except httpx.HTTPError:
            raise RemoteLLMError(
                "Remote provider connection failed",
                model_failure_category="TRANSIENT_PROVIDER_FAILURE",
                safe_error_code="PROVIDER_CONNECTION_ERROR",
                provider_started=started,
                provider_responded=started,
                output_started=output_started,
            ) from None
        finally:
            if response_entered and stream_context is not None:
                await stream_context.__aexit__(None, None, None)
            logger.info(
                "Remote provider stream completed",
                extra={
                    "component": "llm_engine",
                    "phase": "provider_stream",
                    "provider": self.provider_kind,
                    "model": self.model_name,
                    "stream": True,
                    "time_to_first_delta_ms": (
                        None
                        if first_delta_at is None
                        else max(0, int((first_delta_at - invocation_started) * 1000))
                    ),
                    "output_started": output_started,
                    "finish_reason": finish_reason,
                    "cancellation_reason": (
                        getattr(token, "reason", None)
                        if token is not None
                        else None
                    ),
                    "deadline_exceeded": bool(
                        run_context is not None
                        and getattr(run_context, "remaining_seconds", lambda: None)()
                        == 0
                    ),
                },
            )

    @staticmethod
    def _normalize_sse_data(data: str) -> tuple[ProviderDelta, ...]:
        if data.strip() == "[DONE]":
            return (Finish(),)
        try:
            payload = json.loads(data)
        except (TypeError, ValueError):
            raise RemoteLLMError(
                "Remote provider returned malformed SSE JSON",
                model_failure_category="PROVIDER_PROTOCOL_ERROR",
                safe_error_code="PROVIDER_PROTOCOL_ERROR",
                provider_responded=True,
            ) from None
        if not isinstance(payload, dict):
            raise RemoteLLMError(
                "Remote provider returned malformed SSE payload",
                model_failure_category="PROVIDER_PROTOCOL_ERROR",
                safe_error_code="PROVIDER_PROTOCOL_ERROR",
                provider_responded=True,
            )
        deltas: list[ProviderDelta] = []
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            choice = choices[0]
            if isinstance(choice, dict):
                delta = choice.get("delta")
                if isinstance(delta, dict):
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        deltas.append(TextDelta(content))
                    calls = delta.get("tool_calls")
                    if isinstance(calls, list):
                        for call in calls:
                            if not isinstance(call, dict):
                                raise RemoteLLMError(
                                    "Remote provider returned malformed tool-call delta",
                                    model_failure_category="PROVIDER_PROTOCOL_ERROR",
                                    safe_error_code="PROVIDER_PROTOCOL_ERROR",
                                    provider_responded=True,
                                )
                            function = call.get("function")
                            if function is not None and not isinstance(function, dict):
                                raise RemoteLLMError(
                                    "Remote provider returned malformed tool-call delta",
                                    model_failure_category="PROVIDER_PROTOCOL_ERROR",
                                    safe_error_code="PROVIDER_PROTOCOL_ERROR",
                                    provider_responded=True,
                                )
                            function = function or {}
                            index = call.get("index")
                            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                                raise RemoteLLMError(
                                    "Remote provider returned invalid tool-call index",
                                    model_failure_category="PROVIDER_PROTOCOL_ERROR",
                                    safe_error_code="PROVIDER_PROTOCOL_ERROR",
                                    provider_responded=True,
                                )
                            call_id = call.get("id")
                            name = function.get("name")
                            arguments = function.get("arguments")
                            if (
                                (call_id is not None and not isinstance(call_id, str))
                                or (name is not None and not isinstance(name, str))
                                or (arguments is not None and not isinstance(arguments, str))
                            ):
                                raise RemoteLLMError(
                                    "Remote provider returned malformed tool-call delta",
                                    model_failure_category="PROVIDER_PROTOCOL_ERROR",
                                    safe_error_code="PROVIDER_PROTOCOL_ERROR",
                                    provider_responded=True,
                                )
                            deltas.append(
                                ToolCallDelta(
                                    index,
                                    call_id,
                                    name,
                                    arguments or "",
                                )
                            )
                reason = choice.get("finish_reason")
                if reason is not None:
                    deltas.append(Finish(str(reason)))
        usage = payload.get("usage")
        if isinstance(usage, dict):
            prompt = usage.get("prompt_tokens", 0)
            completion = usage.get("completion_tokens", 0)
            deltas.append(
                UsageDelta(
                    int(prompt) if isinstance(prompt, int) else 0,
                    int(completion) if isinstance(completion, int) else 0,
                )
            )
        return tuple(deltas)

    def generate(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        enable_thinking: bool | None = None,
        *,
        run_context: object | None = None,
        cancellation_token: object | None = None,
        timeout_seconds: float | None = None,
    ) -> Generator[str, None, None]:
        with self._session_lock:
            if self._closed:
                raise RemoteLLMError(
                    "Remote model client has been closed",
                    safe_error_code="REMOTE_CLIENT_CLOSED",
                    provider_started=False,
                    provider_responded=False,
                )
            deltas = self._run_async(
                self.agenerate(
                    [dict(message) for message in messages],
                    temperature,
                    max_tokens,
                    enable_thinking,
                    run_context=run_context,
                    cancellation_token=cancellation_token,
                    timeout_seconds=timeout_seconds,
                )
            )
        for delta in deltas:
            if isinstance(delta, TextDelta):
                yield delta.text

    def _run_async(self, iterator):
        async def collect() -> list[ProviderDelta]:
            return [delta async for delta in iterator]

        return asyncio.run(collect())

    def generate_native(
        self,
        messages: List[Dict[str, object]],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        *,
        tools: list[dict[str, object]],
        tool_choice: str = "auto",
        enable_thinking: bool = False,
        run_context: object | None = None,
        cancellation_token: object | None = None,
    ) -> ModelAdapterResponse:
        """执行一次 DeepSeek 非流式 native function calling 请求。

        此处只负责 provider wire 到窄内部 DTO 的正常化；参数和权限仍由
        AgentRouter 后面的 ToolAdapter/Governance 链处理。
        """
        with self._session_lock:
            if self._closed:
                raise RemoteLLMError(
                    "Remote model client has been closed",
                    safe_error_code="REMOTE_CLIENT_CLOSED",
                    provider_started=False,
                    provider_responded=False,
                )
            return asyncio.run(
                self.agenerate_native(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    tools=tools,
                    tool_choice=tool_choice,
                    enable_thinking=enable_thinking,
                    run_context=run_context,
                    cancellation_token=cancellation_token,
                )
            )

    async def agenerate_native(
        self,
        messages: List[Dict[str, object]],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        *,
        tools: list[dict[str, object]],
        tool_choice: str = "auto",
        enable_thinking: bool = False,
        run_context: object | None = None,
        cancellation_token: object | None = None,
    ) -> ModelAdapterResponse:
        chunks: list[str] = []
        call_parts: dict[int, dict[str, str]] = {}
        actual_usage = None
        async for delta in self.agenerate(
            messages,
            temperature,
            max_tokens,
            enable_thinking,
            run_context=run_context,
            cancellation_token=cancellation_token,
            tools=tools,
            tool_choice=tool_choice,
        ):
            if isinstance(delta, TextDelta):
                chunks.append(delta.text)
            elif isinstance(delta, ToolCallDelta):
                item = call_parts.setdefault(delta.index, {"id": "", "name": "", "arguments": ""})
                if delta.provider_tool_call_id:
                    if item["id"] and item["id"] != delta.provider_tool_call_id:
                        raise RemoteLLMError("Remote API returned conflicting native tool call id", model_failure_category="OUTPUT_VALIDATION_FAILED", safe_error_code="REMOTE_NATIVE_TOOL_CALL_INVALID", provider_responded=True, output_started=True)
                    item["id"] = delta.provider_tool_call_id
                if delta.tool_name:
                    if item["name"] and item["name"] != delta.tool_name:
                        raise RemoteLLMError("Remote API returned conflicting native tool name", model_failure_category="OUTPUT_VALIDATION_FAILED", safe_error_code="REMOTE_NATIVE_TOOL_CALL_INVALID", provider_responded=True, output_started=True)
                    item["name"] = delta.tool_name
                item["arguments"] += delta.arguments_fragment
            elif isinstance(delta, UsageDelta):
                from core.runtime.budget import BudgetUsage
                actual_usage = BudgetUsage(input_tokens=delta.input_tokens, output_tokens=delta.output_tokens, total_tokens=delta.input_tokens + delta.output_tokens)
        if not call_parts:
            content = "".join(chunks)
            if not content.strip():
                raise RemoteLLMError("Remote model returned empty content", model_failure_category="OUTPUT_VALIDATION_FAILED", safe_error_code="REMOTE_EMPTY_CONTENT", provider_responded=True)
            return ModelAdapterResponse(content, actual_usage=actual_usage)
        if len(call_parts) != 1:
            raise RemoteLLMError("Remote API returned unsupported tool call count", model_failure_category="OUTPUT_VALIDATION_FAILED", safe_error_code="REMOTE_NATIVE_TOOL_CALL_COUNT_INVALID", provider_responded=True, output_started=True)
        item = next(iter(call_parts.values()))
        try:
            arguments = json.loads(item["arguments"])
        except (TypeError, ValueError):
            raise RemoteLLMError("Remote API returned incomplete native tool arguments", model_failure_category="OUTPUT_VALIDATION_FAILED", safe_error_code="REMOTE_NATIVE_TOOL_CALL_INVALID", provider_responded=True, output_started=True) from None
        if not isinstance(arguments, dict) or not item["id"] or not item["name"]:
            raise RemoteLLMError("Remote API returned malformed native tool call", model_failure_category="OUTPUT_VALIDATION_FAILED", safe_error_code="REMOTE_NATIVE_TOOL_CALL_INVALID", provider_responded=True, output_started=True)
        arguments_json = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        native = NativeToolCall(item["id"], item["name"], arguments_json)
        assistant_message = {"role": "assistant", "content": "".join(chunks) or None, "tool_calls": [{"id": item["id"], "type": "function", "function": {"name": item["name"], "arguments": arguments_json}}]}
        return ModelAdapterResponse("".join(chunks), actual_usage=actual_usage, native_tool_call=native, assistant_message=assistant_message)

    def close(self) -> object | None:
        """幂等关闭 application-scoped client。

        Application shutdown 调用此同步 facade 时，``_invoke_bounded`` 会在
        worker thread 中执行它；若外部直接在 async 代码中调用，则返回可等待
        coroutine，保持同一 client lifecycle owner。
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.aclose())
        return self.aclose()

    async def aclose(self) -> None:
        """Async application-lifecycle close for the shared HTTP client."""
        with self._session_lock:
            if self._closed:
                return
            self._closed = True
        if self._client is not None and self._client_owned:
            await self._client.aclose()
