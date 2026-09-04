#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本地大语言模型推理封装。"""

import os
import logging
import threading
from typing import Any
from typing import Dict, Generator, List

import requests

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
    ) -> None:
        self.status_code = status_code
        self.model_failure_category = model_failure_category
        self.safe_error_code = safe_error_code
        self.provider_started = provider_started
        self.provider_responded = provider_responded
        super().__init__(safe_message)


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
    """封装 OpenAI 兼容协议的远端推理能力。"""

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
        session: requests.Session | None = None,
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
        # 默认 True 保持 requests 既有行为；生产由 Settings 提供已解析值。
        self.trust_env = trust_env
        # 统一 Invocation 本日不允许 Retry；显式覆盖 requests/urllib3 重试配置。
        self._session = session or requests.Session()
        self._session.trust_env = trust_env
        self._session_lock = threading.Lock()
        self._closed = False
        no_retry_adapter = requests.adapters.HTTPAdapter(max_retries=0)
        self._session.mount("http://", no_retry_adapter)
        self._session.mount("https://", no_retry_adapter)

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

    @staticmethod
    def _extract_content(payload: dict[str, Any]) -> str:
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            message = first.get("message")
            if isinstance(message, dict):
                content = message.get("content", "")
                if isinstance(content, str):
                    return content
            delta = first.get("delta")
            if isinstance(delta, dict):
                content = delta.get("content", "")
                if isinstance(content, str):
                    return content
        return ""

    def generate(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        enable_thinking: bool | None = None,
    ) -> Generator[str, None, None]:
        url = self._chat_completions_url()
        effective_thinking = (
            self.enable_thinking if enable_thinking is None else enable_thinking
        )
        body = {
            "model": self.model_name,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if self._supports_deepseek_thinking():
            body["thinking"] = {
                "type": "enabled" if effective_thinking else "disabled",
            }
            if effective_thinking:
                body["reasoning_effort"] = "high"
        else:
            body["chat_template_kwargs"] = {"enable_thinking": effective_thinking}
        # requests.Session 不承诺线程安全；应用级共享 Engine 必须显式串行访问。
        # close() 使用同一把锁，因此会等待当前请求返回，不主动强杀活跃调用。
        with self._session_lock:
            if self._closed:
                raise RemoteLLMError(
                    "Remote model client has been closed",
                    model_failure_category="PROVIDER_CONFIGURATION_ERROR",
                    safe_error_code="REMOTE_CLIENT_CLOSED",
                    provider_started=False,
                    provider_responded=False,
                )
            response = self._session.post(
                url,
                headers=self._build_headers(),
                json=body,
                timeout=self.timeout_seconds,
                verify=self.verify_tls,
            )
        if response.status_code >= 400:
            raise RemoteLLMError(
                f"Remote API request failed: status={response.status_code}",
                status_code=response.status_code,
                safe_error_code="REMOTE_HTTP_ERROR",
                provider_responded=True,
            )
        try:
            payload = response.json()
        except Exception:
            raise RemoteLLMError(
                "Remote API returned non-JSON payload",
                model_failure_category="OUTPUT_VALIDATION_FAILED",
                safe_error_code="REMOTE_RESPONSE_NOT_JSON",
                provider_responded=True,
            ) from None
        content = self._extract_content(payload)
        if not content.strip():
            choices = payload.get("choices") or []
            first_choice = choices[0] if choices else {}
            message = first_choice.get("message") or {}
            finish_reason = first_choice.get("finish_reason")
            reasoning_content = message.get("reasoning_content") or ""
            if finish_reason == "length":
                raise RemoteLLMError(
                    "Remote model output was truncated before producing final content: "
                    f"model={self.model_name}, max_tokens={max_tokens}, "
                    f"reasoning_chars={len(reasoning_content)}",
                    model_failure_category="CONTEXT_LIMIT_EXCEEDED",
                    safe_error_code="REMOTE_OUTPUT_TRUNCATED",
                    provider_responded=True,
                )
            raise RemoteLLMError(
                "Remote model returned empty content",
                model_failure_category="OUTPUT_VALIDATION_FAILED",
                safe_error_code="REMOTE_EMPTY_CONTENT",
                provider_responded=True,
            )
        yield content

    def generate_native(
        self,
        messages: List[Dict[str, object]],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        *,
        tools: list[dict[str, object]],
        tool_choice: str = "auto",
        enable_thinking: bool = False,
    ) -> ModelAdapterResponse:
        """执行一次 DeepSeek 非流式 native function calling 请求。

        此处只负责 provider wire 到窄内部 DTO 的正常化；参数和权限仍由
        AgentRouter 后面的 ToolAdapter/Governance 链处理。
        """
        if self.provider_kind != "deepseek":
            raise RemoteLLMError(
                "Native tool calling is only supported by DeepSeek",
                model_failure_category="PROVIDER_CONFIGURATION_ERROR",
                safe_error_code="NATIVE_TOOL_PROVIDER_UNSUPPORTED",
                provider_started=False,
                provider_responded=False,
            )
        if enable_thinking:
            raise RemoteLLMError(
                "Thinking native tool calling is not supported",
                model_failure_category="PROVIDER_CONFIGURATION_ERROR",
                safe_error_code="NATIVE_TOOL_THINKING_UNSUPPORTED",
                provider_started=False,
                provider_responded=False,
            )
        body: dict[str, object] = {
            "model": self.model_name, "messages": messages,
            "temperature": temperature, "max_tokens": max_tokens,
            "stream": False, "tools": tools, "tool_choice": tool_choice,
            "thinking": {"type": "disabled"},
        }
        with self._session_lock:
            if self._closed:
                raise RemoteLLMError("Remote model client has been closed", safe_error_code="REMOTE_CLIENT_CLOSED", provider_started=False, provider_responded=False)
            response = self._session.post(self._chat_completions_url(), headers=self._build_headers(), json=body, timeout=self.timeout_seconds, verify=self.verify_tls)
        if response.status_code >= 400:
            raise RemoteLLMError("Remote API request failed", status_code=response.status_code, safe_error_code="REMOTE_HTTP_ERROR", provider_responded=True)
        try:
            payload = response.json()
            choice = payload["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError, ValueError):
            raise RemoteLLMError("Remote API returned malformed native response", model_failure_category="OUTPUT_VALIDATION_FAILED", safe_error_code="REMOTE_NATIVE_RESPONSE_INVALID", provider_responded=True) from None
        if not isinstance(message, dict):
            raise RemoteLLMError("Remote API returned malformed native response", model_failure_category="OUTPUT_VALIDATION_FAILED", safe_error_code="REMOTE_NATIVE_RESPONSE_INVALID", provider_responded=True)
        content = message.get("content")
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise RemoteLLMError("Remote API returned malformed native content", model_failure_category="OUTPUT_VALIDATION_FAILED", safe_error_code="REMOTE_NATIVE_RESPONSE_INVALID", provider_responded=True)
        calls = message.get("tool_calls")
        if calls is None:
            if not content.strip():
                raise RemoteLLMError("Remote model returned empty content", model_failure_category="OUTPUT_VALIDATION_FAILED", safe_error_code="REMOTE_EMPTY_CONTENT", provider_responded=True)
            return ModelAdapterResponse(content)
        if not isinstance(calls, list) or len(calls) != 1:
            raise RemoteLLMError("Remote API returned unsupported tool call count", model_failure_category="OUTPUT_VALIDATION_FAILED", safe_error_code="REMOTE_NATIVE_TOOL_CALL_COUNT_INVALID", provider_responded=True)
        call = calls[0]
        function = call.get("function") if isinstance(call, dict) else None
        call_id = call.get("id") if isinstance(call, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        arguments = function.get("arguments") if isinstance(function, dict) else None
        if not all(isinstance(value, str) and value for value in (call_id, name, arguments)):
            raise RemoteLLMError("Remote API returned malformed native tool call", model_failure_category="OUTPUT_VALIDATION_FAILED", safe_error_code="REMOTE_NATIVE_TOOL_CALL_INVALID", provider_responded=True)
        native = NativeToolCall(call_id, name, arguments)
        assistant_message = {"role": "assistant", "content": content or None, "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}]}
        return ModelAdapterResponse(content, native_tool_call=native, assistant_message=assistant_message)

    def close(self) -> None:
        """等待活跃请求结束后幂等关闭共享 Session。"""
        with self._session_lock:
            if self._closed:
                return
            self._closed = True
            self._session.close()
