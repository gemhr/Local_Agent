#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""FastAPI 后端入口。"""

from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from core.agent_router import AgentRouter
from core.chat_service import ChatService
from core.llm_engine import LocalLLMEngine
from core.memory_manager import MemoryManager
from core.settings import Settings
from tools.registry import register_all_tools

try:
    from core.knowledge_base.vector_db_manager import VectorDBManager
except Exception:  # pragma: no cover
    VectorDBManager = None


settings = Settings.load()
chat_service: Optional[ChatService] = None


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
    if VectorDBManager is not None:
        try:
            db_manager = VectorDBManager(
                db_persist_dir=settings.chroma_dir,
                local_model_path=settings.embedding_model_path,
            )
        except Exception as exc:
            print(f"[Server] Vector DB disabled: {exc}")

    engine = LocalLLMEngine(
        model_path=settings.model_path,
        n_ctx=settings.model_context,
        n_threads=settings.model_threads,
        n_gpu_layers=settings.model_gpu_layers,
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
        rag_doc_max_chars=settings.rag_doc_max_chars,
        rag_context_max_chars=settings.rag_context_max_chars,
        max_tokens=settings.model_max_tokens,
        orchestration_enabled=settings.orchestration_enabled,
        orchestration_max_agents=settings.orchestration_max_agents,
    )
    register_all_tools(router)
    chat_service = ChatService(router)
    yield
    chat_service = None


app = FastAPI(title="Local Agent API", lifespan=lifespan)


class ChatRequest(BaseModel):
    """聊天流式接口的请求体。"""

    agent_id: str
    query: str
    file_path: str = ""


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
async def chat_endpoint(request: ChatRequest):
    """流式返回聊天结果。

    Args:
        request: 已校验的聊天请求对象。

    Returns:
        StreamingResponse: 纯文本增量响应流。
    """
    service = require_service()

    def generate():
        """将应用层生成器桥接为 HTTP 响应流。"""
        try:
            yield from service.stream_chat(
                agent_id=request.agent_id,
                query=request.query,
                file_path=request.file_path,
            )
        except Exception as exc:
            yield f"\n[server-error] {exc}"

    return StreamingResponse(generate(), media_type="text/event-stream")


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
    return {"messages": service.get_all_memory()}


@app.delete("/api/memory")
async def delete_memory_endpoint(request: DeleteMemoryRequest):
    """删除指定消息或清空全部记忆。"""
    service = require_service()
    service.delete_memory(message_ids=request.message_ids, delete_all=request.delete_all)
    return {"status": "success"}


if __name__ == "__main__":
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)
