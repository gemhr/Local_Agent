#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""项目统一配置模块。"""

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    """读取整数类型环境变量。"""
    value = os.getenv(name)
    return int(value) if value is not None else default


@dataclass(frozen=True)
class Settings:
    """不可变运行时配置对象。"""

    project_root: str
    api_host: str
    api_port: int
    api_base_url: str
    model_profile: str
    model_path: str
    model_threads: int
    model_context: int
    model_gpu_layers: int
    model_max_tokens: int
    history_window_size: int
    summary_trigger_messages: int
    summary_keep_recent: int
    summary_max_chars: int
    chroma_dir: str
    embedding_model_path: str
    memory_db_path: str
    rag_top_k: int
    rag_doc_max_chars: int
    rag_context_max_chars: int
    orchestration_enabled: bool
    orchestration_max_agents: int
    sync_enabled: bool
    wiki_cookie: str
    local_knowledge_base_dir: str

    @classmethod
    def load(cls) -> "Settings":
        """从环境变量构建配置对象。"""
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        api_host = os.getenv("LOCAL_AGENT_API_HOST", "127.0.0.1")
        api_port = _env_int("LOCAL_AGENT_API_PORT", 8000)
        api_base_url = os.getenv("LOCAL_AGENT_API_BASE_URL", f"http://{api_host}:{api_port}")

        # 本机预设专门针对“无独显、7B、本地 CPU 推理”场景。
        profile_name = os.getenv("LOCAL_AGENT_MODEL_PROFILE", "balanced").lower()
        presets = {
            "fast": {
                "model_threads": 8,
                "model_context": 2048,
                "model_max_tokens": 384,
                "history_window_size": 6,
                "summary_trigger_messages": 12,
                "summary_keep_recent": 6,
                "summary_max_chars": 700,
                "rag_top_k": 2,
                "rag_doc_max_chars": 500,
                "rag_context_max_chars": 900,
            },
            "balanced": {
                "model_threads": 10,
                "model_context": 3072,
                "model_max_tokens": 640,
                "history_window_size": 8,
                "summary_trigger_messages": 16,
                "summary_keep_recent": 8,
                "summary_max_chars": 1000,
                "rag_top_k": 3,
                "rag_doc_max_chars": 700,
                "rag_context_max_chars": 1500,
            },
            "deep": {
                "model_threads": 12,
                "model_context": 4096,
                "model_max_tokens": 896,
                "history_window_size": 10,
                "summary_trigger_messages": 20,
                "summary_keep_recent": 10,
                "summary_max_chars": 1400,
                "rag_top_k": 3,
                "rag_doc_max_chars": 900,
                "rag_context_max_chars": 2200,
            },
        }
        preset = presets.get(profile_name, presets["balanced"])

        return cls(
            project_root=project_root,
            api_host=api_host,
            api_port=api_port,
            api_base_url=api_base_url,
            model_profile=profile_name if profile_name in presets else "balanced",
            model_path=os.getenv(
                "LOCAL_AGENT_MODEL_PATH",
                os.path.join(project_root, "data", "models", "qwen2.5-7b-instruct-q4_k_m.gguf"),
            ),
            model_threads=_env_int("LOCAL_AGENT_MODEL_THREADS", preset["model_threads"]),
            model_context=_env_int("LOCAL_AGENT_MODEL_CONTEXT", preset["model_context"]),
            model_gpu_layers=_env_int("LOCAL_AGENT_MODEL_GPU_LAYERS", 0),
            model_max_tokens=_env_int("LOCAL_AGENT_MODEL_MAX_TOKENS", preset["model_max_tokens"]),
            history_window_size=_env_int("LOCAL_AGENT_HISTORY_WINDOW_SIZE", preset["history_window_size"]),
            summary_trigger_messages=_env_int(
                "LOCAL_AGENT_SUMMARY_TRIGGER_MESSAGES",
                preset["summary_trigger_messages"],
            ),
            summary_keep_recent=_env_int("LOCAL_AGENT_SUMMARY_KEEP_RECENT", preset["summary_keep_recent"]),
            summary_max_chars=_env_int("LOCAL_AGENT_SUMMARY_MAX_CHARS", preset["summary_max_chars"]),
            chroma_dir=os.getenv("LOCAL_AGENT_CHROMA_DIR", os.path.join(project_root, "chroma_db")),
            embedding_model_path=os.getenv(
                "LOCAL_AGENT_EMBEDDING_MODEL_PATH",
                os.path.join(project_root, "data", "models", "bge-large-zh-v1.5"),
            ),
            memory_db_path=os.getenv(
                "LOCAL_AGENT_MEMORY_DB_PATH",
                os.path.join(project_root, "data", "database", "agent_memory.db"),
            ),
            rag_top_k=_env_int("LOCAL_AGENT_RAG_TOP_K", preset["rag_top_k"]),
            rag_doc_max_chars=_env_int("LOCAL_AGENT_RAG_DOC_MAX_CHARS", preset["rag_doc_max_chars"]),
            rag_context_max_chars=_env_int(
                "LOCAL_AGENT_RAG_CONTEXT_MAX_CHARS",
                preset["rag_context_max_chars"],
            ),
            orchestration_enabled=os.getenv("LOCAL_AGENT_ORCHESTRATION_ENABLED", "1") == "1",
            orchestration_max_agents=_env_int("LOCAL_AGENT_ORCHESTRATION_MAX_AGENTS", 3),
            sync_enabled=os.getenv("LOCAL_AGENT_SYNC_ENABLED", "0") == "1",
            wiki_cookie=os.getenv("LOCAL_AGENT_WIKI_COOKIE", ""),
            local_knowledge_base_dir=os.getenv(
                "LOCAL_AGENT_LOCAL_KB_DIR",
                os.path.join(project_root, "data", "knowledge_base"),
            ),
        )
