from __future__ import annotations

import concurrent.futures
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from core.runtime import (
    FaultAction,
    FaultInjectionController,
    FaultMatchContext,
    FaultPlan,
    FaultPoint,
    FaultRule,
    FaultScope,
    FaultTrigger,
    InjectedFaultCode,
    InMemoryRunEventJournal,
    InMemorySpanRecorder,
    ModelProfileId,
    RetryExecutor,
    RetryPolicy,
    RuntimeEventChannel,
)
from tests._runtime_assembly_fixtures import make_services
from tests._shutdown_fault_fixtures import RecordingResource
from tests.test_event_fault_injection import family_drafts
from tests.test_model_fault_injection import invoke
from tests.test_model_invocation import InvocationFixture, LOCAL, RecordingAdapter, routing
from tests.test_observability_dispatcher import dispatcher
from core.runtime import GracefulShutdownCoordinator


NOW = datetime(2026, 8, 1, tzinfo=UTC)


def _rule(
    rule_id: str,
    point: FaultPoint,
    *,
    priority: int = 100,
    max_hits: int = 1,
    enabled: bool = True,
    component: str | None = None,
    run_id_digest: str | None = None,
) -> FaultRule:
    return FaultRule(
        rule_id=rule_id,
        fault_point=point,
        action=FaultAction.RAISE_TYPED_ERROR,
        trigger=FaultTrigger.ALWAYS,
        scope=FaultScope.RUN_SCOPE,
        max_hits=max_hits,
        priority=priority,
        component=component,
        run_id_digest=run_id_digest,
        safe_fault_code=InjectedFaultCode.INJECTED_TRANSIENT_FAILURE,
        dangerous_window=point in {
            FaultPoint.EVENT_AFTER_JOURNAL_APPEND,
            FaultPoint.TOOL_BEFORE_COMPLETION_EVENT,
            FaultPoint.SNAPSHOT_AFTER_SAVE,
            FaultPoint.TRACE_BEFORE_SPAN_END,
        },
        enabled=enabled,
    )


def _controller(*rules: FaultRule, enabled: bool = True) -> FaultInjectionController:
    return FaultInjectionController(
        FaultPlan("day24-chaos", tuple(rules), created_at=NOW),
        enabled=enabled,
    )


def _context(
    point: FaultPoint,
    component: str,
    run_id_digest: str | None = None,
) -> FaultMatchContext:
    return FaultMatchContext(
        fault_point=point,
        component=component,
        run_id_digest=run_id_digest,
    )


def _counts(controller: FaultInjectionController) -> dict[str, tuple[int, int]]:
    return {
        item.rule_id: (item.match_count, item.hit_count)
        for item in controller.snapshot().counters
    }


def test_same_seam_executes_only_priority_then_rule_id_winner() -> None:
    controller = _controller(
        _rule("z-later", FaultPoint.MODEL_BEFORE_PROVIDER_CALL, priority=1, component="model"),
        _rule("a-winner", FaultPoint.MODEL_BEFORE_PROVIDER_CALL, priority=1, component="model"),
        _rule("lower", FaultPoint.MODEL_BEFORE_PROVIDER_CALL, priority=2, component="model"),
    )
    decision = controller.evaluate(_context(FaultPoint.MODEL_BEFORE_PROVIDER_CALL, "model"))
    assert decision.rule_id == "a-winner"
    assert _counts(controller) == {
        "a-winner": (1, 1),
        "z-later": (0, 0),
        "lower": (0, 0),
    }


def test_mismatch_disabled_and_exhausted_rules_allow_deterministic_takeover() -> None:
    controller = _controller(
        _rule("mismatch", FaultPoint.MODEL_BEFORE_PROVIDER_CALL, priority=0, component="other"),
        _rule("disabled", FaultPoint.MODEL_BEFORE_PROVIDER_CALL, priority=1, component="model", enabled=False),
        _rule("first", FaultPoint.MODEL_BEFORE_PROVIDER_CALL, priority=2, component="model"),
        _rule("takeover", FaultPoint.MODEL_BEFORE_PROVIDER_CALL, priority=3, component="model"),
    )
    context = _context(FaultPoint.MODEL_BEFORE_PROVIDER_CALL, "model")
    assert controller.evaluate(context).rule_id == "first"
    assert controller.evaluate(context).rule_id == "takeover"
    assert _counts(controller) == {
        "mismatch": (0, 0),
        "disabled": (0, 0),
        "first": (2, 1),
        "takeover": (1, 1),
    }


@pytest.mark.parametrize(
    ("name", "first", "second"),
    [
        ("model_observability", FaultPoint.MODEL_BEFORE_PROVIDER_CALL, FaultPoint.OBSERVABILITY_BEFORE_RECORD),
        ("retrieval_trace", FaultPoint.RETRIEVAL_BEFORE_SEARCH, FaultPoint.TRACE_BEFORE_SPAN_END),
        ("tool_event", FaultPoint.TOOL_BEFORE_PROVIDER_CALL, FaultPoint.EVENT_AFTER_JOURNAL_APPEND),
        ("tool_completion_disconnect", FaultPoint.TOOL_BEFORE_COMPLETION_EVENT, FaultPoint.CHANNEL_BEFORE_DRAIN_HANDOFF),
        ("terminal_trace", FaultPoint.JOURNAL_BEFORE_TERMINAL_APPEND, FaultPoint.TRACE_BEFORE_SPAN_END),
        ("snapshot_shutdown", FaultPoint.SNAPSHOT_AFTER_SAVE, FaultPoint.SHUTDOWN_BEFORE_JOURNAL_CLOSE),
        ("recovery_diagnostics", FaultPoint.RECOVERY_AFTER_TAIL_READ, FaultPoint.TRACE_BEFORE_FLUSH),
        ("worker_model", FaultPoint.SHUTDOWN_BEFORE_WORKER_DRAIN, FaultPoint.SHUTDOWN_BEFORE_MODEL_CLOSE),
        ("model_alias", FaultPoint.SHUTDOWN_BEFORE_MODEL_CLOSE, FaultPoint.SHUTDOWN_COMPONENT_CLOSE),
        ("multi_shutdown", FaultPoint.OBSERVABILITY_BEFORE_FLUSH, FaultPoint.SHUTDOWN_COMPONENT_CLOSE),
        ("shutdown_reentry", FaultPoint.SHUTDOWN_BEFORE_JOURNAL_CLOSE, FaultPoint.SHUTDOWN_COMPONENT_CLOSE),
        ("parallel_isolation", FaultPoint.MODEL_BEFORE_PROVIDER_CALL, FaultPoint.TOOL_BEFORE_PROVIDER_CALL),
    ],
)
def test_cross_component_matrix_routes_each_physical_seam_once(name, first, second) -> None:
    del name
    first_component = "first_component"
    second_component = "second_component"
    controller = _controller(
        _rule("first-rule", first, component=first_component),
        _rule("second-rule", second, component=second_component),
    )
    assert controller.evaluate(_context(first, first_component)).rule_id == "first-rule"
    assert controller.evaluate(_context(second, second_component)).rule_id == "second-rule"
    assert _counts(controller) == {"first-rule": (1, 1), "second-rule": (1, 1)}


def test_concurrent_siblings_and_run_digests_do_not_consume_each_other_rules() -> None:
    run_a = "a" * 64
    run_b = "b" * 64
    controller = _controller(
        _rule("run-a", FaultPoint.MODEL_BEFORE_PROVIDER_CALL, component="model", run_id_digest=run_a),
        _rule("run-b", FaultPoint.MODEL_BEFORE_PROVIDER_CALL, component="model", run_id_digest=run_b),
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        decisions = tuple(
            pool.map(
                controller.evaluate,
                (
                    _context(FaultPoint.MODEL_BEFORE_PROVIDER_CALL, "model", run_a),
                    _context(FaultPoint.MODEL_BEFORE_PROVIDER_CALL, "model", run_b),
                ),
            )
        )
    assert {decision.rule_id for decision in decisions} == {"run-a", "run-b"}
    assert _counts(controller) == {"run-a": (1, 1), "run-b": (1, 1)}


def test_controller_close_race_is_bounded_and_no_rule_runs_after_close() -> None:
    controller = _controller(
        _rule("ordinary", FaultPoint.MODEL_BEFORE_PROVIDER_CALL, max_hits=100, component="model"),
        _rule("dangerous", FaultPoint.EVENT_AFTER_JOURNAL_APPEND, max_hits=100, component="event_channel"),
    )
    controller.close()
    contexts = (
        _context(FaultPoint.MODEL_BEFORE_PROVIDER_CALL, "model"),
        _context(FaultPoint.EVENT_AFTER_JOURNAL_APPEND, "event_channel"),
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        decisions = tuple(pool.map(controller.evaluate, contexts))
    assert not any(decision.matched for decision in decisions)
    assert _counts(controller) == {"ordinary": (0, 0), "dangerous": (0, 0)}


@pytest.mark.asyncio
async def test_model_retry_and_observability_degradation_coexist_without_business_rerun() -> None:
    controller = _controller(
        _rule("model-transient", FaultPoint.MODEL_BEFORE_PROVIDER_CALL, component="model"),
        _rule(
            "record-failure",
            FaultPoint.OBSERVABILITY_BEFORE_RECORD,
            component="observability_dispatcher",
        ),
    )
    adapter = RecordingAdapter(["safe-output"])
    fixture = InvocationFixture({ModelProfileId.LOCAL_FAST: adapter})
    fixture.router = type(fixture.router)(
        retry_executor=RetryExecutor(
            RetryPolicy(max_attempts=2, base_delay_seconds=0, max_delay_seconds=0)
        )
    )
    result = invoke(fixture, routing(LOCAL), controller)
    value, *_ = dispatcher()
    journal = InMemoryRunEventJournal()
    channel = RuntimeEventChannel(
        4,
        run_id="run-a",
        journal=journal,
        observability_dispatcher=value,
        fault_controller=controller,
    )
    await channel.publish(family_drafts()[0])

    assert result.output == "safe-output"
    assert [attempt.succeeded for attempt in result.attempts] == [False, True]
    assert adapter.calls == 1
    assert journal.last_sequence("run-a") == channel.buffered_count == 1
    assert value.health.snapshot().status == "DEGRADED"
    assert _counts(controller) == {
        "model-transient": (2, 1),
        "record-failure": (1, 1),
    }
    await channel.abort()
    assert await value.close()


@pytest.mark.asyncio
async def test_six_shutdown_faults_each_get_one_bounded_attempt() -> None:
    calls: list[str] = []
    journal = RecordingResource("journal", calls)
    snapshot = RecordingResource("snapshot", calls)
    model = RecordingResource("model", calls)
    remaining = RecordingResource("remaining", calls)
    observability, *_ = dispatcher()
    services = replace(
        make_services(snapshot_enabled=False),
        event_journal=journal,
        observability_dispatcher=observability,
        span_recorder=InMemorySpanRecorder(),
        extra_closeables=(
            ("snapshot_store", snapshot),
            ("model_engine_0", model),
            ("remaining_store", remaining),
        ),
    )
    rules = (
        _rule("obs-flush", FaultPoint.OBSERVABILITY_BEFORE_FLUSH, component="observability_dispatcher"),
        _rule("trace-flush", FaultPoint.TRACE_BEFORE_FLUSH, component="trace_recorder"),
        FaultRule(
            rule_id="snapshot-close",
            fault_point=FaultPoint.SHUTDOWN_COMPONENT_CLOSE,
            action=FaultAction.RAISE_TYPED_ERROR,
            trigger=FaultTrigger.ALWAYS,
            scope=FaultScope.COMPONENT_SCOPE,
            max_hits=1,
            component="graceful_shutdown",
            shutdown_component="snapshot_store",
            safe_fault_code=InjectedFaultCode.INJECTED_COMPONENT_CLOSE_FAILURE,
        ),
        FaultRule(
            rule_id="journal-close",
            fault_point=FaultPoint.SHUTDOWN_BEFORE_JOURNAL_CLOSE,
            action=FaultAction.RAISE_TYPED_ERROR,
            trigger=FaultTrigger.ALWAYS,
            scope=FaultScope.COMPONENT_SCOPE,
            max_hits=1,
            component="graceful_shutdown",
            shutdown_component="event_journal",
            safe_fault_code=InjectedFaultCode.INJECTED_COMPONENT_CLOSE_FAILURE,
        ),
        FaultRule(
            rule_id="model-close",
            fault_point=FaultPoint.SHUTDOWN_BEFORE_MODEL_CLOSE,
            action=FaultAction.RAISE_TYPED_ERROR,
            trigger=FaultTrigger.ALWAYS,
            scope=FaultScope.COMPONENT_SCOPE,
            max_hits=1,
            component="graceful_shutdown",
            shutdown_component="model_engine_0",
            safe_fault_code=InjectedFaultCode.INJECTED_COMPONENT_CLOSE_FAILURE,
        ),
        FaultRule(
            rule_id="remaining-close",
            fault_point=FaultPoint.SHUTDOWN_COMPONENT_CLOSE,
            action=FaultAction.RAISE_TYPED_ERROR,
            trigger=FaultTrigger.ALWAYS,
            scope=FaultScope.COMPONENT_SCOPE,
            max_hits=1,
            component="graceful_shutdown",
            shutdown_component="remaining_store",
            safe_fault_code=InjectedFaultCode.INJECTED_COMPONENT_CLOSE_FAILURE,
        ),
    )
    controller = _controller(*rules)
    report = await GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.1,
    ).shutdown(controller)

    assert report.orchestration_completed is True
    assert report.fully_closed is False
    assert report.has_failures is True
    counts = _counts(controller)
    assert all(hit_count == 1 for _match_count, hit_count in counts.values()), counts
    assert journal.close_calls == snapshot.close_calls == model.close_calls == remaining.close_calls == 0
