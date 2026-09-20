"""Stage10-WP1 narrow persistence-contract tests."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
import uuid

import pytest

from core.persistence.models import (
    DurableToolInvocationRow,
    RuntimeModelInvocationRow,
    RuntimeRunExecutionRow,
    RuntimeStepExecutionRow,
)
from core.runtime.execution_aggregate import (
    ExecutionRootInput,
    ModelAttemptState,
    canonical_payload_digest,
    plan_from_payload,
    plan_payload,
)
from core.runtime.planning import TaskCapabilityRequirements, create_single_step_plan
from core.persistence.repositories.execution import DurableExecutionRepository
from core.runtime.run_control import DurableRunControlService, OwnershipLost


def _execution_root(run_id: str) -> ExecutionRootInput:
    plan = create_single_step_plan("core_router", TaskCapabilityRequirements())
    return ExecutionRootInput(
        run_id=run_id,
        resume_input={"entry_agent_id": "core_router", "user_query": "resume"},
        plan=plan,
        absolute_deadline=None,
        budget_totals={"max_model_calls": 2},
        budget_reserved={"model_calls": 1},
        budget_consumed={"model_calls": 0},
    )


def test_execution_digest_is_canonical_and_not_repr_based() -> None:
    assert canonical_payload_digest({"b": 2, "a": 1}) == canonical_payload_digest(
        {"a": 1, "b": 2}
    )
    assert canonical_payload_digest({"value": 1}) != canonical_payload_digest(
        {"value": "1"}
    )


def test_plan_payload_round_trips_with_stable_fingerprint_source() -> None:
    plan = create_single_step_plan("agent", TaskCapabilityRequirements())
    payload = plan_payload(plan)
    restored = plan_from_payload(payload)
    assert restored == plan
    assert canonical_payload_digest(payload) == canonical_payload_digest(plan_payload(restored))


def test_execution_schema_has_recovery_identity_and_fencing_constraints() -> None:
    root_names = {constraint.name for constraint in RuntimeRunExecutionRow.__table__.constraints}
    step_names = {constraint.name for constraint in RuntimeStepExecutionRow.__table__.constraints}
    assert "ck_runtime_run_execution_status" in root_names
    assert "ck_runtime_run_execution_fencing_token" in root_names
    assert "uq_runtime_step_execution_identity" in step_names
    assert "ck_runtime_step_execution_status" in step_names
    assert [column.name for column in RuntimeStepExecutionRow.__table__.primary_key.columns] == [
        "run_id",
        "step_id",
    ]
    assert RuntimeStepExecutionRow.__table__.c.run_id.foreign_keys
    assert RuntimeModelInvocationRow.__table__.c.run_id.foreign_keys


def test_tool_recovery_binding_columns_are_safe_optional_bindings() -> None:
    table = DurableToolInvocationRow.__table__
    assert table.c.arguments_digest.type.length == 64
    assert table.c.committed_result.nullable is True
    assert table.c.committed_result_digest.type.length == 64


def test_stage10_migration_declares_tool_recovery_columns() -> None:
    migration = Path(__file__).parents[1] / "alembic" / "versions" / "0021_stage10_wp1_execution_aggregate.py"
    source = migration.read_text(encoding="utf-8")
    assert 'sa.Column("arguments_digest", sa.CHAR(64)' in source
    assert 'sa.Column("committed_result", postgresql.JSONB()' in source
    assert 'sa.Column("committed_result_digest", sa.CHAR(64)' in source


def test_absolute_deadline_is_utc_and_plan_payload_is_safe_json() -> None:
    plan = create_single_step_plan("agent", TaskCapabilityRequirements())
    from core.runtime.execution_aggregate import ExecutionRootInput

    root = ExecutionRootInput(
        run_id="run-1",
        resume_input={"query_digest": "digest"},
        plan=plan,
        absolute_deadline=datetime.now(UTC),
        budget_totals={"tokens": 10},
        budget_reserved={"tokens": 0},
        budget_consumed={"tokens": 0},
    )
    values = root.as_row_values()
    assert values["absolute_deadline"].tzinfo is not None
    assert isinstance(values["plan_payload"], dict)


@pytest.mark.asyncio
async def test_stale_scan_is_hint_and_only_one_takeover_claim_wins(clean_database) -> None:
    run_id = f"stage10-stale-{uuid.uuid4().hex}"
    control = DurableRunControlService(clean_database, lease_seconds=30)
    repository = DurableExecutionRepository(clean_database, control)
    first = await control.claim(run_id, "worker-a")
    await repository.initialize(_execution_root(run_id), lease=first)
    assert run_id not in await repository.stale_run_ids()

    await control.release(first)
    assert run_id in await repository.stale_run_ids()
    claims = await asyncio.gather(
        control.claim(run_id, "worker-b"),
        control.claim(run_id, "worker-c"),
        return_exceptions=True,
    )
    assert sum(not isinstance(item, Exception) for item in claims) == 1


@pytest.mark.asyncio
async def test_model_recovery_allows_one_retry_preserves_budget_then_blocks(clean_database) -> None:
    run_id = f"stage10-model-{uuid.uuid4().hex}"
    control = DurableRunControlService(clean_database, lease_seconds=30)
    repository = DurableExecutionRepository(clean_database, control)
    first = await control.claim(run_id, "worker-a")
    await repository.initialize(_execution_root(run_id), lease=first)
    step_attempt = await repository.start_step(
        first, step_id="answer", plan_version=1
    )
    await repository.start_model_attempt(
        first,
        step_id="answer",
        attempt=step_attempt,
        model_attempt=1,
        request_digest="a" * 64,
        provider_kind="deterministic",
        profile_identity="test-model",
    )

    await control.release(first)
    second = await control.claim(run_id, "worker-b")
    recovered = await repository.prepare_recovery(second)
    assert recovered.steps[0].status == "PENDING"
    assert recovered.models[0].state == ModelAttemptState.UNKNOWN.value
    assert recovered.root.budget_reserved == {"model_calls": 1}

    second_attempt = await repository.start_step(
        second, step_id="answer", plan_version=1
    )
    await repository.start_model_attempt(
        second,
        step_id="answer",
        attempt=second_attempt,
        model_attempt=1,
        request_digest="b" * 64,
        provider_kind="deterministic",
        profile_identity="test-model",
    )
    await control.release(second)
    third = await control.claim(run_id, "worker-c")
    exhausted = await repository.prepare_recovery(third)
    assert exhausted.steps[0].status == "BLOCKED"
    assert exhausted.steps[0].safe_error == "MODEL_RECOVERY_EXHAUSTED"

    with pytest.raises(OwnershipLost):
        await repository.finish_model_attempt(
            second,
            step_id="answer",
            attempt=second_attempt,
            model_attempt=1,
            state=ModelAttemptState.COMPLETED,
            result={"output": "late"},
        )


@pytest.mark.asyncio
async def test_recovery_failure_backoff_becomes_manual_required(clean_database) -> None:
    run_id = f"stage10-backoff-{uuid.uuid4().hex}"
    control = DurableRunControlService(clean_database, lease_seconds=30)
    repository = DurableExecutionRepository(clean_database, control)
    lease = await control.claim(run_id, "worker-a")
    await repository.initialize(_execution_root(run_id), lease=lease)

    assert await repository.record_recovery_failure(
        run_id, "FIRST_FAILURE", max_attempts=2, initial_seconds=1, max_seconds=1
    )
    image = await repository.load(run_id)
    assert image is not None
    assert image.root.recovery_attempt_count == 1
    assert image.root.manual_required is False
    assert await repository.record_recovery_failure(
        run_id, "SECOND_FAILURE", max_attempts=2, initial_seconds=1, max_seconds=1
    )
    image = await repository.load(run_id)
    assert image is not None
    assert image.root.recovery_attempt_count == 2
    assert image.root.manual_required is True
    assert image.root.next_recovery_at is None
