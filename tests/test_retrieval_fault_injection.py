from __future__ import annotations

from datetime import UTC, datetime

import pytest

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
    QueryRewriteStrategy,
    RetrievalErrorCategory,
    RetrievalExecutionService,
    RetrievalExecutionStatus,
    RetrievalStage,
    RetrievalStageStatus,
)
from tests.test_retrieval_execution import (
    FakeRetrievalAdapter,
    make_context,
    make_invocation,
)

NOW = datetime(2026, 7, 31, tzinfo=UTC)


def controller(
    point: FaultPoint,
    code: InjectedFaultCode,
    *,
    recorder: FaultInjectionRecorder | None = None,
) -> FaultInjectionController:
    rule = FaultRule(
        rule_id="retrieval-fault",
        fault_point=point,
        action=FaultAction.RAISE_TYPED_ERROR,
        trigger=FaultTrigger.FIRST_MATCH,
        scope=FaultScope.COMPONENT_SCOPE,
        max_hits=1,
        component="retrieval",
        safe_fault_code=code,
    )
    return FaultInjectionController.for_test(
        FaultPlan("retrieval-plan", (rule,), created_at=NOW),
        recorder=recorder,
    )


def execute(adapter, fault_controller=None):
    context, _source = make_context()
    result = RetrievalExecutionService(adapter).execute(
        make_invocation(),
        run_context=context,
        fault_controller=fault_controller,
    )
    return result, context


def test_rewrite_fault_uses_existing_original_query_degradation() -> None:
    adapter = FakeRetrievalAdapter()
    recorder = FaultInjectionRecorder()

    result, context = execute(
        adapter,
        controller(
            FaultPoint.RETRIEVAL_BEFORE_REWRITE,
            InjectedFaultCode.INJECTED_TRANSIENT_FAILURE,
            recorder=recorder,
        ),
    )

    assert result.status is RetrievalExecutionStatus.DEGRADED
    assert adapter.calls.count("rewrite") == 0
    assert adapter.calls.count("retrieve") == 1
    rewrite = result.stage_records[0]
    assert rewrite.stage is RetrievalStage.QUERY_REWRITE
    assert rewrite.status is RetrievalStageStatus.FAILED
    assert rewrite.degraded is True
    assert context.budget_ledger.snapshot().committed_usage.retrieval_calls == 1
    assert len(recorder.snapshot().records) == 1


def test_rewrite_timeout_is_not_degraded_or_retried() -> None:
    adapter = FakeRetrievalAdapter()
    result, _context = execute(
        adapter,
        controller(
            FaultPoint.RETRIEVAL_BEFORE_REWRITE,
            InjectedFaultCode.INJECTED_TIMEOUT,
        ),
    )

    assert result.status is RetrievalExecutionStatus.TIMED_OUT
    assert result.error.category is RetrievalErrorCategory.TIMEOUT
    assert adapter.calls == []
    assert len(result.stage_records) == 1
    assert result.stage_records[0].status is RetrievalStageStatus.TIMED_OUT


@pytest.mark.parametrize(
    "code",
    [
        InjectedFaultCode.INJECTED_TRANSIENT_FAILURE,
        InjectedFaultCode.INJECTED_PERMANENT_FAILURE,
    ],
)
def test_search_fault_prevents_embedding_and_vector_calls(
    code: InjectedFaultCode,
) -> None:
    adapter = FakeRetrievalAdapter()
    result, context = execute(
        adapter,
        controller(FaultPoint.RETRIEVAL_BEFORE_SEARCH, code),
    )

    assert result.status is RetrievalExecutionStatus.FAILED
    assert result.error.category is RetrievalErrorCategory.VECTOR_STORE_FAILED
    assert adapter.calls == ["rewrite"]
    usage = context.budget_ledger.snapshot().committed_usage
    assert usage.embedding_calls == 0
    assert usage.vector_queries == 0


def test_search_timeout_preserves_real_timeout_contract() -> None:
    adapter = FakeRetrievalAdapter()
    result, _context = execute(
        adapter,
        controller(
            FaultPoint.RETRIEVAL_BEFORE_SEARCH,
            InjectedFaultCode.INJECTED_TIMEOUT,
        ),
    )

    assert result.status is RetrievalExecutionStatus.TIMED_OUT
    assert result.error.category is RetrievalErrorCategory.TIMEOUT
    assert adapter.calls == ["rewrite"]


def test_no_rewrite_adapter_does_not_create_a_fake_rewrite_call() -> None:
    adapter = FakeRetrievalAdapter()
    adapter.query_rewrite_strategy = QueryRewriteStrategy.NONE
    result, _context = execute(
        adapter,
        controller(
            FaultPoint.RETRIEVAL_BEFORE_REWRITE,
            InjectedFaultCode.INJECTED_PERMANENT_FAILURE,
        ),
    )

    assert result.status is RetrievalExecutionStatus.SUCCEEDED
    assert "rewrite" not in adapter.calls
    assert result.stage_records[0].status is RetrievalStageStatus.SKIPPED
