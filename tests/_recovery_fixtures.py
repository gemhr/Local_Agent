from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from core.runtime.budget import BudgetLedger, RunBudget
from core.runtime.checkpoint_contract import CheckpointKind, RuntimeActivitySnapshot
from core.runtime.events import (
    RuntimeEvent,
    RuntimeEventPayload,
    RuntimeEventType,
)
from core.runtime.plan_fingerprint import PlanFingerprinter
from core.runtime.planning import (
    Plan,
    PlanSource,
    PlanStep,
    TaskCapabilityRequirements,
)
from core.runtime.snapshot_contract import (
    AgentStateSnapshot,
    BudgetSnapshot,
    PlanSnapshot,
    RunSnapshot,
    RuntimeMetadata,
)
from core.runtime.state import AgentState


NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def recovery_plan(*, summary: str = "summary") -> Plan:
    return Plan(
        plan_id="recovery-plan",
        version=1,
        task_summary=summary,
        steps=(
            PlanStep(
                "step",
                "step",
                "safe description",
                (),
                "done",
                "router",
                TaskCapabilityRequirements(),
            ),
        ),
        created_at=NOW,
        source=PlanSource.DETERMINISTIC,
    )


def activity(
    *,
    running_step_count: int = 0,
    budget_reservation_count: int = 0,
    model_attempts_active: int = 0,
    tool_attempts_active: int = 0,
    retrievals_active: int = 0,
    detached_tool_workers: int = 0,
    detached_retrieval_workers: int = 0,
    event_publications_in_flight: int = 0,
    step_workers_active: int = 0,
    state_event_transitions_in_flight: int = 0,
    state_event_transition_epoch: int = 0,
    state_event_transition_observed: bool = False,
    activity_unknown: bool = False,
) -> RuntimeActivitySnapshot:
    return RuntimeActivitySnapshot(
        claim_in_progress=0,
        running_step_count=running_step_count,
        budget_reservation_count=budget_reservation_count,
        model_attempts_active=model_attempts_active,
        tool_attempts_active=tool_attempts_active,
        retrievals_active=retrievals_active,
        detached_tool_workers=detached_tool_workers,
        detached_retrieval_workers=detached_retrieval_workers,
        event_publications_in_flight=event_publications_in_flight,
        step_workers_active=step_workers_active,
        activity_unknown=activity_unknown,
        captured_at=NOW,
        state_event_transitions_in_flight=state_event_transitions_in_flight,
        state_event_transition_epoch=state_event_transition_epoch,
        state_event_transition_observed=state_event_transition_observed,
    )


def recovery_snapshot(
    *,
    plan: Plan | None = None,
    state: AgentState | None = None,
    sequence: int = 0,
    kind: CheckpointKind = CheckpointKind.STEP_BOUNDARY,
    activity_snapshot: RuntimeActivitySnapshot | None = None,
    quiescent: bool = True,
) -> RunSnapshot:
    plan = plan or recovery_plan()
    if state is None:
        state = AgentState("run")
        state.mark_running()
        state.add_step("step", "step")
    return RunSnapshot.create(
        snapshot_id=uuid4().hex,
        run_id=state.run_id,
        trace_id="trace",
        plan_snapshot=PlanSnapshot.from_plan(plan),
        plan_fingerprint=PlanFingerprinter.fingerprint(plan),
        state_snapshot=AgentStateSnapshot.from_agent_state(state),
        budget_snapshot=BudgetSnapshot.from_runtime_snapshot(
            BudgetLedger(RunBudget(max_model_calls=3)).snapshot()
        ),
        last_journal_sequence=sequence,
        runtime_metadata=RuntimeMetadata(
            runtime_schema_version=1,
            runtime_mode="test",
            planner_version="1",
            scheduler_version="1",
            model_routing_policy_version="1",
            tool_contract_version="1",
            retrieval_contract_version="1",
            event_schema_version="2",
            journal_schema_version="2",
        ),
        checkpoint_kind=kind,
        quiescent=quiescent,
        activity_snapshot=activity_snapshot or activity(),
        created_at=NOW,
    )


def runtime_event(
    sequence: int,
    event_type: RuntimeEventType,
    payload: RuntimeEventPayload,
    *,
    run_id: str = "run",
    step_id: str | None = None,
    step_sequence: int | None = None,
) -> RuntimeEvent:
    return RuntimeEvent(
        schema_version=2,
        event_id=uuid4().hex,
        run_id=run_id,
        trace_id="trace",
        sequence=sequence,
        event_type=event_type,
        emitted_at=NOW,
        component="test",
        payload=payload,
        step_id=step_id,
        step_sequence=step_sequence,
    )
