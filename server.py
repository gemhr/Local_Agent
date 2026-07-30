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
    ApplicationRuntimeServices,
    ChatRuntimeMode,
    ChatRuntimeSelector,
    CancellationReason,
    CoordinatedRuntimeFactory,
    GracefulShutdownCoordinator,
    InMemorySpanRecorder,
    ModelCircuitBreakerConfig,
    ModelCircuitBreakerRegistry,
    ModelCostProfile,
    ModelProfile,
    ModelProfileId,
    ModelResolver,
    RecoveryValidator,
    RuntimeInitializationStack,
    RunRegistry,
    RunCancelledError,
    SQLiteEventConsumptionCheckpointStore,
    SQLiteRunEventJournal,
    SQLiteSnapshotStore,
    RuntimeLifecycleState,
    RuntimeAdmissionRejectedError,
    BlockingExecutorAdmissionTimeout,
    BlockingExecutorClosedError,
    BlockingTaskKind,
    process_legacy_step_executor,
    process_run_registry,
)
from core.runtime.blocking_executor import BoundedBlockingExecutor
from core.runtime.metrics import (
    ApplicationRuntimeGaugeProvider,
    InMemoryMetricsRecorder,
    MetricLabelPolicy,
    RecorderInfrastructureMetricsHook,
    RuntimeMetricsCollector,
    RuntimeMetricsProjector,
)
from core.runtime.observability_dispatcher import RuntimeObservabilityDispatcher
from core.runtime.structured_logging import (
    JsonStructuredRuntimeLogger,
    StructuredLogProjector,
)
from core.settings import Settings
from tools.registry import register_all_tools


class _RequestOwnedStreamingResponse(StreamingResponse):
    """Always close the request-owned body iterator after transport exit."""

    async def stream_response(self, send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": self.raw_headers,
            }
        )
        try:
            async for chunk in self.body_iterator:
                if not isinstance(chunk, (bytes, memoryview)):
                    chunk = chunk.encode(self.charset)
                await send(
                    {
                        "type": "http.response.body",
                        "body": chunk,
                        "more_body": True,
                    }
                )
        finally:
            close = getattr(self.body_iterator, "aclose", None)
            if callable(close):
                await close()
        await send(
            {
                "type": "http.response.body",
                "body": b"",
                "more_body": False,
            }
        )


try:
    from core.knowledge_base.vector_db_manager import VectorDBManager
    vector_db_import_error: Exception | None = None
except Exception as exc:  # pragma: no cover
    VectorDBManager = None
    vector_db_import_error = exc


settings = Settings.load()
chat_service: Optional[ChatService] = None
application_runtime_services: Optional[ApplicationRuntimeServices] = None
logger = logging.getLogger(__name__)
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
            logger.warning(
                "Model runtime close failed safely",
                extra={
                    "safe_error_code": "MODEL_ENGINE_CLOSE_FAILED",
                    "component": "model_runtime",
                    "phase": "shutdown",
                    "status": "FAILED",
                },
            )
    return tuple(error_codes)


async def _watch_request_disconnect(
    request: Request,
    *,
    run_registry,
    run_id: str,
    disconnected: asyncio.Event,
    stopped: asyncio.Event,
) -> None:
    """The sole logical disconnect owner for one HTTP request."""
    while not stopped.is_set():
        try:
            if await request.is_disconnected():
                disconnected.set()
                run_registry.cancel(
                    run_id, CancellationReason.CLIENT_DISCONNECTED
                )
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            # An uncertain watcher must not cancel an otherwise healthy run.
            return
        try:
            await asyncio.wait_for(stopped.wait(), timeout=0.05)
        except TimeoutError:
            pass


async def _stop_disconnect_watcher(
    watcher: asyncio.Task,
    stopped: asyncio.Event,
) -> None:
    stopped.set()
    if not watcher.done():
        watcher.cancel()
    await asyncio.gather(watcher, return_exceptions=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """构建 FastAPI 生命周期内共享的服务对象。

    Args:
        app: FastAPI 应用实例。

    Yields:
        None: 启动阶段创建服务，关闭阶段释放引用。
    """
    global application_runtime_services, chat_service

    app.state.runtime_lifecycle_state = RuntimeLifecycleState.STARTING
    initialization_stack = RuntimeInitializationStack()

    memory_manager = await initialization_stack.create(
        "memory_manager",
        lambda: MemoryManager(db_path=settings.memory_db_path),
    )
    blocking_executor = await initialization_stack.create(
        "blocking_executor",
        BoundedBlockingExecutor,
        close_operation="shutdown",
    )
    coordinated_step_executor = await initialization_stack.create(
        "coordinated_step_executor",
        lambda: BoundedBlockingExecutor(
            thread_name_prefix="coordinated-step"
        ),
        close_operation="shutdown",
    )
    legacy_step_executor = await initialization_stack.create(
        "legacy_step_executor",
        lambda: BoundedBlockingExecutor(
            thread_name_prefix="legacy-step"
        ),
        close_operation="shutdown",
    )
    span_recorder = await initialization_stack.create(
        "span_recorder",
        InMemorySpanRecorder,
    )
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
                "Knowledge base runtime initialized",
                extra={
                    "component": "knowledge_base",
                    "phase": "initialization",
                    "status": "COMPLETED",
                    "configured": True,
                    "storage_type": "chroma",
                    "document_count": db_manager.count(),
                },
            )
        except Exception:
            knowledge_base_error = "KNOWLEDGE_BASE_INITIALIZATION_FAILED"
            logger.warning(
                "Knowledge base runtime initialization failed safely",
                extra={
                    "safe_error_code": knowledge_base_error,
                    "component": "knowledge_base",
                    "phase": "initialization",
                    "status": "FAILED",
                    "configured": True,
                    "storage_type": "chroma",
                },
            )
    else:
        knowledge_base_error = "KNOWLEDGE_BASE_IMPORT_FAILED"
        logger.warning(
            "Knowledge base runtime import failed safely",
            extra={
                "safe_error_code": knowledge_base_error,
                "component": "knowledge_base",
                "phase": "import",
                "status": "FAILED",
                "configured": VectorDBManager is not None,
                "storage_type": "chroma",
            },
        )

    engines = {}
    profiles = []
    if settings.llm_backend in {"local", "hybrid"}:
        local_engine = await initialization_stack.create(
            "local_model_engine",
            lambda: LocalLLMEngine(
                model_path=settings.model_path,
                n_ctx=settings.model_context,
                n_threads=settings.model_threads,
                n_gpu_layers=settings.model_gpu_layers,
            ),
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
            await initialization_stack.fail(
                RuntimeError(
                    "启用远程模型时必须配置 LOCAL_AGENT_REMOTE_API_BASE_URL"
                )
            )
        remote_engine = await initialization_stack.create(
            "remote_model_engine",
            lambda: RemoteLLMEngine(
                api_base_url=settings.remote_api_base_url,
                model_name=settings.remote_model_name,
                api_key=settings.remote_api_key,
                timeout_seconds=settings.remote_timeout_seconds,
                verify_tls=settings.remote_verify_tls,
                enable_thinking=settings.remote_enable_thinking,
                provider_kind=settings.remote_provider_kind,
            ),
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
        await initialization_stack.fail(
            RuntimeError("LOCAL_AGENT_LLM_BACKEND 必须是 local、remote 或 hybrid")
        )
    engine = engines.get(ModelProfileId.LOCAL_FAST) or engines[ModelProfileId.REMOTE_ADVANCED]
    breaker_registry = ModelCircuitBreakerRegistry(
        ModelCircuitBreakerConfig(
            failure_threshold=settings.model_breaker_failure_threshold,
            recovery_timeout_seconds=settings.model_breaker_recovery_timeout_seconds,
            half_open_max_calls=settings.model_breaker_half_open_max_calls,
            count_rate_limited=settings.model_breaker_count_rate_limited,
        )
    )
    router = await initialization_stack.create(
        "agent_router",
        lambda: AgentRouter(
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
            span_recorder=span_recorder,
            blocking_executor=blocking_executor,
        ),
    )
    await initialization_stack.run(
        lambda: register_all_tools(router),
        component="tool_registry",
    )
    runtime_metrics = InMemoryMetricsRecorder(
        label_policy=MetricLabelPolicy(
            tool_name_allowlist=frozenset(settings.metrics_tool_name_allowlist)
        )
    )
    infrastructure_metrics = RecorderInfrastructureMetricsHook(runtime_metrics)
    blocking_executor.set_metrics_hook(infrastructure_metrics)
    run_registry = RunRegistry()
    gauge_provider = ApplicationRuntimeGaugeProvider(
        run_registry=run_registry,
        blocking_executor=blocking_executor,
        tool_workers=router.tool_execution_service.concurrency_controller,
        circuit_registry=breaker_registry,
    )
    structured_logger = JsonStructuredRuntimeLogger()
    logger_checkpoints = await initialization_stack.create(
        "logger_checkpoint_store",
        lambda: SQLiteEventConsumptionCheckpointStore(
            settings.observability_checkpoint_db_path
        ),
    )
    metrics_checkpoints = await initialization_stack.create(
        "metrics_checkpoint_store",
        lambda: SQLiteEventConsumptionCheckpointStore(
            settings.observability_checkpoint_db_path
        ),
    )
    observability_dispatcher = await initialization_stack.create(
        "observability_dispatcher",
        lambda: RuntimeObservabilityDispatcher(
            logger_projector=StructuredLogProjector(structured_logger),
            metrics_projector=RuntimeMetricsProjector(runtime_metrics),
            logger_checkpoint_store=logger_checkpoints,
            metrics_checkpoint_store=metrics_checkpoints,
            queue_capacity=settings.observability_queue_capacity,
            infrastructure_hook=infrastructure_metrics,
            gauge_provider=gauge_provider,
        ),
    )
    event_journal = await initialization_stack.create(
        "event_journal",
        lambda: SQLiteRunEventJournal(
            settings.event_journal_db_path,
            metrics_hook=infrastructure_metrics,
        ),
    )
    snapshot_store = (
        await initialization_stack.create(
            "snapshot_store",
            lambda: SQLiteSnapshotStore(settings.snapshot_store_db_path),
        )
        if settings.snapshot_store_enabled
        else None
    )
    recovery_validator = (
        RecoveryValidator(
            snapshot_store=snapshot_store,
            journal=event_journal,
        )
        if snapshot_store is not None
        else None
    )
    application_runtime_services = await initialization_stack.run(
        lambda: ApplicationRuntimeServices(
            event_journal=event_journal,
            observability_dispatcher=observability_dispatcher,
            structured_logger=structured_logger,
            runtime_metrics_recorder=runtime_metrics,
            span_recorder=span_recorder,
            snapshot_store=snapshot_store,
            recovery_validator=recovery_validator,
            model_invocation_router=router.model_invocation_router,
            tool_execution_service=router.tool_execution_service,
            retrieval_execution_service=router.retrieval_execution_service,
            blocking_executors=(
                blocking_executor,
                coordinated_step_executor,
                legacy_step_executor,
            ),
            worker_trackers=(
                router.tool_execution_service.concurrency_controller,
            ),
            run_registry=run_registry,
            coordinated_step_executor=coordinated_step_executor,
            legacy_step_executor=legacy_step_executor,
            snapshot_enabled=settings.snapshot_store_enabled,
            recovery_enabled=settings.snapshot_store_enabled,
            extra_closeables=(
                ("logger_checkpoint_store", logger_checkpoints),
                ("metrics_checkpoint_store", metrics_checkpoints),
                *tuple(
                    (f"model_engine_{index}", engine)
                    for index, engine in enumerate(engines.values())
                ),
            ),
        ),
        component="application_runtime_services",
    )
    initialization_stack.release()
    initialization_stack = RuntimeInitializationStack()
    initialization_stack.track(
        "application_runtime_services",
        application_runtime_services,
    )
    coordinated_runtime_factory = await initialization_stack.create(
        "coordinated_runtime_factory",
        lambda: CoordinatedRuntimeFactory(
            router,
            application_runtime_services,
        ),
    )
    chat_service = await initialization_stack.create(
        "chat_service",
        lambda: ChatService(
            router,
            event_journal=event_journal,
            observability_dispatcher=observability_dispatcher,
            gauge_provider=gauge_provider,
            runtime_selector=ChatRuntimeSelector(settings.chat_runtime_mode),
            coordinated_runtime_factory=coordinated_runtime_factory,
            run_registry=run_registry,
            admission_gate=application_runtime_services.admission_gate,
            legacy_step_executor=legacy_step_executor,
            disconnect_grace_seconds=(
                settings.runtime_disconnect_grace_seconds
            ),
        ),
    )
    shutdown_coordinator = GracefulShutdownCoordinator(
        application_runtime_services,
        shutdown_grace_seconds=settings.runtime_shutdown_grace_seconds,
        component_timeout_seconds=(
            settings.runtime_component_close_timeout_seconds
        ),
    )
    app.state.runtime_services = application_runtime_services
    app.state.coordinated_runtime_factory = coordinated_runtime_factory
    app.state.runtime_metrics = runtime_metrics
    app.state.runtime_metrics_collector = RuntimeMetricsCollector(
        runtime_metrics, gauge_provider
    )
    app.state.runtime_observability = observability_dispatcher
    app.state.runtime_admission_gate = (
        application_runtime_services.admission_gate
    )
    app.state.runtime_shutdown_coordinator = shutdown_coordinator
    app.state.runtime_lifecycle_state = RuntimeLifecycleState.READY
    initialization_stack.release()
    try:
        yield
    finally:
        app.state.runtime_lifecycle_state = RuntimeLifecycleState.SHUTTING_DOWN
        shutdown_report = await shutdown_coordinator.shutdown()
        if shutdown_report.error_codes:
            logger.warning(
                "Runtime services closed with safe lifecycle issues",
                extra={
                    "component": "graceful_shutdown_coordinator",
                    "phase": "shutdown",
                    "status": "PARTIAL",
                    "safe_error_codes": shutdown_report.error_codes,
                    "cancelled_run_count": (
                        shutdown_report.cancelled_run_count
                    ),
                    "forced_run_count": shutdown_report.forced_run_count,
                    "remaining_run_count": (
                        shutdown_report.remaining_run_count
                    ),
                    "detached_worker_count": (
                        shutdown_report.detached_worker_count
                    ),
                },
            )
        chat_service = None
        application_runtime_services = None
        app.state.runtime_lifecycle_state = RuntimeLifecycleState.CLOSED


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


def _run_registry_for(service) -> object:
    """Use the lifespan-owned registry, preserving test/legacy compatibility."""
    return getattr(service, "run_registry", process_run_registry)


def _close_legacy_stream(stream) -> None:
    """Best-effort close after the worker has stopped touching the generator."""
    close = getattr(stream, "close", None)
    if close is None:
        return
    try:
        close()
    except (RuntimeError, ValueError):
        # A running generator is closed by the worker completion callback.
        pass


def _submit_legacy_stream_step(service, stream, run_id: str):
    submit = getattr(service, "submit_legacy_stream_step", None)
    if callable(submit):
        return submit(lambda: _next_or_none(stream), run_id=run_id)
    return process_legacy_step_executor.submit_nowait(
        lambda: _next_or_none(stream),
        kind=BlockingTaskKind.LEGACY_STREAM_STEP,
        run_id=run_id,
        operation_id="legacy_stream_next",
        cancellation_check=lambda: None,
    )


@app.post("/api/chat")
async def chat_endpoint(payload: ChatRequest, request: Request):
    """流式返回聊天结果。

    Args:
        request: 已校验的聊天请求对象。

    Returns:
        StreamingResponse: 纯文本增量响应流。
    """
    service = require_service()
    mode = service.selected_runtime_mode()
    run_registry = _run_registry_for(service)
    admission_gate = getattr(service, "admission_gate", None)
    if (
        admission_gate is not None
        and not admission_gate.accepts_new_runs
    ):
        raise HTTPException(
            status_code=503, detail="RUNTIME_SHUTTING_DOWN"
        )

    run_id = payload.run_id or uuid.uuid4().hex
    try:
        uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid run_id") from exc

    if mode is ChatRuntimeMode.LEGACY:
        stream = service.stream_chat(
            agent_id=payload.agent_id,
            query=payload.query,
            file_path=payload.file_path,
            run_id=run_id,
        )

        async def generate():
            """Bridge the selected synchronous Legacy text stream."""
            disconnected = asyncio.Event()
            stopped = asyncio.Event()
            watcher = asyncio.create_task(
                _watch_request_disconnect(
                    request,
                    run_registry=run_registry,
                    run_id=run_id,
                    disconnected=disconnected,
                    stopped=stopped,
                )
            )
            active_worker = None
            try:
                while True:
                    if disconnected.is_set():
                        return
                    active_worker = _submit_legacy_stream_step(
                        service, stream, run_id
                    )
                    next_task = asyncio.create_task(
                        active_worker.result_async()
                    )
                    disconnect_task = asyncio.create_task(
                        disconnected.wait()
                    )
                    done, _ = await asyncio.wait(
                        {next_task, disconnect_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if disconnect_task in done and disconnected.is_set():
                        if not next_task.done():
                            next_task.cancel()
                        await asyncio.gather(
                            next_task, return_exceptions=True
                        )
                        return
                    disconnect_task.cancel()
                    await asyncio.gather(
                        disconnect_task, return_exceptions=True
                    )
                    chunk = await next_task
                    active_worker = None
                    if chunk is None:
                        return
                    if disconnected.is_set():
                        return
                    yield chunk
            except RuntimeAdmissionRejectedError:
                if not disconnected.is_set():
                    yield "[runtime-error] RUNTIME_SHUTTING_DOWN\n"
            except BlockingExecutorClosedError:
                if not disconnected.is_set():
                    yield "[runtime-error] RUNTIME_SHUTTING_DOWN\n"
            except BlockingExecutorAdmissionTimeout:
                if not disconnected.is_set():
                    yield (
                        "[runtime-error] "
                        "LEGACY_WORKER_ADMISSION_REJECTED\n"
                    )
            except RunCancelledError:
                return
            except asyncio.CancelledError:
                run_registry.cancel(
                    run_id, CancellationReason.CLIENT_DISCONNECTED
                )
                raise
            except (BrokenPipeError, ConnectionResetError):
                run_registry.cancel(
                    run_id, CancellationReason.CLIENT_DISCONNECTED
                )
            except Exception:
                if not disconnected.is_set():
                    yield "[runtime-error] RUNTIME_EXECUTION_FAILED\n"
            finally:
                if active_worker is not None:
                    wait_state = active_worker.cancel_or_detach()
                    if wait_state.background_work_pending:
                        active_worker.add_done_callback(
                            lambda: _close_legacy_stream(stream)
                        )
                await _stop_disconnect_watcher(watcher, stopped)
                _close_legacy_stream(stream)

    elif mode is ChatRuntimeMode.COORDINATED:
        coordinated_query = payload.query
        if payload.file_path:
            coordinated_query += (
                f"\n\nPlease analyze this file path: '{payload.file_path}'"
            )
        stream = service.stream_coordinated_agent_text(
            agent_id=payload.agent_id,
            query=coordinated_query,
            run_id=run_id,
        )

        async def generate():
            """Forward the selected custom coordinated text-chunk stream."""
            disconnected = asyncio.Event()
            stopped = asyncio.Event()
            watcher = asyncio.create_task(
                _watch_request_disconnect(
                    request,
                    run_registry=run_registry,
                    run_id=run_id,
                    disconnected=disconnected,
                    stopped=stopped,
                )
            )
            try:
                async for chunk in stream:
                    if disconnected.is_set():
                        return
                    yield chunk
            except asyncio.CancelledError:
                run_registry.cancel(
                    run_id, CancellationReason.CLIENT_DISCONNECTED
                )
                raise
            except (BrokenPipeError, ConnectionResetError):
                run_registry.cancel(
                    run_id, CancellationReason.CLIENT_DISCONNECTED
                )
            except Exception:
                if not disconnected.is_set():
                    yield "[runtime-error] RUNTIME_EXECUTION_FAILED\n"
            finally:
                await _stop_disconnect_watcher(watcher, stopped)
                await stream.aclose()

    else:  # pragma: no cover - ChatRuntimeMode is a closed enum
        raise RuntimeError("unreachable chat runtime mode")

    return _RequestOwnedStreamingResponse(
        generate(),
        media_type="text/plain",
        headers={"X-Run-Id": run_id},
    )


@app.post("/api/runtime/runs/{run_id}/cancel")
async def cancel_run_endpoint(run_id: str):
    """客户端只能请求用户主动取消，重复请求保持幂等。"""
    try:
        uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid run_id") from exc
    service = require_service()
    result = _run_registry_for(service).cancel(
        run_id, CancellationReason.REQUEST_CANCELLED
    )
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
