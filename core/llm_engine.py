#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本地大语言模型推理封装。"""

import os
import threading
from typing import Any
from typing import Dict, Generator, List

import requests
from llama_cpp import Llama


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
    ) -> None:
        self.api_base_url = api_base_url.rstrip("/")
        self.model_name = model_name
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.verify_tls = verify_tls
        self.enable_thinking = enable_thinking

    def _build_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

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
    ) -> Generator[str, None, None]:
        url = f"{self.api_base_url}/v1/chat/completions"
        body = {
            "model": self.model_name,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        body["chat_template_kwargs"] = {"enable_thinking": self.enable_thinking}
        response = requests.post(
            url,
            headers=self._build_headers(),
            json=body,
            timeout=self.timeout_seconds,
            verify=self.verify_tls,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                "Remote API request failed: "
                f"status={response.status_code}, "
                f"response={response.text[:1200]}"
            )
        try:
            payload = response.json()
        except Exception as exc:
            raise RuntimeError(f"Remote API returned non-JSON payload: {response.text[:1200]}") from exc
        content = self._extract_content(payload)
        if not content:
            raise RuntimeError(f"Remote model returned empty content: payload={payload}")
        yield content
