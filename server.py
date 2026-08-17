#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""FastAPI 后端入口。"""

from contextlib import asynccontextmanager
import asyncio
import logging
import uuid
from typing import Annotated, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from core.agent_router import AgentRouter
from core.application_metadata import create_application_metadata
from core.chat_service import ChatService
from core.llm_engine import LocalLLMEngine, RemoteLLMEngine
from core.memory_manager import MemoryManager
from core.request_payload import (
    REQUEST_PAYLOAD_POLICY,
    RequestBodyLimitMiddleware,
)
from core.persistence_migration import (
    PERSISTENCE_PREFLIGHT_FAILED,
    PersistenceError,
    PersistencePaths,
    PreflightMode,
    PreflightStatus,
    preflight_blocks_startup,
    run_persistence_preflight,
)
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
    RuntimeInitializationError,
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
    StartupDependencySnapshot,
    FilesystemResourcePolicy,
    ResourceAuthorizationService,
    ResourceKind,
    ResourceOperation,
    ToolResourceExtractorCatalog,
    ToolResourceExtractorDescriptor,
    process_legacy_step_executor,
    process_run_registry,
)
from core.runtime.health import (
    health_http_status,
    readiness_http_status,
    resolve_application_diagnostic,
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
from core.runtime.tool_registry import ToolRegistry
from core.runtime.tool_governance import (
    ToolGovernanceService,
    ToolPolicyCatalog,
    register_default_tool_policies,
)
from core.runtime.agent_registry import DEFAULT_AGENT_REGISTRY
from core.runtime.agent_evalops_trace_exporter import AgentEvalOpsTraceExporter
from core.runtime.trace_export_dispatcher import TraceExportDispatcher
from core.settings import SERVER_ROLE, Settings, validate_role_configuration
from tools.registry import register_all_tools

# WP4-C：AgentEvalOps trace export dispatcher 的 code-owned bounded queue 容量
# （最小配置约束：不新增 Settings；默认与 observability queue 一致）。
AGENTEVALOPS_TRACE_EXPORT_QUEUE_CAPACITY = 256


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


def _populate_tool_registry() -> ToolRegistry:
    """构造并冻结生产 ToolRegistry；非法/重复注册在此 fail closed。"""
    registry = ToolRegistry()
    register_all_tools(registry)
    registry.freeze()
    return registry


def _build_tool_governance(tool_registry: ToolRegistry) -> ToolGovernanceService:
    """构造/校验/冻结 ToolPolicyCatalog 并创建唯一 ToolGovernanceService。

    任一 missing/duplicate/unknown policy 引用、unknown/disabled Agent 引用、
    非法 risk fact / approval rule 都使 startup fail closed（never READY）。
    """
    catalog = ToolPolicyCatalog(
        tool_registry=tool_registry,
        agent_registry=DEFAULT_AGENT_REGISTRY,
    )
    register_default_tool_policies(catalog)
    catalog.freeze()
    return ToolGovernanceService(catalog, DEFAULT_AGENT_REGISTRY)


def _build_resource_authorization(
    tool_registry: ToolRegistry,
) -> ResourceAuthorizationService:
    """构造、校验并冻结 application-scoped File Tool read policy。"""
    catalog = ToolResourceExtractorCatalog()
    catalog.register(
        ToolResourceExtractorDescriptor(
            tool_name="list_files",
            argument_key="argument_text",
            resource_kind=ResourceKind.DIRECTORY,
            operation=ResourceOperation.READ,
        )
    )
    catalog.register(
        ToolResourceExtractorDescriptor(
            tool_name="analyze_excel",
            argument_key="argument_text",
            resource_kind=ResourceKind.FILE,
            operation=ResourceOperation.READ,
        )
    )
    catalog.validate(tool_registry)
    catalog.freeze()
    policy = FilesystemResourcePolicy(settings.tool_allowed_read_roots)
    return ResourceAuthorizationService(policy, catalog)


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


class _ChromaRebuildRequiredError(RuntimeError):
    """内部信号：Chroma collection 缺 marker 或 marker mismatch，需 operator rebuild。"""


def _publish_compatibility_handles(app: FastAPI, service, services) -> None:
    """Publish identical application-scope compatibility handles."""
    global application_runtime_services, chat_service
    chat_service = service
    application_runtime_services = services
    app.state.chat_service = service
    app.state.runtime_services = services

def _clear_compatibility_handles(app: FastAPI) -> None:
    """Invalidate both compatibility views after application shutdown."""
    global application_runtime_services, chat_service
    chat_service = None
    application_runtime_services = None
    app.state.chat_service = None
    app.state.runtime_services = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """构建 FastAPI 生命周期内共享的服务对象。

    Args:
        app: FastAPI 应用实例。

    Yields:
        None: 启动阶段创建服务，关闭阶段释放引用。
    """
    global application_runtime_services, chat_service

    # 配置 Parse / Semantic / Role failure 必须发生在首个 Application Resource
    # 构造之前；role validation 不做任何 I/O。
    validate_role_configuration(settings, role=SERVER_ROLE)
    app.state.application_metadata = create_application_metadata(settings)

    app.state.runtime_lifecycle_state = RuntimeLifecycleState.STARTING
    initialization_stack = RuntimeInitializationStack()

    # WP1-D：自动 SQLite persistence preflight（READ ONLY + quick_check）。
    # 必须在任何持久 Store constructor 之前执行；migration-required /
    # unsupported / failed 都阻止 READY。Preflight 不创建、不修改任何 DB 文件。
    persistence_paths = PersistencePaths(
        memory_db_path=settings.memory_db_path,
        event_journal_db_path=settings.event_journal_db_path,
        observability_checkpoint_db_path=settings.observability_checkpoint_db_path,
        snapshot_store_db_path=(
            settings.snapshot_store_db_path
            if settings.snapshot_store_enabled
            else None
        ),
    )
    try:
        persistence_preflight_results = run_persistence_preflight(
            persistence_paths, mode=PreflightMode.STARTUP
        )
    except PersistenceError as exc:
        logger.warning(
            "Persistence preflight failed",
            extra={
                "safe_error_code": exc.error_code,
                "component": "persistence_preflight",
                "phase": "initialization",
                "status": "FAILED",
            },
        )
        await initialization_stack.fail(
            RuntimeInitializationError("persistence_preflight")
        )
    blocking_preflight = preflight_blocks_startup(persistence_preflight_results)
    if blocking_preflight:
        for result in blocking_preflight:
            logger.warning(
                "Persistence preflight blocked startup",
                extra={
                    "safe_error_code": (
                        result.safe_error_code or PERSISTENCE_PREFLIGHT_FAILED
                    ),
                    "component": "persistence_preflight",
                    "phase": "initialization",
                    "status": result.status.value,
                    "store_id": result.store_id.value,
                },
            )
        await initialization_stack.fail(
            RuntimeInitializationError("persistence_preflight")
        )

    memory_manager = await initialization_stack.create(
        "memory_manager",
        lambda: MemoryManager(db_path=settings.memory_db_path),
    )
    blocking_executor = await initialization_stack.create(
        "blocking_executor",
        lambda: BoundedBlockingExecutor(
            max_workers=settings.blocking_max_workers,
            max_pending_tasks=settings.blocking_max_pending_tasks,
        ),
        close_operation="shutdown",
    )
    coordinated_step_executor = await initialization_stack.create(
        "coordinated_step_executor",
        lambda: BoundedBlockingExecutor(
            thread_name_prefix="coordinated-step",
            max_workers=settings.blocking_max_workers,
            max_pending_tasks=settings.blocking_max_pending_tasks,
        ),
        close_operation="shutdown",
    )
    legacy_step_executor = await initialization_stack.create(
        "legacy_step_executor",
        lambda: BoundedBlockingExecutor(
            thread_name_prefix="legacy-step",
            max_workers=settings.blocking_max_workers,
            max_pending_tasks=settings.blocking_max_pending_tasks,
        ),
        close_operation="shutdown",
    )
    # WP4-C：enabled 时构造 AgentEvalOpsTraceExporter 并注入 TraceExportDispatcher；
    # disabled 时保持 WP4-B 现状（无 exporter/dispatcher、无 HTTP 依赖）。
    # exporter 构造失败（含 PycURL/libcurl 能力缺失）由 initialization rollback
    # 以 startup-fatal 处理；remote 暂不可达不阻止启动，不执行 startup probe。
    trace_export_dispatcher = None
    if settings.agentevalops_trace_export_enabled:
        agent_evalops_exporter = await initialization_stack.create(
            "agentevalops_trace_exporter",
            lambda: AgentEvalOpsTraceExporter(
                base_url=settings.agentevalops_base_url,
                api_key=settings.agentevalops_api_key,
                project_id=settings.agentevalops_project_id,
                connect_timeout_seconds=(
                    settings.agentevalops_connect_timeout_seconds
                ),
                total_deadline_seconds=(
                    settings.agentevalops_total_deadline_seconds
                ),
            ),
        )
        trace_export_dispatcher = await initialization_stack.create(
            "trace_export_dispatcher",
            lambda: TraceExportDispatcher(
                exporter=agent_evalops_exporter,
                queue_capacity=AGENTEVALOPS_TRACE_EXPORT_QUEUE_CAPACITY,
            ),
        )
    span_recorder = await initialization_stack.create(
        "span_recorder",
        lambda: InMemorySpanRecorder(
            completion_observer=(
                trace_export_dispatcher.observe_completed_span
                if trace_export_dispatcher is not None
                else None
            )
        ),
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
            # WP1-D：Chroma LocalAgent collection marker validation。空 collection
            # 允许 startup marker initialization；非空不匹配/缺 marker →
            # REBUILD_REQUIRED（required KB 阻止 READY，optional KB 走 degraded）。
            # startup 绝不自动 clear / rebuild。
            chroma_preflight = db_manager.collection_preflight()
            if chroma_preflight.status is PreflightStatus.NEW:
                db_manager.publish_collection_marker()
            elif chroma_preflight.status is PreflightStatus.REBUILD_REQUIRED:
                raise _ChromaRebuildRequiredError()
            logger.info(
                "Knowledge base runtime initialized",
                extra={
                    "component": "knowledge_base",
                    "phase": "initialization",
                    "status": "COMPLETED",
                    "configured": True,
                    "storage_type": "chroma",
                    "document_count": db_manager.count(),
                    "collection_status": chroma_preflight.status.value,
                },
            )
        except _ChromaRebuildRequiredError:
            knowledge_base_error = "KNOWLEDGE_BASE_REBUILD_REQUIRED"
            if settings.knowledge_base_required:
                await initialization_stack.fail(
                    RuntimeInitializationError("knowledge_base")
                )
            logger.warning(
                "Knowledge base collection rebuild required",
                extra={
                    "safe_error_code": knowledge_base_error,
                    "component": "knowledge_base",
                    "phase": "initialization",
                    "status": "FAILED",
                    "configured": True,
                    "storage_type": "chroma",
                },
            )
        except Exception:
            knowledge_base_error = "KNOWLEDGE_BASE_INITIALIZATION_FAILED"
            if settings.knowledge_base_required:
                await initialization_stack.fail(
                    RuntimeInitializationError("knowledge_base")
                )
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
        if settings.knowledge_base_required:
            await initialization_stack.fail(
                RuntimeInitializationError("knowledge_base")
            )
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
                trust_env=settings.remote_trust_env,
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
    tool_registry = await initialization_stack.run(
        _populate_tool_registry,
        component="tool_registry",
    )
    # WP2-B Tool Governance：Registry freeze 后构造/校验/冻结 ToolPolicyCatalog
    # 并创建唯一 Service；任一 policy 校验失败 -> startup fail（never READY）。
    tool_governance_service = await initialization_stack.run(
        lambda: _build_tool_governance(tool_registry),
        component="tool_governance",
    )
    resource_authorization_service = await initialization_stack.run(
        lambda: _build_resource_authorization(tool_registry),
        component="resource_authorization",
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
            tool_registry=tool_registry,
            tool_governance_service=tool_governance_service,
            resource_authorization_service=resource_authorization_service,
        ),
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
            trace_export_dispatcher=trace_export_dispatcher,
            snapshot_enabled=settings.snapshot_store_enabled,
            recovery_enabled=settings.snapshot_store_enabled,
            startup_dependency_snapshot=StartupDependencySnapshot(
                knowledge_base_degraded=knowledge_base_error is not None
            ),
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
            event_channel_capacity=settings.event_channel_capacity,
            planning_timeout_seconds=settings.planning_timeout_seconds,
            step_result_per_result_chars=settings.step_result_per_result_chars,
            step_result_run_total_chars=settings.step_result_run_total_chars,
            step_result_max_entries=settings.step_result_max_entries,
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
    _publish_compatibility_handles(
        app,
        chat_service,
        application_runtime_services,
    )
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
        _clear_compatibility_handles(app)
        app.state.runtime_lifecycle_state = RuntimeLifecycleState.CLOSED


app = FastAPI(title="Local Agent API", lifespan=lifespan)
app.add_middleware(
    RequestBodyLimitMiddleware,
    policy=REQUEST_PAYLOAD_POLICY,
)


class ChatRequest(BaseModel):
    """聊天流式接口的请求体。"""

    agent_id: Annotated[
        str, Field(max_length=REQUEST_PAYLOAD_POLICY.AGENT_ID_MAX_CHARS)
    ]
    query: Annotated[
        str, Field(max_length=REQUEST_PAYLOAD_POLICY.CHAT_QUERY_MAX_CHARS)
    ]
    file_path: Annotated[
        str,
        Field(max_length=REQUEST_PAYLOAD_POLICY.CHAT_FILE_PATH_MAX_CHARS),
    ] = ""
    run_id: Annotated[
        str, Field(max_length=REQUEST_PAYLOAD_POLICY.RUN_ID_MAX_CHARS)
    ] | None = None


MessageId = Annotated[
    int,
    Field(
        ge=REQUEST_PAYLOAD_POLICY.MESSAGE_ID_MIN,
        le=REQUEST_PAYLOAD_POLICY.MESSAGE_ID_MAX,
    ),
]


class DeleteMemoryRequest(BaseModel):
    """删除记忆接口的请求体。"""

    message_ids: list[MessageId] = Field(
        default_factory=list,
        max_length=REQUEST_PAYLOAD_POLICY.DELETE_MESSAGE_IDS_MAX_COUNT,
    )
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


@app.get("/health")
async def health_endpoint():
    """Health 只证明 application 尚未进入 terminal CLOSED / fatal unavailable。

    不证明可以接受新 Run、所有依赖健康或所有 endpoint 可用。
    """
    snapshot = resolve_application_diagnostic(
        application_runtime_services,
        fallback_lifecycle=getattr(
            app.state, "runtime_lifecycle_state", None
        ),
    )
    return JSONResponse(
        content=snapshot.to_safe_dict(),
        status_code=health_http_status(snapshot),
    )


@app.get("/readyz")
async def readiness_endpoint():
    """Readiness 证明可以安全尝试接受一个新的 Run。

    条件：services 可用 + lifecycle READY + admission ACCEPTING；
    唯一 allowlisted KB degradation 不阻止该结论。
    """
    snapshot = resolve_application_diagnostic(
        application_runtime_services,
        fallback_lifecycle=getattr(
            app.state, "runtime_lifecycle_state", None
        ),
    )
    return JSONResponse(
        content=snapshot.to_safe_dict(),
        status_code=readiness_http_status(snapshot),
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
async def cancel_run_endpoint(
    run_id: Annotated[
        str, Path(max_length=REQUEST_PAYLOAD_POLICY.RUN_ID_MAX_CHARS)
    ],
):
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
async def get_history_endpoint(
    agent_id: Annotated[
        str, Path(max_length=REQUEST_PAYLOAD_POLICY.AGENT_ID_MAX_CHARS)
    ],
    limit: Annotated[
        int,
        Query(
            ge=REQUEST_PAYLOAD_POLICY.HISTORY_LIMIT_MIN,
            le=REQUEST_PAYLOAD_POLICY.HISTORY_LIMIT_MAX,
        ),
    ] = REQUEST_PAYLOAD_POLICY.HISTORY_LIMIT_DEFAULT,
    offset: Annotated[
        int,
        Query(
            ge=REQUEST_PAYLOAD_POLICY.HISTORY_OFFSET_MIN,
            le=REQUEST_PAYLOAD_POLICY.HISTORY_OFFSET_MAX,
        ),
    ] = REQUEST_PAYLOAD_POLICY.HISTORY_OFFSET_DEFAULT,
):
    """按页返回某个智能体的历史消息。"""
    service = require_service()
    return {"messages": service.get_history(agent_id=agent_id, limit=limit, offset=offset)}


@app.get("/api/search")
async def search_endpoint(
    keyword: Annotated[
        str,
        Query(max_length=REQUEST_PAYLOAD_POLICY.SEARCH_KEYWORD_MAX_CHARS),
    ],
):
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
