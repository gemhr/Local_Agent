from __future__ import annotations

import asyncio
import concurrent.futures
import time
from datetime import UTC, datetime

import pytest

from core.runtime import (
    CancellationReason,
    FaultAction,
    FaultInjectionController,
    FaultInjectionRecorder,
    FaultInjectionScope,
    FaultPlan,
    FaultPoint,
    FaultRule,
    FaultScope,
    FaultTrigger,
    InjectedFaultCode,
    ModelInvocationChainError,
    ModelProfileId,
    RetrievalExecutionService,
    RetrievalExecutionStatus,
    RunCancelledError,
)
from tests.test_model_fault_injection import invoke
from tests.test_model_invocation import InvocationFixture, LOCAL, RecordingAdapter, routing
from tests.test_retrieval_execution import (
    FakeRetrievalAdapter,
    make_context,
    make_invocation,
)

NOW = datetime(2026, 7, 31, tzinfo=UTC)


def plan(
    point: FaultPoint,
    *,
    action: FaultAction = FaultAction.RAISE_TYPED_ERROR,
    code: InjectedFaultCode | None = InjectedFaultCode.INJECTED_PERMANENT_FAILURE,
    delay_seconds: float | None = None,
) -> FaultPlan:
    return FaultPlan(
        "isolated-plan",
        (
            FaultRule(
                rule_id="isolated-rule",
                fault_point=point,
                action=action,
                trigger=FaultTrigger.FIRST_MATCH,
                scope=FaultScope.RUN_SCOPE,
                max_hits=1,
                component=(
                    "model"
                    if point.value.startswith("MODEL_")
                    else "retrieval"
                ),
                safe_fault_code=code,
                delay_seconds=delay_seconds,
            ),
        ),
        created_at=NOW,
    )


def test_concurrent_model_runs_do_not_share_controller_or_recorder() -> None:
    router = InvocationFixture(
        {ModelProfileId.LOCAL_FAST: RecordingAdapter(["unused"])}
    ).router
    recorder_a = FaultInjectionRecorder()
    recorder_b = FaultInjectionRecorder()
    controller_a = FaultInjectionController.for_test(
        plan(FaultPoint.MODEL_BEFORE_INVOCATION),
        recorder=recorder_a,
    )
    controller_b = FaultInjectionController.disabled()

    def run_a():
        adapter = RecordingAdapter(["secret-a"])
        fixture = InvocationFixture({ModelProfileId.LOCAL_FAST: adapter})
        fixture.router = router
        with pytest.raises(ModelInvocationChainError):
            invoke(fixture, routing(LOCAL), controller_a)
        return adapter.calls

    def run_b():
        adapter = RecordingAdapter(["ok-b"])
        fixture = InvocationFixture({ModelProfileId.LOCAL_FAST: adapter})
        fixture.router = router
        return invoke(fixture, routing(LOCAL), controller_b).output, adapter.calls

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(run_a)
        future_b = pool.submit(run_b)
        assert future_a.result() == 0
        assert future_b.result() == ("ok-b", 1)

    controller_a.close()
    assert controller_b.enabled is False
    assert len(recorder_a.snapshot().records) == 1
    assert recorder_b.snapshot().records == ()
    assert not hasattr(recorder_a.snapshot().records[0], "run_id")


def test_application_scoped_retrieval_service_does_not_cache_run_controller() -> None:
    adapter = FakeRetrievalAdapter()
    service = RetrievalExecutionService(adapter)
    recorder = FaultInjectionRecorder()
    controller = FaultInjectionController.for_test(
        plan(
            FaultPoint.RETRIEVAL_BEFORE_SEARCH,
            code=InjectedFaultCode.INJECTED_TRANSIENT_FAILURE,
        ),
        recorder=recorder,
    )

    def run(active_controller):
        context, _source = make_context()
        return service.execute(
            make_invocation(),
            run_context=context,
            fault_controller=active_controller,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        failed_future = pool.submit(run, controller)
        healthy_future = pool.submit(run, None)
        assert failed_future.result().status is RetrievalExecutionStatus.FAILED
        assert healthy_future.result().status is RetrievalExecutionStatus.SUCCEEDED

    assert len(recorder.snapshot().records) == 1
    assert not hasattr(service, "fault_controller")


def test_model_delay_responds_to_run_cancellation_without_provider_call() -> None:
    adapter = RecordingAdapter(["never-called"])
    fixture = InvocationFixture({ModelProfileId.LOCAL_FAST: adapter})
    controller = FaultInjectionController.for_test(
        plan(
            FaultPoint.MODEL_BEFORE_PROVIDER_CALL,
            action=FaultAction.DELAY,
            code=None,
            delay_seconds=5.0,
        )
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(invoke, fixture, routing(LOCAL), controller)
        time.sleep(0.05)
        fixture.source.cancel(CancellationReason.REQUEST_CANCELLED)
        with pytest.raises(RunCancelledError):
            future.result(timeout=1)

    assert adapter.calls == 0
    assert fixture.ledger.snapshot().committed_usage.model_calls == 0


def test_retrieval_block_responds_to_cancellation_and_scope_cleanup() -> None:
    adapter = FakeRetrievalAdapter()
    context, source = make_context()
    scope = FaultInjectionScope(
        plan(
            FaultPoint.RETRIEVAL_BEFORE_SEARCH,
            action=FaultAction.BLOCK_UNTIL_RELEASED,
            code=None,
        ),
        blocker_timeout_seconds=5.0,
    )
    blocker = scope.blocker("isolated-rule")

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            RetrievalExecutionService(adapter).execute,
            make_invocation(),
            run_context=context,
            fault_controller=scope.controller,
        )
        deadline = time.monotonic() + 1
        while not blocker.entered.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert blocker.entered.is_set()
        source.cancel(CancellationReason.REQUEST_CANCELLED)
        result = future.result(timeout=1)

    assert result.status is RetrievalExecutionStatus.CANCELLED
    assert "embed" not in adapter.calls
    assert "retrieve" not in adapter.calls
    asyncio.run(scope.aclose())
    assert scope.controller.snapshot().closed is True
