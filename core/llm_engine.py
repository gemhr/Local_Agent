#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本地大语言模型推理封装。"""

import os
import threading
from typing import Dict, Generator, List

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
