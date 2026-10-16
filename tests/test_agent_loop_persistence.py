"""真实 PostgreSQL 验证接受先于执行、generation 与预算恢复。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from core.persistence.repositories.execution import DurableExecutionRepository
from core.runtime.execution_aggregate import ExecutionRootInput, ModelAttemptState, canonical_payload_digest
from core.runtime.planning import TaskCapabilityRequirements, create_single_step_plan
from core.runtime.run_control import DurableRunControlService, OwnershipLost


async def _setup(database):
    control = DurableRunControlService(database)
    repository = DurableExecutionRepository(database, control)
    run_id = f"loop-{uuid4().hex}"
    lease = await control.claim(run_id, "first")
    root = ExecutionRootInput(
        run_id=run_id, resume_input={"agent_loop": {
            "context_seed": {"messages": [{"role": "user", "content": "synthetic"}]},
            "pinned_config": {"model": "synthetic"}, "loop_policy": {"max_iterations": 3},
        }}, plan=create_single_step_plan("core_router", TaskCapabilityRequirements()),
        absolute_deadline=datetime.now(UTC) + timedelta(seconds=300),
        budget_totals={"max_model_calls": 3}, budget_reserved={}, budget_consumed={},
        execution_policy="durable-agent-loop.v1",
    )
    await repository.initialize(root, lease=lease)
    await repository.start_step(lease, step_id="answer", plan_version=1)
    frame, _ = await repository.load_loop(lease, "answer")
    await repository.open_loop_iteration(lease, frame.frame_id, resume_generation=0,
        row_version=frame.row_version, context_input_digest="a" * 64, context_cursor=0)
    return control, repository, lease, frame.frame_id


@pytest.mark.asyncio
async def test_accepted_decision_survives_takeover_and_rejects_late_generation(clean_database):
    control, repository, lease, frame_id = await _setup(clean_database)
    await repository.start_model_attempt(lease, step_id="answer", attempt=1, model_attempt=1,
        request_digest="a" * 64, provider_kind="synthetic", profile_identity="synthetic",
        loop_frame_id=frame_id, loop_iteration_no=1, resume_generation=0,
        invocation_group=f"{frame_id}:1", operation_id="model:1", reserved_usage={"model_calls": 1})
    await repository.finish_model_attempt(lease, step_id="answer", attempt=1, model_attempt=1,
        state=ModelAttemptState.COMPLETED, result={"output": "accepted"}, resume_generation=0,
        operation_id="model:1", actual_usage={"model_calls": 1})
    frame, _ = await repository.load_loop(lease, "answer")
    decision_id, action_id = "decision-" + uuid4().hex, "action-" + uuid4().hex
    accepted = await repository.accept_loop_decision(lease, frame_id, 1, resume_generation=0,
        row_version=frame.row_version, decision_id=decision_id,
        decision_kind="TOOL_CALL", decision_digest="b" * 64, model_attempt=(1, 1),
        action_id=action_id, action_recipe={"read_only": False})
    replay = await repository.accept_loop_decision(lease, frame_id, 1, resume_generation=0,
        row_version=frame.row_version, decision_id=decision_id,
        decision_kind="TOOL_CALL", decision_digest="b" * 64, model_attempt=(1, 1),
        action_id=action_id, action_recipe={"read_only": False})
    assert replay.decision_id == accepted.decision_id
    await control.release(lease)
    takeover = await control.claim(lease.run_id, "second")
    image = await repository.prepare_recovery(takeover)
    assert image.steps[0].status == "RUNNING"
    assert image.loop_frames[0].resume_generation == 1
    assert image.loop_frames[0].phase == "DECISION_ACCEPTED"
    assert len(image.models) == 1
    assert image.loop_iterations[0].accepted_generation == 0
    assert (await repository.get_loop_model_response(takeover, frame_id, 1)).result_binding == {"output": "accepted"}
    with pytest.raises(OwnershipLost):
        await repository.stop_loop(lease, frame_id, resume_generation=0, stop_code="LATE")
    with pytest.raises(RuntimeError, match="LOOP_STALE_GENERATION"):
        await repository.stop_loop(takeover, frame_id, resume_generation=0, stop_code="LATE")
    repeated = await repository.prepare_recovery(takeover)
    assert repeated.loop_frames[0].resume_generation == 1
    assert repeated.root.budget_consumed["model_calls"] == 1


@pytest.mark.asyncio
async def test_crashed_model_reservation_settles_once_and_only_current_iteration_retry_counts(clean_database):
    control, repository, lease, frame_id = await _setup(clean_database)
    await repository.start_model_attempt(lease, step_id="answer", attempt=1, model_attempt=1,
        request_digest="a" * 64, provider_kind="synthetic", profile_identity="synthetic",
        loop_frame_id=frame_id, loop_iteration_no=1, resume_generation=0,
        invocation_group=f"{frame_id}:1", operation_id="model:1", reserved_usage={"model_calls": 1})
    # 已收口的 transport UNKNOWN 计费，但不消耗 crash recovery 次数。
    await repository.finish_model_attempt(lease, step_id="answer", attempt=1, model_attempt=1,
        state=ModelAttemptState.UNKNOWN, resume_generation=0, operation_id="model:1",
        actual_usage={"model_calls": 1})
    await repository.start_model_attempt(lease, step_id="answer", attempt=1, model_attempt=2,
        request_digest="a" * 64, provider_kind="synthetic", profile_identity="synthetic",
        loop_frame_id=frame_id, loop_iteration_no=1, resume_generation=0,
        invocation_group=f"{frame_id}:1", operation_id="model:2", reserved_usage={"model_calls": 1})
    await control.release(lease)
    takeover = await control.claim(lease.run_id, "second")
    image = await repository.prepare_recovery(takeover)
    assert image.models[0].state == "UNKNOWN"
    assert image.root.budget_reserved["model_calls"] == 0
    assert image.root.budget_consumed["model_calls"] == 2
    assert image.root.budget_operations["model:2"]["source"] == "RECOVERY_CONSERVATIVE"
    assert image.loop_iterations[0].model_recovery_count == 1
    repeated = await repository.prepare_recovery(takeover)
    assert repeated.root.budget_consumed["model_calls"] == 2
    assert repeated.loop_iterations[0].model_recovery_count == 1
    assert repeated.loop_frames[0].phase == "NEED_DECISION"
    await repository.start_model_attempt(takeover, step_id="answer", attempt=1, model_attempt=3,
        request_digest="a" * 64, provider_kind="synthetic", profile_identity="synthetic",
        loop_frame_id=frame_id, loop_iteration_no=1, resume_generation=1,
        invocation_group=f"{frame_id}:1", operation_id="model:3", reserved_usage={"model_calls": 1})
    await control.release(takeover)
    last = await control.claim(lease.run_id, "third")
    image = await repository.prepare_recovery(last)
    assert image.loop_frames[0].phase == "STOPPED"
    assert image.loop_frames[0].stop_code == "LOOP_MODEL_RECOVERY_EXHAUSTED"
    assert image.root.budget_consumed["model_calls"] == 3
    assert image.loop_iterations[0].model_recovery_count == 2
