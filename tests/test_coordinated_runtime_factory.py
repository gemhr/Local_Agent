from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from core.runtime import (
    CoordinatedRuntimeFactory,
    EventChannelState,
    FaultInjectionController,
)
from tests._runtime_assembly_fixtures import FakeRouter, make_services
from tests._recovery_fixtures import recovery_plan
from core.runtime.execution_aggregate import plan_payload
from core.runtime.plan_fingerprint import PlanFingerprinter
from core.agent_platform.contracts import AgentDefinition, AgentRegistration
from core.agent_platform.registry import AgentRegistrationBundle, compile_agent_catalog
from core.runtime.agent_registry import DEFAULT_AGENT_REGISTRY
from core.runtime.tracing import InMemorySpanRecorder


def compiled_test_registry(*, router_model_profile="default"):
    # Factory 的真实必填目录：builtin seeds 加 recovery fixture 使用的 router Agent。
    router = AgentDefinition(
        agent_id="router", agent_version="test-1", display_name="Router",
        role="router", instructions="test router", model_profile_id=router_model_profile,
    )
    return compile_agent_catalog(
        AgentRegistrationBundle(registrations=(AgentRegistration(router),)),
        builtin_registrations=tuple(
            DEFAULT_AGENT_REGISTRY.resolve(agent_id)
            for agent_id in DEFAULT_AGENT_REGISTRY.agent_ids
        ),
        actual_model_profile_ids=frozenset({"default", router_model_profile}),
    ).agent_registry


def durable_identity_fields(agent_id="router", *, registry=None):
    registration = (registry or compiled_test_registry()).resolve(agent_id)
    definition = registration.definition
    return {
        "resolved_agent_id": agent_id,
        "toolset_identity": registration.toolset_identity,
        "resolved_model_profile_id": definition.model_profile_id,
        "resolved_retrieval_profile_id": definition.retrieval_profile_id or "NONE",
        "resolved_memory_profile_id": definition.memory_profile_id or "NONE",
        "performer_identities": {agent_id: registration.binding_identity()},
    }


def factory_for(router, services, **kwargs):
    return CoordinatedRuntimeFactory(
        router, services,
        agent_registry=kwargs.pop("agent_registry", compiled_test_registry()),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_factory_creates_isolated_single_identity_run_scopes() -> None:
    services = make_services()
    factory = factory_for(FakeRouter(), services)

    first = await factory.create_run_scope("core_router", "question")
    second = await factory.create_run_scope("core_router", "question")

    assert first.run_context.run_id == first.agent_state.run_id
    assert first.event_channel.run_id == first.run_context.run_id
    assert first.event_emitter.channel is first.event_channel
    assert first.coordinator.event_emitter is first.event_emitter
    assert first.coordinator.activity_tracker is first.run_context.activity_tracker
    assert first.cancellation_source.token is first.run_context.cancellation_token
    assert first.checkpoint_coordinator is None
    assert first.plan is None
    assert first.scheduler is None
    assert first.run_context.data.resolved_agent_id == "core_router"
    assert first.run_context.data.resolved_model_profile_id == "default"
    assert first.run_context.data.resolved_retrieval_profile_id == "NONE"
    assert first.run_context.data.resolved_memory_profile_id == "NONE"
    assert len(first.run_context.data.toolset_identity) == 64
    assert first.run_context.run_id != second.run_context.run_id
    assert first.agent_state is not second.agent_state
    assert first.event_channel is not second.event_channel

    await first.close()
    await first.close()
    await second.close()
    assert first.event_channel.state is EventChannelState.CLOSED


@pytest.mark.asyncio
async def test_run_context_root_trace_and_resume_capture_resolved_identity():
    recorder = InMemorySpanRecorder()
    services = make_services(span_recorder=recorder)
    repository = SimpleNamespace(initialized=None)

    class RecordingRepository:
        async def initialize(self, root, *, lease):
            repository.initialized = root

        async def start_step(self, lease, *, step_id, plan_version, journal_append=None):
            return 1

        async def complete_step(self, lease, **kwargs):
            return True

        async def finalize_terminal(self, lease, *, event, journal, **kwargs):
            return None

    registry = compiled_test_registry()
    registration = registry.resolve("router")
    scope = await CoordinatedRuntimeFactory(
        FakeRouter(), services, agent_registry=registry,
        execution_repository=RecordingRepository(),
    ).create_run_scope("router", "identity", agent_version="test-1")
    try:
        assert scope.run_context.data.toolset_identity == registration.toolset_identity
        result = await scope.execute()
        assert result.status.value == "SUCCEEDED"
        assert repository.initialized.resume_input["toolset_identity"] == registration.toolset_identity
        assert repository.initialized.resume_input["resolved_model_profile_id"] == "default"
        assert repository.initialized.resume_input["performer_versions"] == {"router": "test-1"}
        assert repository.initialized.resume_input["performer_identities"] == {
            "router": registration.binding_identity()
        }
        with pytest.raises(TypeError):
            scope.run_context.data.performer_binding_identities["router"]["agent_version"] = "mutated"
        assert dict(scope.run_context.data.performer_binding_versions) == {"router": "test-1"}
        root = next(span for span in recorder.snapshot() if span.operation == "runtime.run")
        assert root.attributes["resolved_agent_id"] == "router"
        assert root.attributes["toolset_identity"] == registration.toolset_identity
        assert root.attributes["resolved_model_profile_id"] == "default"
    finally:
        await scope.close()


@pytest.mark.asyncio
async def test_factory_binds_compiled_structured_schema_to_business_delivery_feed():
    from dataclasses import replace
    from core.agent_platform.contracts import AgentDefinition, AgentRegistration
    from core.agent_platform.registry import AgentRegistrationBundle, compile_agent_catalog
    from core.runtime.client_event_feed import InMemoryClientEventFeed

    schema = {"type": "object", "required": ["answer"]}
    definition = AgentDefinition(
        agent_id="structured_helper", agent_version="1", display_name="Structured",
        role="structured output", instructions="Return the declared schema.",
        output_schema=schema,
    )
    registry = compile_agent_catalog(
        AgentRegistrationBundle(registrations=(AgentRegistration(definition),)),
        builtin_registrations=tuple(
            DEFAULT_AGENT_REGISTRY.resolve(agent_id)
            for agent_id in DEFAULT_AGENT_REGISTRY.agent_ids
        ),
    ).agent_registry
    feed = InMemoryClientEventFeed()
    services = replace(make_services(), client_event_feed=feed)
    factory = CoordinatedRuntimeFactory(
        FakeRouter(), services, agent_registry=registry,
    )
    run_scope = await factory.create_run_scope(
        "structured_helper", "question", agent_version="1"
    )
    try:
        assert feed._structured_schemas[run_scope.run_id] == definition.output_schema
    finally:
        await run_scope.close()


@pytest.mark.asyncio
async def test_factory_does_not_cache_request_scope_or_auto_checkpoint() -> None:
    services = make_services()
    factory = factory_for(FakeRouter(), services)
    scope = await factory.create("core_router", "question")

    assert not hasattr(services, "run_context")
    assert not hasattr(services, "agent_state")
    assert not hasattr(services, "event_channel")
    assert services.snapshot_store.list_for_run(scope.run_id, 10) == ()

    await scope.close()


@pytest.mark.asyncio
async def test_factory_transports_fault_controller_only_on_the_selected_run() -> None:
    services = make_services()
    factory = factory_for(FakeRouter(), services)
    controller = FaultInjectionController.disabled()

    selected = await factory.create_run_scope(
        "core_router",
        "question",
        fault_controller=controller,
    )
    ordinary = await factory.create_run_scope("core_router", "question")

    assert selected.fault_controller is controller
    assert selected.driver._fault_controller is controller
    assert ordinary.fault_controller is None
    assert ordinary.driver._fault_controller is None
    assert not hasattr(services, "fault_controller")

    await selected.close()
    await ordinary.close()


@pytest.mark.asyncio
async def test_unexecuted_scope_can_be_aborted_safely() -> None:
    services = make_services()
    scope = await factory_for(FakeRouter(), services).create_run_scope("core_router", "question")

    await scope.close(abort=True)
    await scope.close(abort=True)

    assert scope.event_channel.state is EventChannelState.ABORTED
    assert scope.cancellation_source.token.is_cancelled() is False


@pytest.mark.asyncio
async def test_factory_failure_unregisters_request_channel(monkeypatch) -> None:
    services = make_services()
    dispatcher = services.observability_dispatcher

    class BrokenCoordinator:
        @classmethod
        def for_dynamic_resolver(cls, **kwargs):
            raise RuntimeError("constructor failed")

    monkeypatch.setattr(
        "core.runtime.runtime_factory.RunCoordinator",
        BrokenCoordinator,
    )
    factory = factory_for(FakeRouter(), services)

    with pytest.raises(RuntimeError, match="constructor failed"):
        await factory.create_run_scope("core_router", "question")

    assert dispatcher.gauge_provider.channels == set()


@pytest.mark.asyncio
async def test_factory_rehydrates_terminal_step_into_state_and_store() -> None:
    plan = recovery_plan()
    now = datetime.now(UTC)
    root = SimpleNamespace(
        run_id="recovered-run",
        plan_payload=plan_payload(plan),
        plan_fingerprint=PlanFingerprinter.fingerprint(plan),
        resume_input={"entry_agent_id": "router", "agent_version": "test-1", "performer_versions": {"router": "test-1"}, **durable_identity_fields(), "query": "resume"},
        status="ACTIVE",
        stop_reason=None,
        final_result_binding=None,
        absolute_deadline=now + timedelta(minutes=5),
        budget_totals={"max_model_calls": 4},
        budget_reserved={"model_calls": 1},
        budget_consumed={"model_calls": 1},
        created_at=now,
        updated_at=now,
    )
    row = SimpleNamespace(
        run_id="recovered-run",
        step_id="step",
        status="SUCCEEDED",
        typed_result_payload={
            "producer_agent_id": "router",
            "content_type": "TEXT",
            "content": "durable result",
            "complete": True,
        },
        safe_error=None,
        created_at=now,
        updated_at=now,
    )
    image = SimpleNamespace(root=root, steps=(row,), models=())
    services = make_services()
    scope = await factory_for(FakeRouter(), services).create_rehydrated_run_scope(
        image, lease=("recovered-run", "worker-b"), run_id="recovered-run"
    )
    try:
        assert scope.plan == plan
        assert scope.run_context.data.deadline_at == root.absolute_deadline
        assert scope.agent_state.steps["step"].status.value == "SUCCEEDED"
        assert scope.coordinator.step_result_store is not None
        assert scope.coordinator.step_result_store.has_readable("step")
        assert scope.coordinator.output_gate is not None
        assert scope.budget_ledger.snapshot().committed_usage.model_calls == 2
        scope.agent_state.mark_running()
        assert scope.scheduler.evaluate(plan, scope.agent_state).claimable_step_ids == ()
    finally:
        await scope.close()


@pytest.mark.asyncio
async def test_recovery_rejects_drifted_delegated_performer_version() -> None:
    from dataclasses import replace

    plan = recovery_plan()
    plan = replace(plan, steps=(replace(plan.steps[0], preferred_agent="code_expert"),))
    now = datetime.now(UTC)
    root = SimpleNamespace(
        run_id="recovered-run",
        plan_payload=plan_payload(plan),
        plan_fingerprint=PlanFingerprinter.fingerprint(plan),
        resume_input={
            "entry_agent_id": "router", "agent_version": "test-1",
            "performer_versions": {"router": "test-1", "code_expert": "drifted"},
            **durable_identity_fields(),
        },
    )
    services = make_services()
    with pytest.raises(ValueError, match="Recovery performer identity/version mismatch"):
        await factory_for(FakeRouter(), services).create_rehydrated_run_scope(
            SimpleNamespace(root=root, steps=(), models=()),
            lease=("recovered-run", "worker-b"), run_id="recovered-run",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("identity_key", "changed_value"),
    (("toolset_identity", "b" * 64), ("resolved_model_profile_id", "other-profile")),
)
async def test_recovery_rejects_toolset_or_provider_identity_drift(identity_key, changed_value):
    plan = recovery_plan()
    now = datetime.now(UTC)
    resume_input = {
        "entry_agent_id": "router", "agent_version": "test-1",
        "workflow_id": None, "workflow_version": None,
        "performer_versions": {"router": "test-1"},
        **durable_identity_fields(),
    }
    resume_input[identity_key] = changed_value
    root = SimpleNamespace(
        run_id="recovered-run", plan_payload=plan_payload(plan),
        plan_fingerprint=PlanFingerprinter.fingerprint(plan),
        resume_input=resume_input, created_at=now, absolute_deadline=None,
    )
    with pytest.raises(ValueError, match=f"Recovery {identity_key} mismatch"):
        await factory_for(FakeRouter(), make_services()).create_rehydrated_run_scope(
            SimpleNamespace(root=root, steps=(), models=()),
            lease=("recovered-run", "worker-b"), run_id="recovered-run",
        )


@pytest.mark.asyncio
async def test_recovery_rejects_changed_compiled_model_binding_with_same_agent_version():
    plan = recovery_plan()
    current_registry = compiled_test_registry(router_model_profile="alternate")
    resume_input = {
        "entry_agent_id": "router", "resolved_agent_id": "router",
        "agent_version": "test-1", "workflow_id": None, "workflow_version": None,
        "performer_versions": {"router": "test-1"},
        **durable_identity_fields(),
    }
    # Tool identity is kept current so the recovery check reaches the provider fact.
    resume_input["toolset_identity"] = durable_identity_fields(registry=current_registry)["toolset_identity"]
    root = SimpleNamespace(
        run_id="recovered-run", plan_payload=plan_payload(plan),
        plan_fingerprint=PlanFingerprinter.fingerprint(plan),
        resume_input=resume_input, created_at=datetime.now(UTC), absolute_deadline=None,
    )
    with pytest.raises(ValueError, match="Recovery resolved_model_profile_id mismatch"):
        await factory_for(
            FakeRouter(), make_services(), agent_registry=current_registry,
        ).create_rehydrated_run_scope(
            SimpleNamespace(root=root, steps=(), models=()),
            lease=("recovered-run", "worker-b"), run_id="recovered-run",
        )


@pytest.mark.asyncio
async def test_fresh_run_persists_plan_before_first_step_execution() -> None:
    calls: list[tuple[str, object]] = []

    class RecordingExecutionRepository:
        async def initialize(self, root, *, lease):
            calls.append(("initialize", root.plan))

        async def start_step(self, lease, *, step_id, plan_version, journal_append=None):
            assert calls and calls[0][0] == "initialize"
            calls.append(("start_step", step_id))
            return 1

        async def complete_step(self, lease, **kwargs):
            calls.append(("complete_step", kwargs["step_id"]))
            return True

        async def finalize_terminal(self, lease, *, event, journal, **kwargs):
            calls.append(("terminal", event.payload.status))
            return None

    repository = RecordingExecutionRepository()
    services = make_services(snapshot_enabled=False)
    scope = await factory_for(
        FakeRouter(), services, execution_repository=repository
    ).create_run_scope("core_router", "question")
    try:
        result = await scope.execute()
        assert result.status.value == "SUCCEEDED"
        assert [name for name, _ in calls] == [
            "initialize",
            "start_step",
            "complete_step",
            "terminal",
        ]
    finally:
        await scope.close()


@pytest.mark.asyncio
async def test_factory_rehydrates_approval_and_execution_claim_binding() -> None:
    plan = recovery_plan()
    now = datetime.now(UTC)
    root = SimpleNamespace(
        run_id="approval-recovery-run",
        plan_payload=plan_payload(plan),
        plan_fingerprint=PlanFingerprinter.fingerprint(plan),
        resume_input={"entry_agent_id": "router", "agent_version": "test-1", "performer_versions": {"router": "test-1"}, **durable_identity_fields(), "user_query": "resume"},
        status="ACTIVE",
        stop_reason=None,
        final_result_binding=None,
        absolute_deadline=now + timedelta(minutes=5),
        budget_totals={},
        budget_reserved={},
        budget_consumed={},
        created_at=now,
        updated_at=now,
    )
    step = SimpleNamespace(
        run_id=root.run_id,
        step_id="step",
        status="RUNNING",
        typed_result_payload=None,
        safe_error=None,
        created_at=now,
        updated_at=now,
    )
    approval = SimpleNamespace(
        run_id=root.run_id,
        step_id="step",
        approval_id="approval-1",
        invocation_id="invocation-1",
        tool_name="complex_workflow_simulator",
        arguments_digest="a" * 64,
        invocation_binding_digest="b" * 64,
    )
    claim = SimpleNamespace(
        run_id=root.run_id,
        approval_id="approval-1",
        claim_id="claim-1",
    )
    image = SimpleNamespace(
        root=root,
        steps=(step,),
        models=(),
        tool_invocations=(),
        approvals=(approval,),
        execution_claims=(claim,),
    )
    scope = await factory_for(
        FakeRouter(), make_services()
    ).create_rehydrated_run_scope(
        image, lease=(root.run_id, "worker-b"), run_id=root.run_id
    )
    try:
        binding = scope.run_context.durable_tool_invocation
        assert binding.invocation_id == "invocation-1"
        assert binding.approval_id == "approval-1"
        assert binding.execution_claim_id == "claim-1"
    finally:
        await scope.close()
