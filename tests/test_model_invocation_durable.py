"""Stage10-WP1 model durable lifecycle seam tests."""

from dataclasses import dataclass
from types import SimpleNamespace
import asyncio
import tempfile

import pytest

from core.agent_router import AgentRouter
from core.llm_engine import ScriptedEvaluationLLMEngine
from core.memory_manager import MemoryManager
from core.runtime.budget import BudgetLedger, RunBudget
from core.runtime.execution_aggregate import ModelAttemptState
from core.runtime.model_invocation import (
    DurableModelLifecycleError,
    ModelAdapterResolver,
    ModelAdapterResponse,
    ModelInvocationChainError,
    ModelInvocationRouter,
)
from core.runtime.circuit_breaker import ModelCircuitBreakerRegistry
from core.runtime.event_channel import RuntimeEventChannel
from core.runtime.event_emitter import RunEventEmitter
from core.runtime.model_routing import ModelFailureCategory
from core.runtime.retry import RetryExecutor, RetryPolicy
from core.runtime.context import create_run_context

from tests.test_model_invocation import LOCAL, RecordingAdapter, provider_error, routing


@dataclass
class _ModelRow:
    step_id: str
    attempt_number: int
    model_attempt_number: int
    state: str
    request_digest: str
    result_binding: dict | None = None
    safe_error: str | None = None


class _DurableRepository:
    def __init__(self) -> None:
        self.rows: list[_ModelRow] = []
        self.calls: list[tuple[str, int]] = []

    async def load(self, run_id: str):
        return SimpleNamespace(
            steps=(SimpleNamespace(step_id="answer", current_attempt=1),),
            models=tuple(self.rows),
        )

    async def start_model_attempt(self, lease, **kwargs):
        self.calls.append(("start", kwargs["model_attempt"]))
        self.rows.append(
            _ModelRow(
                step_id=kwargs["step_id"],
                attempt_number=kwargs["attempt"],
                model_attempt_number=kwargs["model_attempt"],
                state=ModelAttemptState.STARTED.value,
                request_digest=kwargs["request_digest"],
            )
        )

    async def finish_model_attempt(self, lease, **kwargs):
        self.calls.append((kwargs["state"].value.lower(), kwargs["model_attempt"]))
        row = next(
            item
            for item in self.rows
            if item.step_id == kwargs["step_id"]
            and item.attempt_number == kwargs["attempt"]
            and item.model_attempt_number == kwargs["model_attempt"]
        )
        if row.state != ModelAttemptState.STARTED.value:
            raise RuntimeError("stale model attempt")
        row.state = kwargs["state"].value
        row.result_binding = kwargs.get("result")
        row.safe_error = kwargs.get("safe_error")


def _invoke(context, repository, adapter, *, retry=False):
    context.attach_durable_lease(SimpleNamespace(run_id=context.run_id, fencing_token=1))
    context.attach_execution_repository(repository)
    ledger = BudgetLedger(RunBudget(), deadline_remaining=context.remaining_seconds)
    context.attach_budget_ledger(ledger)
    policy = RetryPolicy(max_attempts=2 if retry else 1, base_delay_seconds=0, max_delay_seconds=0)
    router = ModelInvocationRouter(retry_executor=RetryExecutor(policy))
    submit = lambda coroutine: asyncio.run(coroutine)
    return router.invoke(
        run_context=context,
        budget_ledger=ledger,
        routing_decision=routing(LOCAL),
        messages=({"role": "user", "content": "secret prompt"},),
        adapter_resolver=ModelAdapterResolver({LOCAL.profile_id: adapter}),
        circuit_breaker_registry=ModelCircuitBreakerRegistry(),
        token_estimate=1,
        max_tokens=8,
        event_emitter=SimpleNamespace(step_id="answer"),
        async_submit=submit,
    )


def test_durable_model_started_then_completed_persists_safe_binding() -> None:
    context, _ = create_run_context(entry_agent_id="test")
    repository = _DurableRepository()

    result = _invoke(
        context,
        repository,
        RecordingAdapter([ModelAdapterResponse("ok")]),
    )

    assert result.output == "ok"
    assert [name for name, _ in repository.calls] == ["start", "completed"]
    row = repository.rows[0]
    assert row.state == ModelAttemptState.COMPLETED.value
    assert row.result_binding == {"output": "ok"}
    assert "secret prompt" not in row.request_digest


def test_provider_started_failure_becomes_unknown_and_one_retry_is_new_attempt() -> None:
    context, _ = create_run_context(entry_agent_id="test")
    repository = _DurableRepository()

    result = _invoke(
        context,
        repository,
        RecordingAdapter([
            provider_error(ModelFailureCategory.TRANSIENT_PROVIDER_FAILURE),
            "recovered",
        ]),
        retry=True,
    )

    assert result.output == "recovered"
    assert [(row.model_attempt_number, row.state) for row in repository.rows] == [
        (1, ModelAttemptState.UNKNOWN.value),
        (2, ModelAttemptState.COMPLETED.value),
    ]


def test_second_provider_failure_stays_fail_closed_after_retry_budget() -> None:
    context, _ = create_run_context(entry_agent_id="test")
    repository = _DurableRepository()

    with pytest.raises(ModelInvocationChainError):
        _invoke(
            context,
            repository,
            RecordingAdapter([
                provider_error(ModelFailureCategory.TRANSIENT_PROVIDER_FAILURE),
                provider_error(ModelFailureCategory.TRANSIENT_PROVIDER_FAILURE),
            ]),
            retry=True,
        )

    assert [row.state for row in repository.rows] == [
        ModelAttemptState.UNKNOWN.value,
        ModelAttemptState.UNKNOWN.value,
    ]


@pytest.mark.asyncio
async def test_production_planner_run_emitter_executes_before_step_aggregate() -> None:
    """Dynamic core_router planning must not require a Step durable row."""
    context, _ = create_run_context(entry_agent_id="core_router")
    repository = _DurableRepository()
    context.attach_durable_lease(
        SimpleNamespace(run_id=context.run_id, fencing_token=1)
    )
    context.attach_execution_repository(repository)
    ledger = BudgetLedger(RunBudget(), deadline_remaining=context.remaining_seconds)
    context.attach_budget_ledger(ledger)

    channel = RuntimeEventChannel(8, run_id=context.run_id)
    emitter = RunEventEmitter(
        run_id=context.run_id,
        trace_id=context.trace_id,
        channel=channel,
    )
    with tempfile.TemporaryDirectory() as directory:
        router = AgentRouter(
            ScriptedEvaluationLLMEngine(),
            MemoryManager(f"{directory}/memory.db"),
            orchestration_enabled=False,
        )
        output = await asyncio.to_thread(
            router.complete_planning_decision,
            "fixture_env_probe",
            run_context=context,
            event_emitter=emitter,
        )

    assert '"decision":"DELEGATE"' in output
    # The planner ran through the production AgentRouter/model provider, but
    # no Step-scoped durable attempt exists before dynamic plan initialization.
    assert repository.calls == []

    await channel.close()
    events = [event async for event in channel]
    assert [event.event_type.value for event in events] == [
        "MODEL_STARTED",
        "MODEL_COMPLETED",
    ]


def test_step_scoped_model_without_step_id_remains_fail_closed() -> None:
    context, _ = create_run_context(entry_agent_id="test")
    repository = _DurableRepository()
    adapter = RecordingAdapter([ModelAdapterResponse("must not run")])

    with pytest.raises(DurableModelLifecycleError):
        _invoke_with_emitter(
            context,
            repository,
            adapter,
            event_emitter=SimpleNamespace(),
        )

    assert adapter.calls == 0
    assert repository.calls == []


def _invoke_with_emitter(
    context,
    repository,
    adapter,
    *,
    event_emitter,
):
    context.attach_durable_lease(SimpleNamespace(run_id=context.run_id, fencing_token=1))
    context.attach_execution_repository(repository)
    ledger = BudgetLedger(RunBudget(), deadline_remaining=context.remaining_seconds)
    context.attach_budget_ledger(ledger)
    return ModelInvocationRouter().invoke(
        run_context=context,
        budget_ledger=ledger,
        routing_decision=routing(LOCAL),
        messages=({"role": "user", "content": "redacted"},),
        adapter_resolver=ModelAdapterResolver({LOCAL.profile_id: adapter}),
        circuit_breaker_registry=ModelCircuitBreakerRegistry(),
        token_estimate=1,
        max_tokens=8,
        event_emitter=event_emitter,
        async_submit=lambda coroutine: asyncio.run(coroutine),
    )
