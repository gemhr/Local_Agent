#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""FastAPI 后端入口。"""

from contextlib import asynccontextmanager
import asyncio
import logging
import uuid
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from core.agent_router import AgentRouter
from core.chat_service import ChatService
from core.llm_engine import LocalLLMEngine, RemoteLLMEngine
from core.memory_manager import MemoryManager
from core.runtime import (
    CancellationReason,
    ModelCircuitBreakerConfig,
    ModelCircuitBreakerRegistry,
    ModelCostProfile,
    ModelProfile,
    ModelProfileId,
    ModelResolver,
    RunCancelledError,
    process_run_registry,
)
from core.settings import Settings
from tools.registry import register_all_tools

try:
    from core.knowledge_base.vector_db_manager import VectorDBManager
    vector_db_import_error: Exception | None = None
except Exception as exc:  # pragma: no cover
    VectorDBManager = None
    vector_db_import_error = exc


settings = Settings.load()
chat_service: Optional[ChatService] = None
logger = logging.getLogger(__name__)
SHUTDOWN_GRACE_SECONDS = 2.0


def _next_or_none(stream):
    """避免 StopIteration 穿透 Future 边界。"""
    try:
        return next(stream)
    except StopIteration:
        return None


def _close_model_engines(engines: dict) -> tuple[str, ...]:
    """幂等关闭可关闭 Engine；单个关闭异常不阻断其余 Shutdown 清理。"""
    error_codes = []
    closed_ids = set()
    for engine in engines.values():
        identity = id(engine)
        if identity in closed_ids:
            continue
        closed_ids.add(identity)
        close = getattr(engine, "close", None)
        if close is None:
            continue
        try:
            close()
        except Exception:
            error_codes.append("MODEL_ENGINE_CLOSE_FAILED")
            logger.warning("[Model Runtime] engine close failed", exc_info=True)
    return tuple(error_codes)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """构建 FastAPI 生命周期内共享的服务对象。

    Args:
        app: FastAPI 应用实例。

    Yields:
        None: 启动阶段创建服务，关闭阶段释放引用。
    """
    global chat_service

    memory_manager = MemoryManager(db_path=settings.memory_db_path)
    db_manager = None
    knowledge_base_error = None
    if VectorDBManager is not None:
        try:
            db_manager = VectorDBManager(
                db_persist_dir=settings.chroma_dir,
                local_model_path=settings.embedding_model_path,
                collection_name=settings.knowledge_collection_name,
                embedding_batch_size=settings.embedding_batch_size,
                query_prompt_name=settings.embedding_query_prompt_name or None,
            )
            logger.info(
                "[KB Runtime] collection=%s, chroma_dir=%s, model=%s, count=%s",
                settings.knowledge_collection_name,
                settings.chroma_dir,
                settings.embedding_model_path,
                db_manager.count(),
            )
        except Exception as exc:
            knowledge_base_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "[KB Runtime] initialization failed: collection=%s, "
                "chroma_dir=%s, model=%s, error=%s",
                settings.knowledge_collection_name,
                settings.chroma_dir,
                settings.embedding_model_path,
                exc,
            )
    else:
        knowledge_base_error = (
            f"{type(vector_db_import_error).__name__}: {vector_db_import_error}"
            if vector_db_import_error is not None
            else "VectorDBManager is unavailable"
        )
        logger.warning("[KB Runtime] import failed: %s", knowledge_base_error)

    engines = {}
    profiles = []
    if settings.llm_backend in {"local", "hybrid"}:
        local_engine = LocalLLMEngine(
            model_path=settings.model_path,
            n_ctx=settings.model_context,
            n_threads=settings.model_threads,
            n_gpu_layers=settings.model_gpu_layers,
        )
        engines[ModelProfileId.LOCAL_FAST] = local_engine
        profiles.append(
            ModelProfile(
                ModelProfileId.LOCAL_FAST,
                settings.model_context,
                settings.model_max_tokens,
                False,
                False,
                False,
                False,
                1,
                1,
                ModelCostProfile(
                    ModelProfileId.LOCAL_FAST,
                    False,
                    settings.local_fixed_call_cost_units,
                    settings.local_input_cost_units_per_1k_tokens,
                    settings.local_output_cost_units_per_1k_tokens,
                    settings.local_estimated_latency_ms,
                ),
                False,
                "local_inference",
            )
        )
    if settings.llm_backend in {"remote", "hybrid"}:
        if not settings.remote_api_base_url:
            raise RuntimeError(
                "启用远程模型时必须配置 LOCAL_AGENT_REMOTE_API_BASE_URL"
            )
        remote_engine = RemoteLLMEngine(
            api_base_url=settings.remote_api_base_url,
            model_name=settings.remote_model_name,
            api_key=settings.remote_api_key,
            timeout_seconds=settings.remote_timeout_seconds,
            verify_tls=settings.remote_verify_tls,
            enable_thinking=settings.remote_enable_thinking,
            provider_kind=settings.remote_provider_kind,
        )
        engines[ModelProfileId.REMOTE_ADVANCED] = remote_engine
        profiles.append(
            ModelProfile(
                ModelProfileId.REMOTE_ADVANCED,
                settings.remote_context_window,
                settings.model_max_tokens,
                True,
                True,
                True,
                True,
                2,
                2,
                ModelCostProfile(
                    ModelProfileId.REMOTE_ADVANCED,
                    True,
                    settings.remote_fixed_call_cost_units,
                    settings.remote_input_cost_units_per_1k_tokens,
                    settings.remote_output_cost_units_per_1k_tokens,
                    settings.remote_estimated_latency_ms,
                ),
                True,
                "remote_openai_compatible",
            )
        )
    if not engines:
        raise RuntimeError("LOCAL_AGENT_LLM_BACKEND 必须是 local、remote 或 hybrid")
    engine = engines.get(ModelProfileId.LOCAL_FAST) or engines[ModelProfileId.REMOTE_ADVANCED]
    breaker_registry = ModelCircuitBreakerRegistry(
        ModelCircuitBreakerConfig(
            failure_threshold=settings.model_breaker_failure_threshold,
            recovery_timeout_seconds=settings.model_breaker_recovery_timeout_seconds,
            half_open_max_calls=settings.model_breaker_half_open_max_calls,
            count_rate_limited=settings.model_breaker_count_rate_limited,
        )
    )
    router = AgentRouter(
        llm_engine=engine,
        memory_manager=memory_manager,
        db_manager=db_manager,
        history_window_size=settings.history_window_size,
        summary_trigger_messages=settings.summary_trigger_messages,
        summary_keep_recent=settings.summary_keep_recent,
        summary_max_chars=settings.summary_max_chars,
        rag_top_k=settings.rag_top_k,
        rag_min_score=settings.rag_min_score,
        rag_doc_max_chars=settings.rag_doc_max_chars,
        rag_context_max_chars=settings.rag_context_max_chars,
        max_tokens=settings.model_max_tokens,
        model_context_window=settings.model_context,
        orchestration_enabled=settings.orchestration_enabled,
        orchestration_max_agents=settings.orchestration_max_agents,
        knowledge_base_error=knowledge_base_error,
        model_profiles=tuple(profiles),
        model_resolver=ModelResolver(engines),
        circuit_breaker_registry=breaker_registry,
    )
    register_all_tools(router)
    chat_service = ChatService(router)
    yield
    cancelled = process_run_registry.cancel_all(CancellationReason.SYSTEM_SHUTDOWN)
    remaining = await asyncio.to_thread(process_run_registry.wait_until_empty, SHUTDOWN_GRACE_SECONDS)
    if remaining:
        logger.warning("shutdown cleanup timed out for runs=%s", ",".join(remaining))
    elif cancelled:
        logger.info("shutdown cleanup completed for runs=%s", ",".join(cancelled))
    # 先取消 Run 并等待 Grace Period，再关闭共享 Remote Session。
    _close_model_engines(engines)
    chat_service = None


app = FastAPI(title="Local Agent API", lifespan=lifespan)


class ChatRequest(BaseModel):
    """聊天流式接口的请求体。"""

    agent_id: str
    query: str
    file_path: str = ""
    run_id: str | None = None


class DeleteMemoryRequest(BaseModel):
    """删除记忆接口的请求体。"""

    message_ids: list[int] = Field(default_factory=list)
    delete_all: bool = False


def require_service() -> ChatService:
    """获取已初始化的聊天服务。

    Returns:
        ChatService: 全局聊天服务实例。

    Raises:
        HTTPException: 服务尚未完成启动。
    """
    if chat_service is None:
        raise HTTPException(status_code=503, detail="Service not ready")
    return chat_service


@app.post("/api/chat")
async def chat_endpoint(payload: ChatRequest, request: Request):
    """流式返回聊天结果。

    Args:
        request: 已校验的聊天请求对象。

    Returns:
        StreamingResponse: 纯文本增量响应流。
    """
    service = require_service()

    run_id = payload.run_id or uuid.uuid4().hex
    try:
        uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid run_id") from exc

    async def generate():
        """桥接同步文本生成器；这是自定义分块 HTTP 流，不是 SSE。"""
        stream = service.stream_chat(
            agent_id=payload.agent_id, query=payload.query, file_path=payload.file_path, run_id=run_id)
        try:
            while True:
                if await request.is_disconnected():
                    process_run_registry.cancel(run_id, CancellationReason.CLIENT_DISCONNECTED)
                    return
                try:
                    chunk = await asyncio.to_thread(_next_or_none, stream)
                except StopIteration:
                    return
                if chunk is None:
                    return
                yield chunk
        except RunCancelledError:
            # 受控取消是流的正常终态；AgentLoop 已记录 CANCELLED 状态，
            # 此处只负责阻止业务异常穿透 StreamingResponse/ASGI 边界。
            return
        except asyncio.CancelledError:
            process_run_registry.cancel(run_id, CancellationReason.CLIENT_DISCONNECTED)
            raise
        except (BrokenPipeError, ConnectionResetError):
            process_run_registry.cancel(run_id, CancellationReason.CLIENT_DISCONNECTED)
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                close()

    return StreamingResponse(generate(), media_type="text/plain", headers={"X-Run-Id": run_id})


@app.post("/api/runtime/runs/{run_id}/cancel")
async def cancel_run_endpoint(run_id: str):
    """客户端只能请求用户主动取消，重复请求保持幂等。"""
    try:
        uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid run_id") from exc
    result = process_run_registry.cancel(run_id, CancellationReason.USER_CANCELLED)
    if result is None:
        return {"status": "inactive", "run_id": run_id}
    return {"status": "cancelled" if result else "already_cancelled", "run_id": run_id}


@app.get("/api/history/{agent_id}")
async def get_history_endpoint(agent_id: str, limit: int = 10, offset: int = 0):
    """按页返回某个智能体的历史消息。"""
    service = require_service()
    return {"messages": service.get_history(agent_id=agent_id, limit=limit, offset=offset)}


@app.get("/api/search")
async def search_endpoint(keyword: str):
    """根据关键词搜索持久化消息。"""
    service = require_service()
    return {"results": service.search_memory(keyword)}


@app.get("/api/memory")
async def get_all_memory_endpoint():
    """返回记忆管理弹窗使用的消息集合。"""
    service = require_service()
    return service.get_all_memory()


@app.delete("/api/memory")
async def delete_memory_endpoint(request: DeleteMemoryRequest):
    """删除指定消息或清空全部记忆。"""
    service = require_service()
    return service.delete_memory(message_ids=request.message_ids, delete_all=request.delete_all)


if __name__ == "__main__":
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)
