"""复用标准 AgentApplicationService / Router / Runtime 的 opt-in 装配。"""

from __future__ import annotations

import asyncio
import json
import hashlib
from uuid import uuid4


from core.agent_platform.application import AgentApplicationService
from core.agent_platform.business_tool_adapter import BusinessToolAdapter
from core.agent_platform.contracts import AgentRegistration, BusinessToolDefinition
from core.agent_platform.registry import AgentRegistrationBundle, compile_agent_catalog
from core.agent_router import AgentRouter
from core.chat_service import ChatService
from core.llm_engine import provider_response_observer
from core.persistence import (
    PostgresMemoryManager,
    PostgresMemoryManagerBridge,
    SyncPersistenceBridge,
)
from core.persistence.repositories.execution import DurableExecutionRepository
from core.runtime import (
    ApplicationRuntimeServices,
    CoordinatedRuntimeFactory,
    RunRegistry,
)
from core.runtime.blocking_executor import BoundedBlockingExecutor
from core.runtime.durable_approval import DurableApprovalService
from core.runtime.event_consumer import InMemoryEventConsumptionCheckpointStore
from core.runtime.metrics import NoopMetricsRecorder, RuntimeMetricsProjector
from core.runtime.model_invocation import (
    GeneratorModelAdapter,
    ModelAdapterResolver,
    ModelInvocationRouter,
)
from core.runtime.model_selection import (
    ModelCostProfile,
    ModelProfile,
    ModelProfileId,
    ModelResolver,
)
from core.runtime.observability_dispatcher import RuntimeObservabilityDispatcher
from core.runtime.event_journal_store import PostgresRunEventJournal
from core.runtime.tool_snapshot_store import PostgresToolResolutionSnapshotStore
from core.runtime.resource_authorization import (
    FilesystemResourcePolicy,
    ResourceAuthorizationService,
    ToolResourceExtractorCatalog,
)
from core.runtime.retry import RetryExecutor, RetryPolicy
from core.runtime.run_control import DurableRunControlService
from core.runtime.structured_logging import (
    NoopStructuredRuntimeLogger,
    StructuredLogProjector,
)
from core.runtime.tool_governance import (
    ToolGovernanceService,
    ToolPolicy,
    ToolPolicyCatalog,
)
from core.runtime.tool_idempotency import DurableToolInvocationService
from core.runtime.tool_registry import ToolDescriptor, ToolRegistration, ToolRegistry
from core.runtime.tracing import NoopSpanRecorder
from core.stage13.triage_models import TriageRunRow
from core.stage13.triage_subject import definitions, manifest_for, digest
from core.stage13.model_comparability import project_receipt


class TriageModelAdapter(GeneratorModelAdapter):
    """统一 Model Invocation 内的单次 Provider adapter；durable 计数禁止重发。"""

    def __init__(self, engine, service, model_config):
        super().__init__(engine)
        self.service, self.model_config = service, model_config

    async def ainvoke(self, messages, *, run_context=None, **kwargs):
        run_id = run_context.run_id
        async with self.service.database.transaction() as session:
            row = await session.get(TriageRunRow, run_id, with_for_update=True)
            if row is None:
                raise ValueError("UNBOUND_STAGE13_MODEL_CALL")
            if row.model_call is not None:
                raise ValueError("MODEL_CALL_ALREADY_DISPATCHED")
            manifest = next(
                m
                for m in self.service.manifests.values()
                if m["subject_manifest_digest"] == row.subject_digest
            )
            call = {
                "call_id": str(uuid4()),
                "run_id": run_id,
                "role": row.role,
                "resolved_profile_id": "remote_advanced",
                "resolved_profile_digest": digest(self.model_config),
                "model_config_digest": digest(self.model_config),
                "requested_provider": self.model_config["provider"],
                "requested_model": self.model_config["model"],
                "requested_revision": self.model_config["revision"],
                "reported_provider": None,
                "reported_model": None,
                "reported_revision": None,
                "actual_revision": None,
                "resolved_provider": self._engine.provider_kind,
                "resolved_provider_source": "CONFIGURED_TRANSPORT",
                "resolved_endpoint": self._engine.api_base_url,
                "resolved_model_config": {
                    **self.model_config,
                    "provider": self._engine.provider_kind,
                    "model": self._engine.model_name,
                    "endpoint_digest": hashlib.sha256(
                        self._engine.api_base_url.encode()
                    ).hexdigest(),
                },
                "system_fingerprint": None,
                "reported_artifact_digest": None,
                "reported_deployment_id": None,
                "dispatch_certainty": "MAY_HAVE_DISPATCHED",
                "verification_status": "UNKNOWN",
                "input_tokens": None,
                "output_tokens": None,
                "cost": None,
                "state": "STARTED",
            }
            call["effective_messages_digest"] = digest(
                [dict(message) for message in messages]
            )
            call["effective_system_prompt_digest"] = digest(messages[0]["content"])
            row.model_call = call
        reported = {}

        def observe(value):
            for source, target in (
                ("model", "reported_model"),
                ("model_revision", "reported_revision"),
                ("system_fingerprint", "system_fingerprint"),
                ("model_artifact_sha256", "reported_artifact_digest"),
                ("deployment_id", "reported_deployment_id"),
            ):
                if isinstance(value.get(source), str):
                    prior = reported.get(target)
                    if prior is not None and prior != value[source]:
                        raise ValueError("PROVIDER_IDENTITY_CHANGED")
                    reported[target] = value[source]

        token = provider_response_observer.set(observe)
        response = None
        try:
            response = await super().ainvoke(
                messages, run_context=run_context, **kwargs
            )
            return response
        finally:
            provider_response_observer.reset(token)
            call.update(reported)
            # reported_provider 只来自响应；resolved_provider 明示实际 transport 来源。
            if response is not None:
                call["dispatch_certainty"] = "PROVIDER_RESPONDED"
                call["state"] = "COMPLETED"
                if response.actual_usage is not None:
                    call["input_tokens"] = response.actual_usage.input_tokens
                    call["output_tokens"] = response.actual_usage.output_tokens
                if (
                    call["reported_model"] == call["requested_model"]
                    and bool((call["reported_revision"] or "").strip())
                    and (
                        call["requested_revision"] is None
                        or call["reported_revision"] == call["requested_revision"]
                    )
                ):
                    call["actual_revision"] = call["reported_revision"]
                    call["verification_status"] = "VERIFIED_BY_PROVIDER_RESPONSE"
            else:
                call["state"] = "UNKNOWN"
            receipt = {
                "actual_subject_manifest": manifest,
                "resolved_toolset_identity": manifest["tool_profile_digest"],
                "run_id": run_id,
                "role": row.role,
                "model_call_receipts": [call],
            }
            receipt["receipt_digest"] = digest(receipt)
            call["verification_status"] = project_receipt(manifest, receipt)[
                "identity_level"
            ]
            async with self.service.database.transaction() as session:
                row = await session.get(TriageRunRow, run_id, with_for_update=True)
                row.model_call = call


class EvidenceLookupAdapter(BusinessToolAdapter):
    def __init__(self, definition, service, bridge):
        super().__init__(definition)
        self.service, self.bridge = service, bridge

    def invoke_once(self, invocation, context):
        from core.runtime.tool_adapters import ToolAdapterResponse

        context.raise_if_cancelled()
        result = self.bridge.run(
            lambda: self.service.read_evidence(
                context.run_context.run_id,
                invocation.invocation_id,
                dict(invocation.arguments),
            ),
            operation="stage13_evidence_lookup",
            timeout_seconds=min(10, context.remaining_seconds()),
        )
        context.raise_if_cancelled()
        return ToolAdapterResponse(
            content=json.dumps(result, ensure_ascii=False),
            content_type="application/json",
            safe_summary="授权证据已读取",
        )


async def compose(database, engine, model_config, scope):
    """应用级资源有明确关闭 owner；不导入 Fake / 自建 Agent runner。"""
    from core.stage13.triage_execution import TriageExecutionService

    bridge = SyncPersistenceBridge(asyncio.get_running_loop())
    service = TriageExecutionService(database, scope)
    agents = definitions()
    tool_definition = BusinessToolDefinition(
        name="stage13_evidence_lookup",
        description="读取本次 Run 授权的 Evidence（只读）",
        input_schema={
            "type": "object",
            "properties": {"evidence_id": {"type": "string"}},
            "required": ["evidence_id"],
            "additionalProperties": False,
        },
        handler=lambda a, c: None,
        handler_binding_id="stage13.authorized_evidence.v1",
        granted_agent_ids=frozenset(d.agent_id for d in agents),
        side_effect_kind="READ_ONLY",
        idempotency="IDEMPOTENT",
    )
    tools = ToolRegistry()
    tools.register(
        ToolRegistration(
            ToolDescriptor(tool_definition.name, tool_definition.description),
            EvidenceLookupAdapter(tool_definition, service, bridge),
        )
    )
    bundle = AgentRegistrationBundle(
        registrations=tuple(AgentRegistration(d) for d in agents),
        tool_grants={tool_definition.name: tool_definition.granted_agent_ids},
        model_profile_ids=frozenset({"remote_advanced"}),
    )
    tools.freeze()
    catalog = compile_agent_catalog(
        bundle,
        actual_model_profile_ids=frozenset({"remote_advanced"}),
        actual_tool_names=tools.registered_names,
        actual_tool_registrations={r.descriptor.name: r for r in tools.registrations()},
    )
    policies = ToolPolicyCatalog(
        tool_registry=tools, agent_registry=catalog.agent_registry
    )
    policies.register(
        ToolPolicy(
            tool_name=tool_definition.name,
            allowed_agent_ids=tool_definition.granted_agent_ids,
        )
    )
    policies.freeze()
    extractors = ToolResourceExtractorCatalog()
    extractors.freeze()
    governance = ToolGovernanceService(policies, catalog.agent_registry)
    authorization = ResourceAuthorizationService(
        FilesystemResourcePolicy(()), extractors
    )
    control = DurableRunControlService(database)
    approval = DurableApprovalService(database)
    invocations = DurableToolInvocationService(database)
    from core.runtime.tool_execution import ToolExecutionService

    tool_execution = ToolExecutionService(durable_invocation_service=invocations)
    executor = BoundedBlockingExecutor(max_workers=8, max_pending_tasks=32)
    steps = BoundedBlockingExecutor(
        max_workers=32, max_pending_tasks=32, thread_name_prefix="stage13-step"
    )
    profile = ModelProfile(
        ModelProfileId.REMOTE_ADVANCED,
        model_config["context_window"],
        model_config["max_tokens"],
        True,
        True,
        False,
        False,
        2,
        2,
        ModelCostProfile(ModelProfileId.REMOTE_ADVANCED, True),
        True,
        "stage13-only",
        supports_native_tool_calling=True,
        provider_kind=model_config["provider"],
        model_identity=model_config["model"],
    )
    invocation_router = ModelInvocationRouter(
        retry_executor=RetryExecutor(RetryPolicy(max_attempts=1))
    )
    router = AgentRouter(
        engine,
        PostgresMemoryManagerBridge(PostgresMemoryManager(database), bridge),
        agent_registry=catalog.agent_registry,
        max_tokens=model_config["max_tokens"],
        model_context_window=model_config["context_window"],
        model_profiles=(profile,),
        model_resolver=ModelResolver({profile.profile_id: engine}),
        model_adapter_resolver=ModelAdapterResolver(
            {profile.profile_id: TriageModelAdapter(engine, service, model_config)}
        ),
        model_invocation_router=invocation_router,
        blocking_executor=executor,
        tool_registry=tools,
        tool_governance_service=governance,
        resource_authorization_service=authorization,
        tool_execution_service=tool_execution,
        tool_snapshot_store=PostgresToolResolutionSnapshotStore(database),
    )
    metrics, logger, spans = (
        NoopMetricsRecorder(),
        NoopStructuredRuntimeLogger(),
        NoopSpanRecorder(),
    )
    dispatcher = RuntimeObservabilityDispatcher(
        logger_projector=StructuredLogProjector(logger),
        metrics_projector=RuntimeMetricsProjector(metrics),
        logger_checkpoint_store=InMemoryEventConsumptionCheckpointStore(),
        metrics_checkpoint_store=InMemoryEventConsumptionCheckpointStore(),
    )
    journal = PostgresRunEventJournal(database)
    services = ApplicationRuntimeServices(
        event_journal=journal,
        observability_dispatcher=dispatcher,
        structured_logger=logger,
        runtime_metrics_recorder=metrics,
        span_recorder=spans,
        snapshot_store=None,
        recovery_validator=None,
        model_invocation_router=invocation_router,
        tool_execution_service=tool_execution,
        retrieval_execution_service=None,
        blocking_executors=(executor, steps),
        worker_trackers=(tool_execution.concurrency_controller,),
        run_registry=RunRegistry(),
        durable_run_control=control,
        durable_approval=approval,
        durable_tool_invocation=invocations,
        run_control_owner_id="stage13-" + uuid4().hex,
        coordinated_step_executor=steps,
    )
    repository = DurableExecutionRepository(database, control)
    factory = CoordinatedRuntimeFactory(
        router,
        services,
        agent_registry=catalog.agent_registry,
        execution_repository=repository,
        step_result_per_result_chars=65536,
        step_result_run_total_chars=131072,
    )
    chat = ChatService(
        router,
        coordinated_runtime_factory=factory,
        event_journal=journal,
        observability_dispatcher=dispatcher,
        gauge_provider=dispatcher.gauge_provider,
        run_registry=services.run_registry,
        admission_gate=services.admission_gate,
    )
    service.application = AgentApplicationService(chat, catalog.agent_registry)
    service.factory, service.repository, service.services = (
        factory,
        repository,
        services,
    )
    service.manifests = {
        a.agent_id: manifest_for(
            catalog.agent_registry.resolve(a.agent_id),
            model_config,
            router._build_system_prompt(a.agent_id, business_definition=a),
        )
        for a in agents
    }
    from core.stage13.triage_admission import RealSubjectAdmissionService

    service.admission = RealSubjectAdmissionService(database, scope, service.manifests)
    await service.admission.register_subjects()
    return service
