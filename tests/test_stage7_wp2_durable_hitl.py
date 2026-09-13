"""Stage7-WP2 real PostgreSQL durable HITL evidence."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import threading
import uuid
from types import SimpleNamespace

from fastapi.testclient import TestClient
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

import pytest
from sqlalchemy import func, select

from core.runtime.approval import (
    AgentStateApprovalBridge,
    ApprovalDecisionValue,
    ApprovalRequest,
    ApprovalStatus,
    ToolApprovalController,
    ToolApprovalRejectedError,
    compute_invocation_binding_digest,
)
from core.runtime.durable_approval import DurableApprovalService
from core.runtime.run_control import DurableRunControlService, OwnershipLost
from core.persistence.models import DurableToolExecutionClaimRow
from core.auth import AuthService, AuthorizationService
from core.redis_service import RedisTokenBucketRateLimiter
from core.runtime import (
    AgentState,
    AgentStateMachine,
    BudgetLedger,
    RunBudget,
    RunEventEmitter,
    RuntimeEventChannel,
    RuntimeEventType,
    StepStatus,
    create_run_context,
)
from core.runtime.event_journal_store import InMemoryRunEventJournal
from core.runtime.tool_contract import ToolInvocation, safe_key_digest
import server
from tests.test_stage6_wp2_ownership import _create_user, _token
from tests.test_tool_approval_router_integration import _make_router, _tool_args
from tests.test_tool_governance import production_registry, production_service


def _request(run_id: str, *, approval_id: str | None = None, binding: str | None = None) -> ApprovalRequest:
    digest = "a" * 64
    return ApprovalRequest(
        approval_id or uuid.uuid4().hex,
        run_id,
        "step-1",
        uuid.uuid4().hex,
        "safe-test-tool",
        digest,
        "b" * 64,
        "c" * 64,
        None,
        "HIGH",
        ("RESOURCE_WRITE",),
        binding or "d" * 64,
        datetime.now(UTC),
    )


async def _run_and_pending(database, *, lease_seconds: int = 30):
    run = DurableRunControlService(database, lease_seconds=lease_seconds)
    lease = await run.claim(uuid.uuid4().hex, "instance-a")
    request = _request(lease.run_id)
    service_a = DurableApprovalService(database)
    await service_a.create(request)
    return run, lease, request, service_a, DurableApprovalService(database)


class _ProductionApprovalHarness:
    def __init__(self, database, lease) -> None:
        self.loop = asyncio.get_running_loop()
        self.context, self.source = create_run_context(
            entry_agent_id="core_router", timeout_seconds=30,
            run_id=lease.run_id,
        )
        self.context.attach_budget_ledger(
            BudgetLedger(
                RunBudget(max_tool_calls=4, max_retries=2),
                deadline_remaining=self.context.remaining_seconds,
            )
        )
        self.journal = InMemoryRunEventJournal()
        self.channel = RuntimeEventChannel(
            64,
            run_id=self.context.run_id,
            cancellation_token=self.context.cancellation_token,
            journal=self.journal,
        )
        self.run_emitter = RunEventEmitter(
            run_id=self.context.run_id,
            trace_id=self.context.trace_id,
            channel=self.channel,
        )
        self.step_emitter = self.run_emitter.for_step("step")
        self.state = AgentState.for_run_context(self.context.run_id)
        self.machine = AgentStateMachine()
        self.state.mark_running()
        self.state.add_step("step", "tool step")
        self.state.start_step("step")
        self.service = DurableApprovalService(database)
        self.controller = ToolApprovalController(
            run_id=self.context.run_id,
            run_context=self.context,
            state_bridge=AgentStateApprovalBridge(self.machine, self.state),
            deadline_check=self.context.remaining_seconds,
            loop=self.loop,
            durable_service=self.service,
            durable_lease=lease,
        )
        self.controller.bind_step_emitter_resolver(
            lambda step_id: self.run_emitter.for_step(step_id)
        )

    async def close(self) -> None:
        self.source.cancel()
        self.controller.close()
        await self.channel.close()

    @property
    def events(self):
        return self.journal.read_after(self.context.run_id, 0, 1000)


async def _wait_for(predicate, timeout: float = 10.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        result = predicate()
        if result:
            return result
        await asyncio.sleep(0.02)
    return predicate()


async def _authenticated_http_setup(database, monkeypatch, run_id: str):
    user_id = await _create_user(database, ("USER",))
    await AuthorizationService(database).bind_new(
        SimpleNamespace(user_id=user_id, roles=frozenset({"USER"})),
        "RUN",
        run_id,
    )
    private = Ed25519PrivateKey.generate()
    settings = SimpleNamespace(
        jwt_public_key=private.public_key().public_bytes(
            Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
        ),
        jwt_issuer="test-issuer",
        jwt_audience="test-api",
        jwt_allowed_algorithm="EdDSA",
        jwt_clock_skew_seconds=0,
    )
    # HTTP 命令使用不同 service 实例，模拟命中另一 backend。
    remote_service = DurableApprovalService(database)
    monkeypatch.setattr(
        server,
        "chat_service",
        SimpleNamespace(
            run_registry=SimpleNamespace(),
            _coordinated_runtime_factory=SimpleNamespace(
                services=SimpleNamespace(durable_approval=remote_service)
            ),
        ),
    )
    monkeypatch.setattr(
        server.app.state,
        "auth_service",
        AuthService(database, settings),
        raising=False,
    )
    monkeypatch.setattr(
        server.app.state,
        "authorization_service",
        AuthorizationService(database),
        raising=False,
    )
    monkeypatch.setattr(
        server.app.state,
        "rate_limiter",
        RedisTokenBucketRateLimiter(
            SimpleNamespace(),
            SimpleNamespace(
                rate_limit_enabled=False,
                rate_limit_capacity=1,
                rate_limit_refill_rate=1.0,
            ),
        ),
        raising=False,
    )
    return {"Authorization": f"Bearer {_token(private, user_id, ['USER'])}"}


@pytest.mark.asyncio
async def test_durable_pending_and_cross_instance_decisions(clean_database):
    _run, _lease, request, service_a, service_b = await _run_and_pending(clean_database)
    assert (await service_b.get(request.approval_id)).approval_id == request.approval_id
    approved = await service_b.decide(
        run_id=request.run_id, approval_id=request.approval_id,
        invocation_binding_digest=request.invocation_binding_digest,
        decision=ApprovalDecisionValue.APPROVE,
    )
    assert approved.effective_status is ApprovalStatus.APPROVED
    assert (await service_a.status(request.approval_id)) is ApprovalStatus.APPROVED


@pytest.mark.asyncio
async def test_reject_first_wins_and_duplicate_is_idempotent(clean_database):
    _run, _lease, request, service_a, service_b = await _run_and_pending(clean_database)
    first = await service_a.decide(
        run_id=request.run_id, approval_id=request.approval_id,
        invocation_binding_digest=request.invocation_binding_digest,
        decision=ApprovalDecisionValue.REJECT,
    )
    duplicate = await service_b.decide(
        run_id=request.run_id, approval_id=request.approval_id,
        invocation_binding_digest=request.invocation_binding_digest,
        decision=ApprovalDecisionValue.REJECT,
    )
    conflict = await service_b.decide(
        run_id=request.run_id, approval_id=request.approval_id,
        invocation_binding_digest=request.invocation_binding_digest,
        decision=ApprovalDecisionValue.APPROVE,
    )
    assert first.effective_status is duplicate.effective_status is ApprovalStatus.REJECTED
    assert duplicate.idempotent is True
    assert conflict.safe_error_code == "APPROVAL_DECISION_CONFLICT"
    assert (await service_a.status(request.approval_id)) is ApprovalStatus.REJECTED


@pytest.mark.asyncio
async def test_decision_race_and_binding_mismatch_fail_closed(clean_database):
    _run, _lease, request, service_a, service_b = await _run_and_pending(clean_database)
    results = await asyncio.gather(
        service_a.decide(run_id=request.run_id, approval_id=request.approval_id,
                         invocation_binding_digest=request.invocation_binding_digest,
                         decision=ApprovalDecisionValue.APPROVE),
        service_b.decide(run_id=request.run_id, approval_id=request.approval_id,
                         invocation_binding_digest=request.invocation_binding_digest,
                         decision=ApprovalDecisionValue.REJECT),
    )
    assert sum(item.effective_status is ApprovalStatus.APPROVED and item.safe_error_code is None for item in results) == 1, repr(results)
    assert sum(item.safe_error_code in {"APPROVAL_DECISION_CONFLICT", None} for item in results) == 2
    assert (await service_a.status(request.approval_id)) is ApprovalStatus.APPROVED

    mismatch = await service_b.decide(
        run_id=request.run_id, approval_id=request.approval_id,
        invocation_binding_digest="e" * 64,
        decision=ApprovalDecisionValue.REJECT,
    )
    assert mismatch.safe_error_code == "APPROVAL_BINDING_MISMATCH"


@pytest.mark.asyncio
async def test_fencing_and_single_durable_claim(clean_database):
    run = DurableRunControlService(clean_database, lease_seconds=1)
    old = await run.claim(uuid.uuid4().hex, "instance-a")
    request = _request(old.run_id)
    service = DurableApprovalService(clean_database)
    await service.create(request)
    await service.decide(run_id=request.run_id, approval_id=request.approval_id,
                         invocation_binding_digest=request.invocation_binding_digest,
                         decision=ApprovalDecisionValue.APPROVE)
    await asyncio.sleep(1.1)
    current = await DurableRunControlService(clean_database, lease_seconds=1).claim(request.run_id, "instance-b")
    with pytest.raises(OwnershipLost):
        await service.claim_execution(lease=old, approval_id=request.approval_id,
                                      invocation_binding_digest=request.invocation_binding_digest)
    claim = await service.claim_execution(lease=current, approval_id=request.approval_id,
                                          invocation_binding_digest=request.invocation_binding_digest)
    assert claim.owner_id == "instance-b"
    with pytest.raises(ValueError, match="already exists"):
        await service.claim_execution(lease=current, approval_id=request.approval_id,
                                      invocation_binding_digest=request.invocation_binding_digest)


@pytest.mark.asyncio
async def test_invalidation_cancel_deadline_terminal_cannot_reactivate(clean_database):
    for reason, expected in (
        ("CANCELLED", ApprovalStatus.INVALIDATED_CANCELLED),
        ("DEADLINE_EXCEEDED", ApprovalStatus.INVALIDATED_TIMEOUT),
        ("RUN_TERMINAL", ApprovalStatus.INVALIDATED_RUN_TERMINAL),
    ):
        _run, _lease, request, service_a, service_b = await _run_and_pending(clean_database)
        result = (await service_a.invalidate_run(request.run_id, reason))[0]
        assert result.effective_status is expected
        assert (await service_b.status(request.approval_id)) is expected
        late = await service_b.decide(
            run_id=request.run_id, approval_id=request.approval_id,
            invocation_binding_digest=request.invocation_binding_digest,
            decision=ApprovalDecisionValue.APPROVE,
        )
        assert late.effective_status is expected
        assert late.safe_error_code == "APPROVAL_INVALIDATED"


@pytest.mark.asyncio
async def test_approved_before_execution_survives_controller_loss(clean_database):
    run, lease, request, service_a, service_b = await _run_and_pending(clean_database)
    await service_a.decide(run_id=request.run_id, approval_id=request.approval_id,
                           invocation_binding_digest=request.invocation_binding_digest,
                           decision=ApprovalDecisionValue.APPROVE)
    del service_a
    recovered = await service_b.get(request.approval_id)
    assert recovered is not None
    claim = await service_b.claim_execution(
        lease=lease, approval_id=recovered.approval_id,
        invocation_binding_digest=recovered.invocation_binding_digest,
    )
    assert claim.approval_id == request.approval_id


@pytest.mark.asyncio
async def test_authenticated_http_approve_reaches_durable_service(clean_database, monkeypatch):
    """真实 FastAPI auth/ownership middleware 到 durable approval 的 production seam。"""
    user_id = await _create_user(clean_database, ("USER",))
    run_id = str(uuid.uuid4())
    await AuthorizationService(clean_database).bind_new(
        SimpleNamespace(user_id=user_id, roles=frozenset({"USER"})), "RUN", run_id
    )
    private = Ed25519PrivateKey.generate()
    settings = SimpleNamespace(
        jwt_public_key=private.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo),
        jwt_issuer="test-issuer", jwt_audience="test-api",
        jwt_allowed_algorithm="EdDSA", jwt_clock_skew_seconds=0,
    )
    control = DurableRunControlService(clean_database)
    await control.claim(run_id, "instance-a")
    approval = DurableApprovalService(clean_database)
    request = _request(run_id)
    await approval.create(request)
    monkeypatch.setattr(server, "chat_service", SimpleNamespace(
        run_registry=SimpleNamespace(),
        _coordinated_runtime_factory=SimpleNamespace(
            services=SimpleNamespace(durable_approval=approval)
        ),
    ))
    monkeypatch.setattr(server.app.state, "auth_service", AuthService(clean_database, settings), raising=False)
    monkeypatch.setattr(server.app.state, "authorization_service", AuthorizationService(clean_database), raising=False)
    monkeypatch.setattr(server.app.state, "rate_limiter", RedisTokenBucketRateLimiter(SimpleNamespace(), SimpleNamespace(rate_limit_enabled=False, rate_limit_capacity=1, rate_limit_refill_rate=1.0)), raising=False)
    response = TestClient(server.app).post(
        f"/api/runtime/runs/{run_id}/tool-approvals/{request.approval_id}/approve",
        json={"invocation_binding_digest": request.invocation_binding_digest},
        headers={"Authorization": f"Bearer {_token(private, user_id, ['USER'])}"},
    )
    assert response.status_code == 200
    assert response.json()["effective_status"] == "APPROVED"
    assert await approval.status(request.approval_id) is ApprovalStatus.APPROVED


@pytest.mark.asyncio
async def test_controller_reentry_uses_canonical_durable_approval(clean_database):
    """Recovery 重入不得发布或等待新生成的非 canonical ID。"""
    control = DurableRunControlService(clean_database)
    lease = await control.claim(str(uuid.uuid4()), "instance-a")
    harness = _ProductionApprovalHarness(clean_database, lease)
    invocation = ToolInvocation.create(
        tool_name="complex_workflow_simulator",
        arguments=json.loads(_tool_args("wp2-canonical")),
    )
    binding = compute_invocation_binding_digest(
        invocation_identity_digest=safe_key_digest(invocation.invocation_id),
        tool_name=invocation.tool_name,
        arguments_digest=invocation.arguments_digest,
        idempotency_key_digest=safe_key_digest(invocation.idempotency_key),
        resource_key_digest=safe_key_digest(invocation.resource_key),
        risk_level="HIGH",
        risk_facts=("NON_IDEMPOTENT",),
    )
    canonical = ApprovalRequest(
        uuid.uuid4().hex,
        lease.run_id,
        "step",
        invocation.invocation_id,
        invocation.tool_name,
        safe_key_digest(invocation.invocation_id),
        invocation.arguments_digest,
        safe_key_digest(invocation.idempotency_key),
        safe_key_digest(invocation.resource_key),
        "HIGH",
        ("NON_IDEMPOTENT",),
        binding,
        datetime.now(UTC),
    )
    await harness.service.create(canonical)
    canonical = await harness.service.get(canonical.approval_id)
    assert canonical is not None
    result_holder = []

    def request_from_worker():
        result_holder.append(
            harness.controller.request_approval(
                step_id="step",
                invocation=invocation,
                tool_name=invocation.tool_name,
                risk_level="HIGH",
                risk_facts=("NON_IDEMPOTENT",),
                event_emitter=harness.step_emitter,
            )
        )

    thread = threading.Thread(target=request_from_worker)
    try:
        thread.start()
        assert await _wait_for(lambda: not thread.is_alive())
        assert result_holder[0].approval_id == canonical.approval_id
        requested = [
            event
            for event in harness.events
            if event.event_type is RuntimeEventType.TOOL_APPROVAL_REQUESTED
        ]
        assert [event.safe_payload["approval_id"] for event in requested] == [
            canonical.approval_id
        ]
        assert harness.controller.get(canonical.approval_id) == canonical
        assert harness.state.steps["step"].status is StepStatus.WAITING_FOR_APPROVAL
    finally:
        await harness.close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    ("decision_path", "expected_status", "expected_execution"),
    (("approve", "APPROVED", 1), ("reject", "REJECTED", 0)),
)
@pytest.mark.asyncio
async def test_cross_instance_authenticated_http_continues_production_tool_chain(
    clean_database,
    monkeypatch,
    decision_path,
    expected_status,
    expected_execution,
):
    """Authenticated HTTP 跨实例决策能唤醒原 production worker。"""
    control = DurableRunControlService(clean_database)
    lease = await control.claim(str(uuid.uuid4()), "instance-a")
    harness = _ProductionApprovalHarness(clean_database, lease)
    registry = production_registry()
    governance = production_service(registry)
    adapter = registry.require("complex_workflow_simulator").adapter
    router = _make_router(
        registry,
        governance,
        "complex_workflow_simulator",
        _tool_args(f"wp2-http-{decision_path}"),
    )
    headers = await _authenticated_http_setup(
        clean_database, monkeypatch, lease.run_id
    )
    worker_errors = []
    worker_results = []

    def worker():
        try:
            worker_results.append(
                router._prepare_answer_messages(
                    "core_router",
                    "query",
                    run_context=harness.context,
                    event_emitter=harness.step_emitter,
                    approval_controller=harness.controller,
                )
            )
        except Exception as exc:
            worker_errors.append(exc)

    thread = threading.Thread(target=worker)
    try:
        thread.start()
        assert await _wait_for(
            lambda: harness.state.steps["step"].status
            is StepStatus.WAITING_FOR_APPROVAL
        )
        requested = next(
            event
            for event in harness.events
            if event.event_type is RuntimeEventType.TOOL_APPROVAL_REQUESTED
        )
        approval_id = requested.safe_payload["approval_id"]
        request = harness.controller.get(approval_id)
        response = TestClient(server.app).post(
            f"/api/runtime/runs/{lease.run_id}/tool-approvals/"
            f"{approval_id}/{decision_path}",
            json={"invocation_binding_digest": request.invocation_binding_digest},
            headers=headers,
        )
        assert response.status_code == 200
        assert response.json()["effective_status"] == expected_status
        assert await _wait_for(lambda: not thread.is_alive())

        started = [
            event
            for event in harness.events
            if event.event_type is RuntimeEventType.TOOL_STARTED
        ]
        assert len(started) == expected_execution
        assert len(adapter._state_store.committed_operations) == expected_execution
        async with clean_database.session() as session:
            claim_count = (
                await session.execute(
                    select(func.count())
                    .select_from(DurableToolExecutionClaimRow)
                    .where(DurableToolExecutionClaimRow.approval_id == approval_id)
                )
            ).scalar_one()
        assert claim_count == expected_execution
        if decision_path == "approve":
            assert worker_errors == []
            assert len(worker_results) == 1
        else:
            assert len(worker_errors) == 1
            assert isinstance(worker_errors[0], ToolApprovalRejectedError)
            assert worker_results == []
    finally:
        await harness.close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    ("reason", "expected_status"),
    (
        ("CANCELLED", ApprovalStatus.INVALIDATED_CANCELLED),
        ("DEADLINE_EXCEEDED", ApprovalStatus.INVALIDATED_TIMEOUT),
        ("RUN_TERMINAL", ApprovalStatus.INVALIDATED_RUN_TERMINAL),
    ),
)
@pytest.mark.asyncio
async def test_cross_instance_invalidation_wakes_durable_waiter(
    clean_database, reason, expected_status
):
    control = DurableRunControlService(clean_database)
    lease = await control.claim(str(uuid.uuid4()), "instance-a")
    harness = _ProductionApprovalHarness(clean_database, lease)
    invocation = ToolInvocation.create(
        tool_name="complex_workflow_simulator",
        arguments=json.loads(_tool_args(f"wp2-invalidate-{reason}")),
    )
    try:
        request = await asyncio.to_thread(
            lambda: harness.controller.request_approval(
                step_id="step",
                invocation=invocation,
                tool_name=invocation.tool_name,
                risk_level="HIGH",
                risk_facts=("NON_IDEMPOTENT",),
                event_emitter=harness.step_emitter,
            )
        )
        waiter = asyncio.create_task(
            asyncio.to_thread(
                harness.controller.wait_for_decision,
                approval_id=request.approval_id,
            )
        )
        await DurableApprovalService(clean_database).invalidate_run(
            lease.run_id, reason
        )
        result = await asyncio.wait_for(waiter, timeout=5)
        assert result.effective_status is expected_status
        assert harness.state.steps["step"].status is StepStatus.CANCELLED
    finally:
        await harness.close()
