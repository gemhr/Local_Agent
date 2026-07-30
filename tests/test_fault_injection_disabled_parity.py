from __future__ import annotations

from datetime import UTC, datetime

from core.runtime import (
    FaultAction,
    FaultInjectionController,
    FaultInjectionRecorder,
    FaultPlan,
    FaultPoint,
    FaultRule,
    FaultScope,
    FaultTrigger,
    InjectedFaultCode,
    InMemorySpanRecorder,
    ModelProfileId,
    RetrievalExecutionService,
)
from tests.test_model_fault_injection import invoke
from tests.test_model_invocation import InvocationFixture, LOCAL, RecordingAdapter, routing
from tests.test_retrieval_execution import (
    FakeRetrievalAdapter,
    make_context,
    make_invocation,
)

NOW = datetime(2026, 7, 31, tzinfo=UTC)


def disabled(recorder: FaultInjectionRecorder) -> FaultInjectionController:
    rule = FaultRule(
        rule_id="disabled-rule",
        fault_point=FaultPoint.MODEL_BEFORE_PROVIDER_CALL,
        action=FaultAction.RAISE_TYPED_ERROR,
        trigger=FaultTrigger.FIRST_MATCH,
        scope=FaultScope.ATTEMPT_SCOPE,
        max_hits=1,
        component="model",
        safe_fault_code=InjectedFaultCode.INJECTED_TRANSIENT_FAILURE,
    )
    return FaultInjectionController(
        FaultPlan("disabled-plan", (rule,), created_at=NOW),
        enabled=False,
        recorder=recorder,
    )


def span_shape(recorder: InMemorySpanRecorder):
    return tuple(
        (
            item.component,
            item.operation,
            item.status,
            tuple(sorted(item.attributes.items())),
        )
        for item in recorder.snapshot()
    )


def test_model_no_controller_and_disabled_controller_are_strongly_equivalent() -> None:
    recorder = FaultInjectionRecorder()

    def run(fault_controller):
        adapter = RecordingAdapter(["same-output"])
        fixture = InvocationFixture({ModelProfileId.LOCAL_FAST: adapter})
        spans = InMemorySpanRecorder()
        fixture.router = type(fixture.router)(span_recorder=spans)
        result = invoke(fixture, routing(LOCAL), fault_controller)
        return (
            result,
            adapter.calls,
            fixture.ledger.snapshot().committed_usage,
            span_shape(spans),
        )

    without = run(None)
    with_disabled = run(disabled(recorder))

    assert without == with_disabled
    assert recorder.snapshot().records == ()


def test_retrieval_no_controller_and_disabled_controller_are_equivalent() -> None:
    recorder = FaultInjectionRecorder()

    def run(fault_controller):
        adapter = FakeRetrievalAdapter()
        context, _source = make_context()
        spans = InMemorySpanRecorder()
        result = RetrievalExecutionService(adapter, span_recorder=spans).execute(
            make_invocation(),
            run_context=context,
            fault_controller=fault_controller,
        )
        return (
            result.status,
            tuple(
                (
                    record.stage,
                    record.status,
                    record.input_count,
                    record.output_count,
                    record.safe_error_code,
                    record.budget_usage,
                    record.degraded,
                )
                for record in result.stage_records
            ),
            result.budget_usage,
            tuple(adapter.calls),
            span_shape(spans),
            result.rendered_context,
        )

    assert run(None) == run(disabled(recorder))
    assert recorder.snapshot().records == ()
