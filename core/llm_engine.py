#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本地大语言模型推理封装。"""

import os
import threading
from typing import Any
from typing import Dict, Generator, List

import requests


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
            raise FileNotFoundError(f"Model file not found: {model_path}")

        from llama_cpp import Llama

        print(f"[LLM] Loading model: {os.path.basename(model_path)} ...")
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
        print("[LLM] Model loaded.")

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
    ) -> None:
        self.api_base_url = api_base_url.rstrip("/")
        self.model_name = model_name
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.verify_tls = verify_tls
        self.enable_thinking = enable_thinking
        self.provider_kind = provider_kind
        # 统一 Invocation 本日不允许 Retry；显式覆盖 requests/urllib3 重试配置。
        self._session = session or requests.Session()
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

    def close(self) -> None:
        """等待活跃请求结束后幂等关闭共享 Session。"""
        with self._session_lock:
            if self._closed:
                return
            self._closed = True
            self._session.close()
