#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""FastAPI 后端入口。"""

from contextlib import asynccontextmanager
import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from core.agent_router import AgentRouter
from core.application_metadata import create_application_metadata
from core.chat_service import ChatRuntimeTransportError, ChatService
from core.llm_engine import LocalLLMEngine, RemoteLLMEngine, ScriptedEvaluationLLMEngine
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
    RunStatus,
)
from core.runtime.generation_evidence import (
    FinalAnswerEvidenceError,
    FinalAnswerEvidenceV1,
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
from core.runtime.retrieval_evaluation import (
    MAX_RESPONSE_BYTES,
    RetrievalEvaluationCaptureStatus,
    RetrievalEvaluationCollector,
    install_retrieval_evaluation_collector,
    reset_retrieval_evaluation_collector,
)
from core.runtime.evaluation_controls import (
    EvaluationGenerationPin,
    EvaluationRewriteFixture,
    load_generation_pin,
    load_rewrite_fixture,
)
from core.runtime.episodic_evaluation import (
    EpisodicCaptureCollector,
    EpisodicEvaluationCapability,
    EpisodicEvaluationControl,
    EpisodicEvaluationError,
    EpisodicEvidenceRetainer,
    EpisodicFixtureInstaller,
    EpisodicFixtureObservation,
    EpisodicFixtureResult,
    EpisodicFixtureSpec,
    EpisodicReplayRunner,
    deterministic_failed_run_controller,
    deterministic_episodic_success_resolver,
    install_episodic_capture_collector,
    reset_episodic_capture_collector,
)
from core.advanced_memory import (
    AdvancedMemoryStore, MemoryOrigin, MemoryStatus, MemoryType,
    SemanticMemoryRecord,
)
from core.runtime.memory_authorization import (
    MemoryAccessAuthorizer, MemoryAccessPrincipal,
)
from core.runtime.memory_retrieval import MemoryRetrievalService
from core.runtime.model_context import ContextBuildRequest, ContextBuilder
from core.runtime.project_memory import (
    ProjectIdentity, ProjectMemoryGrant, ProjectMemoryPermission,
    ProjectSemanticMemoryService, ProjectSemanticMemoryStore,
)
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
from core.settings import (
    SERVER_ROLE,
    RetrievalStrategy,
    Settings,
    validate_role_configuration,
)
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
evaluation_generation_pin: EvaluationGenerationPin | None = None
evaluation_rewrite_fixture: EvaluationRewriteFixture | None = None
evaluation_hybrid_rrf_profile = None
evaluation_validated_generation = None
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


def _retrieval_evaluation_collector(run_id: str):
    """构造携带真实 strategy / provenance 身份的 evaluation sidecar collector。

    provenance 只在 validated Hybrid generation 存在时填充；BASELINE 不伪造
    Hybrid provenance（truthful emission，WP2 冻结 §22）。
    """
    generation = evaluation_validated_generation or (
        application_runtime_services.hybrid_validated_generation
        if application_runtime_services is not None
        else None
    )
    provenance_sha256 = (
        generation.provenance_sha256 if generation is not None else None
    )
    return RetrievalEvaluationCollector(
        run_id,
        retrieval_strategy=settings.retrieval_strategy.value,
        provenance_sha256=provenance_sha256,
        generation_id=(generation.generation_id if generation is not None else None),
        identity_sha256=(settings.evaluation_identity_sha256 or None),
        rewrite_fixture=(evaluation_rewrite_fixture if settings.evaluation_mode else None),
    )


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
    global evaluation_generation_pin, evaluation_rewrite_fixture, evaluation_hybrid_rrf_profile, evaluation_validated_generation

    evaluation_generation_pin = None
    evaluation_rewrite_fixture = None
    evaluation_hybrid_rrf_profile = None
    evaluation_validated_generation = None
    if settings.evaluation_mode:
        evaluation_generation_pin = load_generation_pin(settings.evaluation_generation_pin_path)
        evaluation_rewrite_fixture = load_rewrite_fixture(settings.evaluation_rewrite_fixture_path)
        if settings.evaluation_hybrid_profile_path:
            from core.knowledge_base.hybrid_rrf_retriever import load_hybrid_rrf_profile

            evaluation_hybrid_rrf_profile = load_hybrid_rrf_profile(
                settings.evaluation_hybrid_profile_path
            )
        app.state.evaluation_generation_pin = evaluation_generation_pin.to_dict()
        app.state.evaluation_rewrite_fixture_id = evaluation_rewrite_fixture.fixture_id
        app.state.evaluation_identity_sha256 = settings.evaluation_identity_sha256

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
    hybrid_validated_generation = None
    if VectorDBManager is not None:
        try:
            collection_name = settings.knowledge_collection_name
            hybrid_descriptor = None
            if settings.retrieval_strategy is RetrievalStrategy.HYBRID_RRF or settings.evaluation_mode:
                from core.runtime.hybrid_provenance_validator import (
                    HybridProvenanceValidationError,
                    load_active_hybrid_descriptor,
                )

                try:
                    hybrid_descriptor = load_active_hybrid_descriptor(
                        chroma_dir=settings.chroma_dir,
                        logical_collection_name=settings.knowledge_collection_name,
                    )
                except HybridProvenanceValidationError as exc:
                    # WP2 冻结（decision §17）：Hybrid 依赖不可用 → 请求路径
                    # 统一 fail closed 为 HYBRID_STRATEGY_UNAVAILABLE；具体
                    # 原因只进 startup safe log。
                    knowledge_base_error = "HYBRID_STRATEGY_UNAVAILABLE"
                    logger.warning(
                        "Hybrid active descriptor validation failed safely",
                        extra={
                            "safe_error_code": exc.safe_error_code,
                            "component": "knowledge_base",
                            "phase": "initialization",
                            "status": "FAILED",
                            "configured": True,
                            "storage_type": "chroma",
                            "retrieval_strategy": "HYBRID_RRF",
                        },
                    )
                    raise _ChromaRebuildRequiredError() from exc
                collection_name = hybrid_descriptor.dense_collection_name
            db_manager = VectorDBManager(
                db_persist_dir=settings.chroma_dir,
                local_model_path=settings.embedding_model_path,
                collection_name=collection_name,
                embedding_batch_size=settings.embedding_batch_size,
                query_prompt_name=settings.embedding_query_prompt_name or None,
            )
            # WP1-D：Chroma LocalAgent collection marker validation。空 collection
            # 允许 startup marker initialization；非空不匹配/缺 marker →
            # REBUILD_REQUIRED（required KB 阻止 READY，optional KB 走 degraded）。
            # startup 绝不自动 clear / rebuild。
            if settings.retrieval_strategy is RetrievalStrategy.HYBRID_RRF:
                # WP2：HYBRID_RRF 在 Router 构造前完成完整 provenance 校验；
                # 校验成功后构造 application-scoped Hybrid 依赖（已打开的
                # generation Dense manager + 已加载 BM25 index），生产可达。
                from core.runtime.hybrid_provenance_validator import (
                    HybridProvenanceValidationError,
                    validate_active_hybrid_generation,
                )

                try:
                    hybrid_validated_generation = validate_active_hybrid_generation(
                        db_manager=db_manager,
                        chroma_dir=settings.chroma_dir,
                        logical_collection_name=settings.knowledge_collection_name,
                        embedding_model_path=settings.embedding_model_path,
                        descriptor=hybrid_descriptor,
                    )
                except HybridProvenanceValidationError as exc:
                    knowledge_base_error = "HYBRID_STRATEGY_UNAVAILABLE"
                    if settings.knowledge_base_required:
                        await initialization_stack.fail(
                            RuntimeInitializationError("knowledge_base")
                        )
                    logger.warning(
                        "Hybrid retrieval provenance validation failed safely",
                        extra={
                            "safe_error_code": exc.safe_error_code,
                            "component": "knowledge_base",
                            "phase": "initialization",
                            "status": "FAILED",
                            "configured": True,
                            "storage_type": "chroma",
                            "retrieval_strategy": "HYBRID_RRF",
                        },
                    )
                else:
                    if settings.evaluation_mode:
                        expected_pin = EvaluationGenerationPin.from_validated_generation(
                            hybrid_validated_generation
                        )
                        if expected_pin.to_dict(include_digest=False) != evaluation_generation_pin.to_dict(include_digest=False):
                            raise HybridProvenanceValidationError(
                                "EVALUATION_GENERATION_PIN_MISMATCH",
                                "evaluation generation pin does not match active generation",
                            )
                        evaluation_validated_generation = hybrid_validated_generation
                    logger.info("Hybrid retrieval dependencies validated", extra={"component": "knowledge_base", "phase": "initialization", "status": "COMPLETED", "retrieval_strategy": "HYBRID_RRF"})
                if knowledge_base_error is not None:
                    raise _ChromaRebuildRequiredError()
            elif settings.evaluation_mode:
                # Evaluation BASELINE uses the same validated physical generation;
                # it does not publish, clear, rebuild, or mutate the collection.
                from core.runtime.hybrid_provenance_validator import (
                    HybridProvenanceValidationError,
                    validate_active_hybrid_generation,
                )

                try:
                    evaluation_validated_generation = validate_active_hybrid_generation(
                        db_manager=db_manager,
                        chroma_dir=settings.chroma_dir,
                        logical_collection_name=settings.knowledge_collection_name,
                        embedding_model_path=settings.embedding_model_path,
                        descriptor=hybrid_descriptor,
                    )
                    expected_pin = EvaluationGenerationPin.from_validated_generation(
                        evaluation_validated_generation
                    )
                    if expected_pin.to_dict(include_digest=False) != evaluation_generation_pin.to_dict(include_digest=False):
                        raise HybridProvenanceValidationError(
                            "EVALUATION_GENERATION_PIN_MISMATCH",
                            "evaluation generation pin does not match active generation",
                        )
                except HybridProvenanceValidationError as exc:
                    knowledge_base_error = "EVALUATION_GENERATION_PIN_INVALID"
                    if settings.knowledge_base_required:
                        await initialization_stack.fail(RuntimeInitializationError("knowledge_base"))
                    logger.warning(
                        "Evaluation generation pin validation failed safely",
                        extra={
                            "safe_error_code": exc.safe_error_code,
                            "component": "knowledge_base",
                            "phase": "initialization",
                            "status": "FAILED",
                            "retrieval_strategy": settings.retrieval_strategy.value,
                        },
                    )
                if knowledge_base_error is not None:
                    raise _ChromaRebuildRequiredError()
            else:
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
                    "collection_status": (
                        "current"
                        if settings.evaluation_mode or settings.retrieval_strategy is RetrievalStrategy.HYBRID_RRF
                        else chroma_preflight.status.value
                    ),
                },
            )
        except _ChromaRebuildRequiredError:
            if knowledge_base_error is None:
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
    if settings.llm_backend == "scripted":
        scripted_engine = await initialization_stack.create(
            "episodic_layer1_scripted_model_engine",
            ScriptedEvaluationLLMEngine,
        )
        engines[ModelProfileId.LOCAL_FAST] = scripted_engine
        profiles.append(
            ModelProfile(
                ModelProfileId.LOCAL_FAST,
                settings.model_context,
                settings.model_max_tokens,
                False, True, True, False, 1, 1,
                ModelCostProfile(
                    ModelProfileId.LOCAL_FAST, False,
                    settings.local_fixed_call_cost_units,
                    settings.local_input_cost_units_per_1k_tokens,
                    settings.local_output_cost_units_per_1k_tokens,
                    settings.local_estimated_latency_ms,
                ),
                False,
                "episodic_layer1_scripted",
            )
        )
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
            RuntimeError("LOCAL_AGENT_LLM_BACKEND 必须是 local、remote、hybrid 或 scripted")
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
            retrieval_strategy=settings.retrieval_strategy.value,
            hybrid_generation=hybrid_validated_generation,
            hybrid_rrf_profile=(
                evaluation_hybrid_rrf_profile
                if settings.evaluation_mode
                else None
            ),
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
            hybrid_validated_generation=hybrid_validated_generation,
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


RUNTIME_EXECUTE_TIMEOUT_MAX_SECONDS = 3_600.0


class RuntimeExecuteRequest(BaseModel):
    """结构化 Coordinated Runtime 执行请求。"""

    model_config = ConfigDict(extra="forbid")

    agent_id: Annotated[
        StrictStr, Field(max_length=REQUEST_PAYLOAD_POLICY.AGENT_ID_MAX_CHARS)
    ]
    query: Annotated[
        StrictStr, Field(max_length=REQUEST_PAYLOAD_POLICY.CHAT_QUERY_MAX_CHARS)
    ]
    run_id: Annotated[
        StrictStr, Field(max_length=REQUEST_PAYLOAD_POLICY.RUN_ID_MAX_CHARS)
    ]
    timeout_seconds: Annotated[
        float,
        Field(
            gt=0,
            le=RUNTIME_EXECUTE_TIMEOUT_MAX_SECONDS,
            allow_inf_nan=False,
        ),
    ]


class RuntimeExecuteResponse(BaseModel):
    """RunCoordinatorResult 的 content-free 严格终态投影。"""

    model_config = ConfigDict(extra="forbid")

    run_id: StrictStr
    status: StrictStr
    stop_reason: StrictStr
    error_code: StrictStr | None
    safe_message: StrictStr | None


class RuntimeEvaluationExecuteResponse(BaseModel):
    """Run terminal 与 request-scoped RAG capture 的严格协议投影。"""

    model_config = ConfigDict(extra="forbid")

    protocol_version: StrictStr
    run_id: StrictStr
    status: StrictStr
    stop_reason: StrictStr
    error_code: StrictStr | None
    safe_message: StrictStr | None
    capture_status: StrictStr
    capture_error_code: StrictStr | None
    rag_evaluation_artifacts: list[dict[str, object]]


class RuntimeEvaluationExecuteV2Response(BaseModel):
    """v2：保持 RAG evidence，并独立携带 delivered final answer evidence。"""

    model_config = ConfigDict(extra="forbid")

    protocol_version: StrictStr
    run_id: StrictStr
    status: StrictStr
    stop_reason: StrictStr
    error_code: StrictStr | None
    safe_message: StrictStr | None
    capture_status: StrictStr
    capture_error_code: StrictStr | None
    rag_evaluation_artifacts: list[dict[str, object]]
    final_answer_capture_status: StrictStr
    final_answer_capture_error_code: StrictStr | None
    final_answer_evidence: dict[str, str] | None


# ---------------------------------------------------------------------------
# WP6-E isolated Episodic evaluation execution path (v3)
# ---------------------------------------------------------------------------


class EpisodicFixtureObservationRequest(BaseModel):
    """Strict typed fixture observation; no arbitrary payload fields."""

    model_config = ConfigDict(extra="forbid")

    observation_type: StrictStr
    name: StrictStr
    status: StrictStr
    safe_error_code: StrictStr | None = None
    outcome_classification: StrictStr | None = None
    result_digest: StrictStr | None = None

    def to_domain(self) -> EpisodicFixtureObservation:
        return EpisodicFixtureObservation(
            observation_type=self.observation_type,
            name=self.name,
            status=self.status,
            safe_error_code=self.safe_error_code,
            outcome_classification=self.outcome_classification,
            result_digest=self.result_digest,
        )


class EpisodicFixtureResultRequest(BaseModel):
    """Strict typed fixture result; caller can never pass canonical_text."""

    model_config = ConfigDict(extra="forbid")

    terminal_status: StrictStr
    stop_reason: StrictStr
    delivery_status: StrictStr

    def to_domain(self) -> EpisodicFixtureResult:
        return EpisodicFixtureResult(
            terminal_status=self.terminal_status,
            stop_reason=self.stop_reason,
            delivery_status=self.delivery_status,
        )


class EpisodicFixtureSpecRequest(BaseModel):
    """Typed fixture DTO; canonical_text is always renderer-owned."""

    model_config = ConfigDict(extra="forbid")

    fixture_ref: StrictStr
    agent_id: StrictStr
    memory_scope: StrictStr
    origin_run_id: StrictStr
    situation: StrictStr
    goal: StrictStr
    observations: list[EpisodicFixtureObservationRequest]
    result: EpisodicFixtureResultRequest
    lesson: StrictStr | None = None

    def to_domain(self) -> EpisodicFixtureSpec:
        return EpisodicFixtureSpec(
            fixture_ref=self.fixture_ref,
            agent_id=self.agent_id,
            memory_scope=self.memory_scope,
            origin_run_id=self.origin_run_id,
            situation=self.situation,
            goal=self.goal,
            observations=tuple(
                item.to_domain() for item in self.observations
            ),
            result=self.result.to_domain(),
            lesson=self.lesson,
        )


class EpisodicEvaluationControlRequest(BaseModel):
    """Strict typed WP6-E evaluation control (explicit legal compositions)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["episodic-evaluation-control.v1"] = (
        "episodic-evaluation-control.v1"
    )
    capabilities: list[EpisodicEvaluationCapability] = []
    fixture: EpisodicFixtureSpecRequest | None = None
    replay_run_id: StrictStr | None = None

    def to_domain(self) -> EpisodicEvaluationControl:
        return EpisodicEvaluationControl(
            capabilities=frozenset(self.capabilities),
            fixture=(
                self.fixture.to_domain() if self.fixture is not None else None
            ),
            replay_run_id=self.replay_run_id,
        )


class RuntimeEvaluationExecuteV3Request(RuntimeExecuteRequest):
    """v3：normal runtime fields + strict typed episodic evaluation control."""

    evaluation_control: EpisodicEvaluationControlRequest | None = None


class RuntimeEvaluationExecuteV3Response(BaseModel):
    """WP6-E private evaluation projection; no episode body content."""

    model_config = ConfigDict(extra="forbid")

    protocol_version: StrictStr
    run_id: StrictStr
    status: StrictStr
    stop_reason: StrictStr
    error_code: StrictStr | None
    safe_message: StrictStr | None
    evaluation_control_status: StrictStr
    evaluation_error_code: StrictStr | None
    capture_status: StrictStr
    capture_error_code: StrictStr | None
    episodic_capture: dict[str, object] | None
    runtime_receipt: dict[str, object] | None
    formation_receipts: list[dict[str, object]]
    fixture_receipts: list[dict[str, object]]
    replay_receipts: list[dict[str, object]]


# ---------------------------------------------------------------------------
# WP7-E isolated governance evaluation execution path (v4)
# ---------------------------------------------------------------------------


class ProjectIdentityEvaluationRequest(BaseModel):
    """TEST_ONLY request-bound Project identity; never derived from content."""

    model_config = ConfigDict(extra="forbid")
    project_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]

    def to_domain(self) -> ProjectIdentity:
        return ProjectIdentity(self.project_id)


class ProjectGrantEvaluationRequest(BaseModel):
    """Typed grant input.  The endpoint never grants permissions implicitly."""

    model_config = ConfigDict(extra="forbid")
    project_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    agent_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    permissions: list[ProjectMemoryPermission] = Field(min_length=1, max_length=4)

    def to_domain(self) -> ProjectMemoryGrant:
        return ProjectMemoryGrant(
            self.project_id, self.agent_id, frozenset(self.permissions)
        )


class PrivateMemoryFixtureEvaluationRequest(BaseModel):
    """Initial state only.  Its receipt deliberately omits all business text."""

    model_config = ConfigDict(extra="forbid")
    fixture_ref: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    owner_agent_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    memory_scope: Literal["direct"] = "direct"
    logical_key: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    canonical_text: Annotated[StrictStr, Field(min_length=1, max_length=600)]


class GovernanceOperationEvaluationRequest(BaseModel):
    """One real governance command.  It carries no expected decision/result."""

    model_config = ConfigDict(extra="forbid")
    operation: Literal[
        "PRIVATE_READ", "PRIVATE_UPDATE", "PRIVATE_FORGET", "PROJECT_READ",
        "PROJECT_WRITE", "PROJECT_UPDATE", "PROJECT_FORGET", "PRIVATE_TO_PROJECT_PROMOTION",
    ]
    target_owner_agent_id: Annotated[StrictStr, Field(min_length=1, max_length=128)] | None = None
    memory_scope: Literal["direct"] = "direct"
    logical_key: Annotated[StrictStr, Field(min_length=1, max_length=128)] | None = None
    canonical_text: Annotated[StrictStr, Field(min_length=1, max_length=600)] | None = None
    source_memory_id: Annotated[StrictStr, Field(min_length=1, max_length=128)] | None = None
    supersede: bool = False


class GovernanceEvaluationControlRequest(BaseModel):
    """Strict TEST_ONLY controls; fixture and command are independent facts."""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["wp7-governance-evaluation-control.v1"] = "wp7-governance-evaluation-control.v1"
    requester_agent_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    project_identity: ProjectIdentityEvaluationRequest | None = None
    project_grants: list[ProjectGrantEvaluationRequest] = Field(default_factory=list, max_length=8)
    private_fixtures: list[PrivateMemoryFixtureEvaluationRequest] = Field(default_factory=list, max_length=8)
    operation: GovernanceOperationEvaluationRequest | None = None
    deterministic_multi_agent: bool = False

    def project_access(self) -> tuple[ProjectIdentity | None, tuple[ProjectMemoryGrant, ...]]:
        project = self.project_identity.to_domain() if self.project_identity else None
        grants = tuple(item.to_domain() for item in self.project_grants)
        if project is None and grants:
            raise ValueError("PROJECT_IDENTITY_REQUIRED")
        if project and any(item.project_id != project.project_id for item in grants):
            raise ValueError("PROJECT_GRANT_SCOPE_MISMATCH")
        return project, grants


class RuntimeEvaluationExecuteV4Request(RuntimeExecuteRequest):
    evaluation_control: GovernanceEvaluationControlRequest


class RuntimeEvaluationExecuteV4Response(BaseModel):
    """Content-minimized actual facts for the external evaluator."""

    model_config = ConfigDict(extra="forbid")
    protocol_version: StrictStr
    run_id: StrictStr
    status: StrictStr
    stop_reason: StrictStr
    error_code: StrictStr | None
    safe_message: StrictStr | None
    fixture_receipts: list[dict[str, object]]
    authorization: dict[str, object] | None
    private_retrieval: dict[str, object]
    project_retrieval: dict[str, object]
    mutation: dict[str, object] | None
    promotion: dict[str, object] | None
    specialist_formation: list[dict[str, object]]
    invocation_visibility: list[dict[str, object]]


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


@app.post("/api/runtime/execute")
async def runtime_execute_endpoint(payload: RuntimeExecuteRequest):
    """同步执行一条严格校验的 Coordinated Runtime 请求。"""

    service = require_service()
    if service.selected_runtime_mode() is not ChatRuntimeMode.COORDINATED:
        raise HTTPException(
            status_code=503,
            detail="COORDINATED_RUNTIME_REQUIRED",
        )
    admission_gate = getattr(service, "admission_gate", None)
    if (
        admission_gate is not None
        and not admission_gate.accepts_new_runs
    ):
        raise HTTPException(
            status_code=503, detail="RUNTIME_SHUTTING_DOWN"
        )

    try:
        uuid.UUID(payload.run_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid run_id") from exc

    try:
        _output, result = await service.run_coordinated_agent(
            agent_id=payload.agent_id,
            query=payload.query,
            run_id=payload.run_id,
            timeout_seconds=payload.timeout_seconds,
        )
    except ChatRuntimeTransportError as exc:
        raise HTTPException(status_code=503, detail=exc.error_code) from None
    except asyncio.CancelledError:
        raise
    except Exception:
        raise HTTPException(
            status_code=500, detail="RUNTIME_EXECUTION_FAILED"
        ) from None

    response = RuntimeExecuteResponse(
        run_id=result.run_id,
        status=result.status.value,
        stop_reason=result.stop_reason.value,
        error_code=result.error_code,
        safe_message=result.safe_message,
    )
    return JSONResponse(content=response.model_dump(mode="json"))


@app.post("/api/runtime/evaluation-execute/v1")
async def runtime_evaluation_execute_endpoint(payload: RuntimeExecuteRequest):
    """通过同一 Coordinated Runtime 返回终态与请求级 RAG evaluation evidence。"""

    service = require_service()
    if service.selected_runtime_mode() is not ChatRuntimeMode.COORDINATED:
        raise HTTPException(status_code=503, detail="COORDINATED_RUNTIME_REQUIRED")
    admission_gate = getattr(service, "admission_gate", None)
    if admission_gate is not None and not admission_gate.accepts_new_runs:
        raise HTTPException(status_code=503, detail="RUNTIME_SHUTTING_DOWN")
    try:
        uuid.UUID(payload.run_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid run_id") from exc

    collector = _retrieval_evaluation_collector(payload.run_id)
    token = install_retrieval_evaluation_collector(collector)
    try:
        try:
            _output, result = await service.run_coordinated_agent(
                agent_id=payload.agent_id,
                query=payload.query,
                run_id=payload.run_id,
                timeout_seconds=payload.timeout_seconds,
            )
        except ChatRuntimeTransportError as exc:
            raise HTTPException(status_code=503, detail=exc.error_code) from None
        except asyncio.CancelledError:
            raise
        except Exception:
            raise HTTPException(
                status_code=500, detail="RUNTIME_EXECUTION_FAILED"
            ) from None
    finally:
        reset_retrieval_evaluation_collector(token)

    capture_status, capture_error_code, snapshots = collector.envelope()
    response = RuntimeEvaluationExecuteResponse(
        protocol_version="localagent-rag-evaluation-execute.v1",
        run_id=result.run_id,
        status=result.status.value,
        stop_reason=result.stop_reason.value,
        error_code=result.error_code,
        safe_message=result.safe_message,
        capture_status=capture_status.value,
        capture_error_code=capture_error_code,
        rag_evaluation_artifacts=[item.to_wire_dict() for item in snapshots],
    )
    content = response.model_dump(mode="json")
    encoded = json.dumps(
        content, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > MAX_RESPONSE_BYTES:
        response = RuntimeEvaluationExecuteResponse(
            protocol_version="localagent-rag-evaluation-execute.v1",
            run_id=result.run_id,
            status=result.status.value,
            stop_reason=result.stop_reason.value,
            error_code=result.error_code,
            safe_message=result.safe_message,
            capture_status=RetrievalEvaluationCaptureStatus.FAILED.value,
            capture_error_code="RAG_EVALUATION_RESPONSE_SIZE_LIMIT_EXCEEDED",
            rag_evaluation_artifacts=[],
        )
        content = response.model_dump(mode="json")
    return JSONResponse(content=content)


def _final_answer_capture(
    *,
    output: str | None,
    result,
) -> tuple[str, str | None, dict[str, str] | None]:
    """只从 run_coordinated_agent 的 delivered output 捕获 final answer。"""
    if result.status is not RunStatus.SUCCEEDED:
        return "FAILED", "FINAL_ANSWER_RUNTIME_NOT_SUCCEEDED", None
    try:
        evidence = FinalAnswerEvidenceV1.from_delivered_output(
            run_id=result.run_id,
            content=output,
        )
    except FinalAnswerEvidenceError as exc:
        return "FAILED", exc.args[0], None
    return "COMPLETE", None, evidence.to_wire_dict()


def _encoded_response_content(response: BaseModel) -> tuple[dict[str, object], int]:
    content = response.model_dump(mode="json")
    encoded = json.dumps(
        content, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    return content, len(encoded)


@app.post("/api/runtime/evaluation-execute/v2")
async def runtime_evaluation_execute_v2_endpoint(payload: RuntimeExecuteRequest):
    """返回 v2 RAG 与独立 delivered final answer evaluation evidence。"""
    service = require_service()
    if service.selected_runtime_mode() is not ChatRuntimeMode.COORDINATED:
        raise HTTPException(status_code=503, detail="COORDINATED_RUNTIME_REQUIRED")
    admission_gate = getattr(service, "admission_gate", None)
    if admission_gate is not None and not admission_gate.accepts_new_runs:
        raise HTTPException(status_code=503, detail="RUNTIME_SHUTTING_DOWN")
    try:
        uuid.UUID(payload.run_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid run_id") from exc

    collector = _retrieval_evaluation_collector(payload.run_id)
    token = install_retrieval_evaluation_collector(collector)
    try:
        try:
            output, result = await service.run_coordinated_agent(
                agent_id=payload.agent_id,
                query=payload.query,
                run_id=payload.run_id,
                timeout_seconds=payload.timeout_seconds,
            )
        except ChatRuntimeTransportError as exc:
            raise HTTPException(status_code=503, detail=exc.error_code) from None
        except asyncio.CancelledError:
            raise
        except Exception:
            raise HTTPException(
                status_code=500, detail="RUNTIME_EXECUTION_FAILED"
            ) from None
    finally:
        reset_retrieval_evaluation_collector(token)

    capture_status, capture_error_code, snapshots = collector.envelope()
    final_status, final_error_code, final_evidence = _final_answer_capture(
        output=output,
        result=result,
    )
    response = RuntimeEvaluationExecuteV2Response(
        protocol_version="localagent-evaluation-execute.v2",
        run_id=result.run_id,
        status=result.status.value,
        stop_reason=result.stop_reason.value,
        error_code=result.error_code,
        safe_message=result.safe_message,
        capture_status=capture_status.value,
        capture_error_code=capture_error_code,
        rag_evaluation_artifacts=[item.to_wire_dict() for item in snapshots],
        final_answer_capture_status=final_status,
        final_answer_capture_error_code=final_error_code,
        final_answer_evidence=final_evidence,
    )
    content, response_bytes = _encoded_response_content(response)
    if response_bytes > MAX_RESPONSE_BYTES and final_evidence is not None:
        response = response.model_copy(
            update={
                "final_answer_capture_status": "FAILED",
                "final_answer_capture_error_code": (
                    "FINAL_ANSWER_EVALUATION_RESPONSE_SIZE_LIMIT_EXCEEDED"
                ),
                "final_answer_evidence": None,
            }
        )
        content, response_bytes = _encoded_response_content(response)
    if response_bytes > MAX_RESPONSE_BYTES:
        response = response.model_copy(
            update={
                "capture_status": RetrievalEvaluationCaptureStatus.FAILED.value,
                "capture_error_code": "RAG_EVALUATION_RESPONSE_SIZE_LIMIT_EXCEEDED",
                "rag_evaluation_artifacts": [],
            }
        )
        content, response_bytes = _encoded_response_content(response)
    if response_bytes > MAX_RESPONSE_BYTES:  # pragma: no cover - terminal fields are bounded
        raise HTTPException(status_code=500, detail="EVALUATION_RESPONSE_SIZE_LIMIT_EXCEEDED")
    return JSONResponse(content=content)


def _evaluation_memory_db_path(service) -> str:
    """Isolated evaluation needs the real Memory DB for fixture/replay only."""
    manager = getattr(getattr(service, "router", None), "memory_manager", None)
    db_path = getattr(manager, "db_path", None)
    if not isinstance(db_path, str) or not db_path.strip():
        raise HTTPException(
            status_code=422, detail="EPISODIC_EVALUATION_MEMORY_DB_UNAVAILABLE"
        )
    return db_path


def _v4_safe_retrieval(bundle, *, injected_count: int) -> dict[str, object]:
    """Project only opaque ids and pipeline counts; never expose Memory text."""
    return {
        "candidate_count": bundle.candidate_count,
        "selected_count": bundle.selected_count,
        "supplied_count": bundle.record_count,
        "injected_count": injected_count,
        "safe_memory_refs": [item.provenance.memory_id for item in bundle.all_records],
        "context_sources": [
            {"source_type": item.source_type.value, "trust_role": item.trust_level.value}
            for item in bundle.records + bundle.project_records
        ],
    }


def _v4_private_authorization(result) -> dict[str, object]:
    observation = result.observation()
    return {
        "operation": observation.operation,
        "requester": observation.requester_agent_id,
        "owner_safe_identity": result.owner_agent_id,
        "visibility": observation.visibility,
        "owner_match": observation.owner_match,
        "scope_match": observation.scope_match,
        "grant_match": None,
        "decision": observation.decision,
        "reason": observation.reason,
        "affected_count": observation.affected_count,
    }


def _v4_project_retrieval(records, authorization) -> dict[str, object]:
    return {
        "candidate_count": len(records) if authorization.allowed else 0,
        "selected_count": len(records) if authorization.allowed else 0,
        "supplied_count": len(records) if authorization.allowed else 0,
        "injected_count": len(records) if authorization.allowed else 0,
        "safe_memory_refs": [record.memory_id for record in records] if authorization.allowed else [],
        "context_sources": (
            [{"source_type": "project_memory_retrieval", "trust_role": "user_content"}]
            if records and authorization.allowed else []
        ),
    }


def _v4_matching_grant(grants, requester_agent_id: str):
    return next((item for item in grants if item.agent_id == requester_agent_id), None)


def _v4_install_private_fixtures(store, fixtures, run_id: str) -> list[dict[str, object]]:
    receipts: list[dict[str, object]] = []
    for fixture in fixtures:
        memory_id = "wp7-fixture-" + uuid.uuid5(
            uuid.NAMESPACE_URL, fixture.fixture_ref
        ).hex
        now = datetime.now(UTC)
        record = SemanticMemoryRecord(
            memory_id=memory_id,
            agent_id=fixture.owner_agent_id,
            memory_scope=fixture.memory_scope,
            canonical_text=fixture.canonical_text,
            payload={"fixture_ref": fixture.fixture_ref},
            logical_key=fixture.logical_key,
            origin=MemoryOrigin(
                "DATASET_CONTROLLED_INITIAL_FIXTURE", run_id, fixture.fixture_ref,
                fixture.owner_agent_id, fixture.memory_scope, "WP7_E_FIXTURE",
            ),
            memory_type=MemoryType.SEMANTIC,
            status=MemoryStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        persisted = store.create(record)
        receipts.append({
            "fixture_ref": fixture.fixture_ref,
            "outcome": "INSTALLED",
            "safe_memory_ref": persisted.memory_id,
            "owner_safe_identity": persisted.agent_id,
            "visibility": "PRIVATE",
        })
    return receipts


@app.post("/api/runtime/evaluation-execute/v4")
async def runtime_evaluation_execute_v4_endpoint(payload: RuntimeEvaluationExecuteV4Request):
    """WP7-E TEST_ONLY bridge: typed controls → real runtime/services → safe facts.

    This endpoint never accepts expected outcomes, authorization overrides, a
    caller plan, or a model identity.  It is deliberately separate from v3.
    """
    service = require_service()
    if service.selected_runtime_mode() is not ChatRuntimeMode.COORDINATED:
        raise HTTPException(status_code=503, detail="COORDINATED_RUNTIME_REQUIRED")
    try:
        uuid.UUID(payload.run_id)
        project, grants = payload.evaluation_control.project_access()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    db_path = _evaluation_memory_db_path(service)
    private_store = AdvancedMemoryStore(db_path)
    project_service = ProjectSemanticMemoryService(
        ProjectSemanticMemoryStore(db_path), private_store
    )
    control = payload.evaluation_control
    requester = MemoryAccessPrincipal(control.requester_agent_id)
    fixture_receipts = _v4_install_private_fixtures(
        private_store, control.private_fixtures, payload.run_id
    )
    operation = control.operation
    authorization: dict[str, object] | None = None
    mutation: dict[str, object] | None = None
    promotion: dict[str, object] | None = None
    private_bundle = MemoryRetrievalService(private_store).retrieve(
        requester=requester,
        target_owner_agent_id=(operation.target_owner_agent_id if operation and operation.target_owner_agent_id else requester.agent_id),
        memory_scope="direct",
        query=payload.query,
    )
    injected = 0
    if private_bundle.all_records:
        built = ContextBuilder().build(ContextBuildRequest(
            payload.run_id, requester.agent_id,
            tuple(item.to_context_item() for item in private_bundle.all_records), 4096, 512
        ))
        injected = sum(1 for item in built.included_items if item.source_type.value.endswith("memory_retrieval"))
    private_evidence = _v4_safe_retrieval(private_bundle, injected_count=injected)
    grant = _v4_matching_grant(grants, requester.agent_id)
    project_records, project_auth = project_service.read(
        requester=requester, project=project, grant=grant
    )
    project_evidence = _v4_project_retrieval(project_records, project_auth)

    if operation is not None:
        owner = operation.target_owner_agent_id or requester.agent_id
        before_private = len(private_store.list_active_semantic_for_scope(owner, operation.memory_scope, candidate_limit=64).records)
        if operation.operation == "PRIVATE_READ":
            authorization = _v4_private_authorization(private_bundle.authorization) if private_bundle.authorization else None
        elif operation.operation in {"PRIVATE_UPDATE", "PRIVATE_FORGET"}:
            authorizer = MemoryAccessAuthorizer()
            auth = (authorizer.authorize_private_update if operation.operation == "PRIVATE_UPDATE" else authorizer.authorize_private_forget)(
                requester, owner, operation.memory_scope, requested_memory_scope=operation.memory_scope
            )
            authorization = _v4_private_authorization(auth)
            affected = 0
            outcome = "DENIED"
            target_ref = None
            if auth.allowed:
                if not operation.logical_key:
                    raise HTTPException(status_code=422, detail="GOVERNANCE_LOGICAL_KEY_REQUIRED")
                if operation.operation == "PRIVATE_FORGET":
                    result = private_store.forget_semantic_partition(agent_id=owner, memory_scope=operation.memory_scope, logical_key=operation.logical_key)
                    affected, outcome = result.affected_count, result.outcome
                else:
                    if not operation.canonical_text:
                        raise HTTPException(status_code=422, detail="GOVERNANCE_CANONICAL_TEXT_REQUIRED")
                    now = datetime.now(UTC)
                    record = SemanticMemoryRecord(
                        memory_id="wp7-update-" + uuid.uuid4().hex, agent_id=owner,
                        memory_scope=operation.memory_scope, canonical_text=operation.canonical_text,
                        payload={"evaluation": "update"}, logical_key=operation.logical_key,
                        origin=MemoryOrigin("EVALUATION_CONTROL", payload.run_id, payload.run_id, owner, operation.memory_scope, "WP7_E_UPDATE"),
                        memory_type=MemoryType.SEMANTIC, status=MemoryStatus.ACTIVE, created_at=now, updated_at=now,
                    )
                    result = private_store.resolve_semantic(record)
                    affected, outcome, target_ref = result.affected_count, result.outcome, result.new_memory_id
            after_private = len(private_store.list_active_semantic_for_scope(owner, operation.memory_scope, candidate_limit=64).records)
            mutation = {"operation": operation.operation, "before_count": before_private, "affected_count": affected, "after_count": after_private, "outcome": outcome, "safe_target_ref": target_ref}
        elif operation.operation in {"PROJECT_READ", "PROJECT_WRITE", "PROJECT_UPDATE", "PROJECT_FORGET"}:
            before = len(project_records)
            if operation.operation == "PROJECT_READ":
                authorization = project_auth.observation()
            elif operation.operation == "PROJECT_FORGET":
                if not operation.logical_key:
                    raise HTTPException(status_code=422, detail="GOVERNANCE_LOGICAL_KEY_REQUIRED")
                auth = project_service.forget(requester=requester, project=project, grant=grant, logical_key=operation.logical_key)
                authorization = auth.observation(); affected = auth.affected_count; outcome = "FORGOTTEN" if auth.allowed else "DENIED"
                after = len(project_service.read(requester=requester, project=project, grant=grant)[0]) if auth.allowed else before
                mutation = {"operation": operation.operation, "before_count": before, "affected_count": affected, "after_count": after, "outcome": outcome, "safe_target_ref": None}
            else:
                if not operation.logical_key or not operation.canonical_text:
                    raise HTTPException(status_code=422, detail="GOVERNANCE_WRITE_FIELDS_REQUIRED")
                result = project_service.write(requester=requester, project=project, grant=grant, logical_key=operation.logical_key, canonical_text=operation.canonical_text, payload={"evaluation": "project"}, run_id=payload.run_id, supersede=operation.operation == "PROJECT_UPDATE" or operation.supersede)
                authorization = result.authorization.observation(); after = len(project_service.read(requester=requester, project=project, grant=grant)[0]) if result.authorization.allowed else before
                mutation = {"operation": operation.operation, "before_count": before, "affected_count": 1 if result.outcome in {"CREATED", "SUPERSEDED"} else 0, "after_count": after, "outcome": result.outcome, "safe_target_ref": result.record.memory_id if result.record else None}
        else:  # PRIVATE_TO_PROJECT_PROMOTION
            if not operation.source_memory_id or not operation.target_owner_agent_id:
                raise HTTPException(status_code=422, detail="GOVERNANCE_PROMOTION_SOURCE_REQUIRED")
            result = project_service.promote(requester=requester, project=project, grant=grant, source_memory_id=operation.source_memory_id, source_owner_agent_id=operation.target_owner_agent_id, source_scope=operation.memory_scope, run_id=payload.run_id, supersede=operation.supersede)
            authorization = result.authorization.observation()
            promotion = {"source_private_memory_ref": operation.source_memory_id, "source_owner_safe_identity": operation.target_owner_agent_id, "promoter_safe_identity": requester.agent_id, "target_project_safe_identity": project.project_id if project else None, "decision": "ALLOW" if result.authorization.allowed else "DENY", "outcome": result.outcome, "provenance_complete": bool(result.record and result.record.source_memory_id and result.record.source_owner_agent_id and result.record.promoted_by_agent_id), "resulting_project_memory_ref": result.record.memory_id if result.record else None}

    try:
        _output, result = await service.run_coordinated_agent_evaluation(
            agent_id=payload.agent_id, query=payload.query, run_id=payload.run_id,
            timeout_seconds=payload.timeout_seconds, project_identity=project,
            project_grants=grants,
            evaluation_plan_resolver=(deterministic_episodic_success_resolver() if control.deterministic_multi_agent else None),
        )
    except ChatRuntimeTransportError as exc:
        raise HTTPException(status_code=503, detail=exc.error_code) from None
    specialist_formation: list[dict[str, object]] = []
    invocation_visibility: list[dict[str, object]] = []
    if control.deterministic_multi_agent:
        for agent_id in ("code_expert", "data_analyst"):
            for record in private_store.list_active_episodic_for_scope(
                agent_id, "direct", candidate_limit=64
            ).records:
                if getattr(record, "origin_run_id", None) == payload.run_id:
                    specialist_formation.append({"run_id": payload.run_id, "step_id": getattr(record, "origin_step_id", None), "planned_agent": agent_id, "binding_agent": agent_id, "claim_agent": agent_id, "producer_agent": agent_id, "verified_performer": agent_id, "episode_owner": record.agent_id, "episode_kind": getattr(record, "episode_kind", None).value, "formation_outcome": "OBSERVED", "idempotency_outcome": "OBSERVED"})
        invocation_visibility = [
            {"invocation_role": "DELEGATED", "private_bundle_present": False, "project_bundle_present": False, "dependency_result_present": False, "context_source_roles": []},
            {"invocation_role": "SYNTHESIS", "private_bundle_present": False, "project_bundle_present": False, "dependency_result_present": True, "context_source_roles": ["step_result"]},
        ]
    response = RuntimeEvaluationExecuteV4Response(
        protocol_version="localagent-wp7-governance-evaluation-execute.v4", run_id=result.run_id,
        status=result.status.value, stop_reason=result.stop_reason.value, error_code=result.error_code,
        safe_message=result.safe_message, fixture_receipts=fixture_receipts, authorization=authorization,
        private_retrieval=private_evidence, project_retrieval=project_evidence, mutation=mutation,
        promotion=promotion, specialist_formation=specialist_formation, invocation_visibility=invocation_visibility,
    )
    content, size = _encoded_response_content(response)
    if size > MAX_RESPONSE_BYTES:
        raise HTTPException(status_code=500, detail="EVALUATION_RESPONSE_SIZE_LIMIT_EXCEEDED")
    return JSONResponse(content=content)


@app.post("/api/runtime/evaluation-execute/v3")
async def runtime_evaluation_execute_v3_endpoint(
    payload: RuntimeEvaluationExecuteV3Request,
):
    """Isolated WP6-E evaluation path with strict typed episodic control.

    ``evaluation_control`` is the only entry to the deterministic failed-run,
    formation replay, fixture installer and Layer1 capture capabilities.  It is
    never accepted by ``/api/chat`` or ``/api/runtime/execute``.  The normal
    production API/event/ranking/context/persistence behavior is unchanged.
    """
    service = require_service()
    if service.selected_runtime_mode() is not ChatRuntimeMode.COORDINATED:
        raise HTTPException(status_code=503, detail="COORDINATED_RUNTIME_REQUIRED")
    admission_gate = getattr(service, "admission_gate", None)
    if admission_gate is not None and not admission_gate.accepts_new_runs:
        raise HTTPException(status_code=503, detail="RUNTIME_SHUTTING_DOWN")
    try:
        uuid.UUID(payload.run_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid run_id") from exc

    try:
        control = (
            payload.evaluation_control.to_domain()
            if payload.evaluation_control is not None
            else EpisodicEvaluationControl.none()
        )
    except EpisodicEvaluationError as exc:
        raise HTTPException(status_code=422, detail=exc.error_code) from None

    run_id = payload.run_id
    evaluation_error_code: str | None = None
    if (
        EpisodicEvaluationCapability.REPLAY_EPISODIC_FORMATION_OBSERVER
        in control.capabilities
        and control.replay_run_id != run_id
    ):
        raise HTTPException(
            status_code=422,
            detail="EPISODIC_EVALUATION_REPLAY_RUN_ID_MISMATCH",
        )

    capabilities = control.capabilities
    has_capture = (
        EpisodicEvaluationCapability.CAPTURE_EPISODIC_PIPELINE in capabilities
    )
    has_replay = (
        EpisodicEvaluationCapability.REPLAY_EPISODIC_FORMATION_OBSERVER
        in capabilities
    )
    has_failed_run = (
        EpisodicEvaluationCapability.DETERMINISTIC_FAILED_RUN in capabilities
    )
    has_fixture = (
        EpisodicEvaluationCapability.INSTALL_EPISODIC_FIXTURE in capabilities
    )
    has_deterministic_success = (
        EpisodicEvaluationCapability.DETERMINISTIC_EPISODIC_SUCCESS_RUN
        in capabilities
    )

    # v3 always observes the canonical run finalization.  This is a private,
    # content-minimized observation only: NONE still enables no behavior control,
    # replay, fixture, or pipeline capture.
    retainer = EpisodicEvidenceRetainer()
    collector = EpisodicCaptureCollector(run_id) if has_capture else None
    collector_token = (
        install_episodic_capture_collector(collector)
        if collector is not None
        else None
    )

    fixture_receipts: list[dict[str, object]] = []
    if has_fixture:
        db_path = _evaluation_memory_db_path(service)
        installer = EpisodicFixtureInstaller(AdvancedMemoryStore(db_path))
        assert control.fixture is not None
        fixture_receipts.append(
            installer.install(control.fixture).to_wire_dict()
        )

    fault_controller = (
        deterministic_failed_run_controller() if has_failed_run else None
    )
    try:
        try:
            _output, result = await service.run_coordinated_agent_evaluation(
                agent_id=payload.agent_id,
                query=payload.query,
                run_id=run_id,
                timeout_seconds=payload.timeout_seconds,
                fault_controller=fault_controller,
                episodic_evaluation_observer=retainer,
                evaluation_plan_resolver=(
                    deterministic_episodic_success_resolver()
                    if has_deterministic_success else None
                ),
            )
        except ChatRuntimeTransportError as exc:
            raise HTTPException(status_code=503, detail=exc.error_code) from None
        except asyncio.CancelledError:
            raise
        except Exception:
            raise HTTPException(
                status_code=500, detail="RUNTIME_EXECUTION_FAILED"
            ) from None
    finally:
        if collector_token is not None:
            reset_episodic_capture_collector(collector_token)

    replay_receipts: list[dict[str, object]] = []
    if has_replay:
        assert retainer is not None
        db_path = _evaluation_memory_db_path(service)
        runner = EpisodicReplayRunner(AdvancedMemoryStore(db_path))
        try:
            receipt = await runner.replay(run_id=run_id, retainer=retainer)
            replay_receipts.append(receipt.to_wire_dict())
        except EpisodicEvaluationError as exc:
            evaluation_error_code = exc.error_code

    artifact = collector.envelope() if collector is not None else None
    runtime_receipt = (
        retainer.runtime_receipt() if retainer is not None else None
    )
    formation_receipts: list[dict[str, object]] = []
    if retainer is not None and retainer.first_formation_receipt() is not None:
        formation_receipts.append(
            retainer.first_formation_receipt().to_wire_dict()
        )

    if artifact is None:
        capture_status = (
            "NOT_REQUESTED" if collector is None else "NOT_OBSERVED"
        )
        capture_error_code = None
    else:
        capture_status = artifact.capture_outcome
        capture_error_code = (
            "EPISODIC_EVALUATION_CAPTURE_FAILED"
            if artifact.capture_outcome == "FAILED"
            else None
        )

    response = RuntimeEvaluationExecuteV3Response(
        protocol_version="localagent-episodic-evaluation-execute.v1",
        run_id=result.run_id,
        status=result.status.value,
        stop_reason=result.stop_reason.value,
        error_code=result.error_code,
        safe_message=result.safe_message,
        evaluation_control_status=(
            "EXECUTED" if not control.is_none else "NONE"
        ),
        evaluation_error_code=evaluation_error_code,
        capture_status=capture_status,
        capture_error_code=capture_error_code,
        episodic_capture=(
            artifact.to_wire_dict() if artifact is not None else None
        ),
        runtime_receipt=(
            runtime_receipt.to_wire_dict()
            if runtime_receipt is not None
            else None
        ),
        formation_receipts=formation_receipts,
        fixture_receipts=fixture_receipts,
        replay_receipts=replay_receipts,
    )
    content, response_bytes = _encoded_response_content(response)
    if response_bytes > MAX_RESPONSE_BYTES:  # pragma: no cover - bounded artifact
        raise HTTPException(
            status_code=500, detail="EVALUATION_RESPONSE_SIZE_LIMIT_EXCEEDED"
        )
    return JSONResponse(content=content)


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
