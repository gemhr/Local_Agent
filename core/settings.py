#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""项目统一配置模块。"""

import os
from dataclasses import dataclass

from core.runtime.runtime_mode import ChatRuntimeMode


def _env_int(name: str, default: int) -> int:
    """读取整数类型环境变量。"""
    value = os.getenv(name)
    return int(value) if value is not None else default


def _env_int_at_least(name: str, default: int, minimum: int) -> int:
    """读取有下限约束的整数环境变量。"""
    return max(minimum, _env_int(name, default))


def _env_float_in_range(name: str, default: float, minimum: float, maximum: float) -> float:
    """读取限定范围内的浮点环境变量。"""
    value = float(os.getenv(name, str(default)))
    return min(maximum, max(minimum, value))


def _env_bool_strict(name: str, default: bool) -> bool:
    """Read an explicit boolean without silently accepting typos."""
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true"}:
        return True
    if normalized in {"0", "false"}:
        return False
    raise ValueError(f"{name} must be one of 1, 0, true, false")


@dataclass(frozen=True)
class Settings:
    """不可变运行时配置对象。"""

    project_root: str
    api_host: str
    api_port: int
    api_base_url: str
    chat_runtime_mode: ChatRuntimeMode
    model_profile: str
    llm_backend: str
    model_path: str
    remote_model_name: str
    remote_provider_kind: str
    remote_api_base_url: str
    remote_api_key: str
    remote_timeout_seconds: int
    remote_verify_tls: bool
    remote_enable_thinking: bool
    remote_context_window: int
    local_fixed_call_cost_units: int
    local_input_cost_units_per_1k_tokens: int
    local_output_cost_units_per_1k_tokens: int
    local_estimated_latency_ms: int
    remote_fixed_call_cost_units: int
    remote_input_cost_units_per_1k_tokens: int
    remote_output_cost_units_per_1k_tokens: int
    remote_estimated_latency_ms: int
    model_breaker_failure_threshold: int
    model_breaker_recovery_timeout_seconds: int
    model_breaker_half_open_max_calls: int
    model_breaker_count_rate_limited: bool
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
    embedding_query_prompt_name: str
    embedding_batch_size: int
    memory_db_path: str
    event_journal_db_path: str
    snapshot_store_enabled: bool
    snapshot_store_db_path: str
    observability_checkpoint_db_path: str
    observability_queue_capacity: int
    observability_shutdown_timeout_seconds: int
    metrics_tool_name_allowlist: tuple[str, ...]
    knowledge_collection_name: str
    rag_top_k: int
    rag_min_score: float
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
        chat_runtime_mode = ChatRuntimeMode.parse(
            os.getenv("CHAT_RUNTIME_MODE"),
            default=ChatRuntimeMode.LEGACY,
        )

        # 预设参数已从 7B 本地 CPU 推理调优为更适配 27B 远端模型：
        # - 允许更长回复（model_max_tokens）
        # - 保留更长会话记忆窗口（history_window_size）
        # - 放宽摘要与 RAG 上下文容量，减少信息截断
        # - 提高远端等待时间，适配 27B 在复杂问题下更长的推理时延
        profile_name = os.getenv("LOCAL_AGENT_MODEL_PROFILE", "balanced").lower()
        presets = {
            "fast": {
                "model_threads": 8,
                "model_context": 3072,
                "model_max_tokens": 640,
                "history_window_size": 8,
                "summary_trigger_messages": 14,
                "summary_keep_recent": 8,
                "summary_max_chars": 1000,
                "rag_top_k": 3,
                "rag_doc_max_chars": 700,
                "rag_context_max_chars": 1400,
            },
            "balanced": {
                "model_threads": 10,
                "model_context": 4096,
                "model_max_tokens": 1024,
                "history_window_size": 12,
                "summary_trigger_messages": 20,
                "summary_keep_recent": 12,
                "summary_max_chars": 1600,
                "rag_top_k": 3,
                "rag_doc_max_chars": 1000,
                "rag_context_max_chars": 2400,
            },
            "deep": {
                "model_threads": 12,
                "model_context": 6144,
                "model_max_tokens": 1536,
                "history_window_size": 16,
                "summary_trigger_messages": 24,
                "summary_keep_recent": 14,
                "summary_max_chars": 2200,
                "rag_top_k": 4,
                "rag_doc_max_chars": 1200,
                "rag_context_max_chars": 3200,
            },
        }
        preset = presets.get(profile_name, presets["balanced"])

        return cls(
            project_root=project_root,
            api_host=api_host,
            api_port=api_port,
            api_base_url=api_base_url,
            chat_runtime_mode=chat_runtime_mode,
            model_profile=profile_name if profile_name in presets else "balanced",
            llm_backend=os.getenv("LOCAL_AGENT_LLM_BACKEND", "remote").lower(),
            model_path=os.getenv(
                "LOCAL_AGENT_MODEL_PATH",
                os.path.join(project_root, "data", "models", "qwen2.5-7b-instruct-q4_k_m.gguf"),
            ),
            remote_model_name=os.getenv("LOCAL_AGENT_REMOTE_MODEL_NAME", "Qwen3.5-27B"),
            remote_provider_kind=os.getenv(
                "LOCAL_AGENT_REMOTE_PROVIDER_KIND", "openai_compatible"
            ).lower(),
            remote_api_base_url=os.getenv("LOCAL_AGENT_REMOTE_API_BASE_URL", ""),
            remote_api_key=os.getenv("LOCAL_AGENT_REMOTE_API_KEY", ""),
            remote_timeout_seconds=_env_int("LOCAL_AGENT_REMOTE_TIMEOUT_SECONDS", 120),
            remote_verify_tls=os.getenv("LOCAL_AGENT_REMOTE_VERIFY_TLS", "0") == "1",
            remote_enable_thinking=os.getenv("LOCAL_AGENT_REMOTE_ENABLE_THINKING", "0") == "1",
            remote_context_window=_env_int("LOCAL_AGENT_REMOTE_CONTEXT_WINDOW", 32768),
            local_fixed_call_cost_units=_env_int_at_least(
                "LOCAL_AGENT_LOCAL_FIXED_CALL_COST_UNITS", 1, 0
            ),
            local_input_cost_units_per_1k_tokens=_env_int_at_least(
                "LOCAL_AGENT_LOCAL_INPUT_COST_UNITS_PER_1K_TOKENS", 1, 0
            ),
            local_output_cost_units_per_1k_tokens=_env_int_at_least(
                "LOCAL_AGENT_LOCAL_OUTPUT_COST_UNITS_PER_1K_TOKENS", 1, 0
            ),
            local_estimated_latency_ms=_env_int_at_least(
                "LOCAL_AGENT_LOCAL_ESTIMATED_LATENCY_MS", 1000, 1
            ),
            remote_fixed_call_cost_units=_env_int_at_least(
                "LOCAL_AGENT_REMOTE_FIXED_CALL_COST_UNITS", 10, 0
            ),
            remote_input_cost_units_per_1k_tokens=_env_int_at_least(
                "LOCAL_AGENT_REMOTE_INPUT_COST_UNITS_PER_1K_TOKENS", 2, 0
            ),
            remote_output_cost_units_per_1k_tokens=_env_int_at_least(
                "LOCAL_AGENT_REMOTE_OUTPUT_COST_UNITS_PER_1K_TOKENS", 4, 0
            ),
            remote_estimated_latency_ms=_env_int_at_least(
                "LOCAL_AGENT_REMOTE_ESTIMATED_LATENCY_MS", 3000, 1
            ),
            model_breaker_failure_threshold=_env_int_at_least(
                "LOCAL_AGENT_MODEL_BREAKER_FAILURE_THRESHOLD", 3, 1
            ),
            model_breaker_recovery_timeout_seconds=_env_int_at_least(
                "LOCAL_AGENT_MODEL_BREAKER_RECOVERY_TIMEOUT_SECONDS", 30, 1
            ),
            model_breaker_half_open_max_calls=_env_int_at_least(
                "LOCAL_AGENT_MODEL_BREAKER_HALF_OPEN_MAX_CALLS", 1, 1
            ),
            model_breaker_count_rate_limited=os.getenv(
                "LOCAL_AGENT_MODEL_BREAKER_COUNT_RATE_LIMITED", "1"
            )
            == "1",
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
            embedding_query_prompt_name=os.getenv(
                "LOCAL_AGENT_EMBEDDING_QUERY_PROMPT_NAME",
                "",
            ),
            embedding_batch_size=_env_int_at_least(
                "LOCAL_AGENT_EMBEDDING_BATCH_SIZE",
                8,
                1,
            ),
            memory_db_path=os.getenv(
                "LOCAL_AGENT_MEMORY_DB_PATH",
                os.path.join(project_root, "data", "database", "agent_memory.db"),
            ),
            event_journal_db_path=os.getenv(
                "LOCAL_AGENT_EVENT_JOURNAL_DB_PATH",
                os.path.join(
                    project_root, "data", "database", "runtime_event_journal.db"
                ),
            ),
            snapshot_store_enabled=_env_bool_strict(
                "LOCAL_AGENT_SNAPSHOT_ENABLED", True
            ),
            snapshot_store_db_path=os.getenv(
                "LOCAL_AGENT_SNAPSHOT_DB_PATH",
                os.path.join(
                    project_root,
                    "data",
                    "database",
                    "runtime_snapshots.db",
                ),
            ),
            observability_checkpoint_db_path=os.getenv(
                "LOCAL_AGENT_OBSERVABILITY_CHECKPOINT_DB_PATH",
                os.path.join(
                    project_root,
                    "data",
                    "database",
                    "runtime_observability_checkpoint.db",
                ),
            ),
            observability_queue_capacity=_env_int_at_least(
                "LOCAL_AGENT_OBSERVABILITY_QUEUE_CAPACITY", 256, 1
            ),
            observability_shutdown_timeout_seconds=_env_int_at_least(
                "LOCAL_AGENT_OBSERVABILITY_SHUTDOWN_TIMEOUT_SECONDS", 5, 1
            ),
            metrics_tool_name_allowlist=tuple(
                value.strip()
                for value in os.getenv(
                    "LOCAL_AGENT_METRICS_TOOL_NAME_ALLOWLIST", ""
                ).split(",")
                if value.strip()
            ),
            rag_top_k=_env_int("LOCAL_AGENT_RAG_TOP_K", preset["rag_top_k"]),
            rag_min_score=_env_float_in_range("LOCAL_AGENT_RAG_MIN_SCORE", 0.55, 0.0, 1.0),
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
            knowledge_collection_name=os.getenv(
                "LOCAL_AGENT_KB_COLLECTION",
                "huawei_wiki_collection",
            ),
        )
