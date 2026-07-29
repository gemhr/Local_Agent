from __future__ import annotations

from dataclasses import replace

import pytest

from core.runtime.journal_tail_reducer import LimitedJournalTailReducer
from core.runtime.recovery_contract import (
    ToolRecoveryDecisionStatus as Status,
    ToolRecoveryEvidence,
)
from core.runtime.state import AgentState
from core.runtime.tool_recovery import ToolRecoveryDecisionEngine
from tests._recovery_fixtures import recovery_snapshot


INVOCATION_A = "a" * 64
INVOCATION_B = "b" * 64
ATTEMPT_A = "c" * 64
ATTEMPT_B = "d" * 64
KEY = "e" * 64


def projection(*, step_succeeded: bool = False):
    state = AgentState("run")
    state.mark_running()
    state.add_step("step", "step")
    if step_succeeded:
        state.start_step("step")
        state.succeed_step("step")
    snapshot = recovery_snapshot(state=state)
    return LimitedJournalTailReducer.reduce(snapshot, ()).projection


def evidence(
    sequence: int,
    *,
    event_kind: str,
    invocation: str | None = INVOCATION_A,
    attempt: str | None = ATTEMPT_A,
    attempt_sequence: int = 0,
    side_effect_kind: str | None = "NONE",
    idempotency_kind: str | None = "READ_ONLY",
    key: str | None = None,
    replay_supported: bool | None = False,
    side_effect_state: str | None = "NOT_STARTED",
    compensation_state: str | None = "NOT_ATTEMPTED",
    retry_disposition: str | None = "UNSAFE",
    outcome: str | None = "PENDING",
    detached: bool = False,
    worker_terminated: bool = False,
    succeeded: bool | None = None,
    version: int | None = 1,
) -> ToolRecoveryEvidence:
    return ToolRecoveryEvidence(
        tool_name="writer",
        invocation_identity_digest=invocation,
        attempt_identity_digest=attempt,
        side_effect_kind=side_effect_kind,
        side_effect_state=side_effect_state,
        retry_disposition=retry_disposition,
        execution_detached=detached,
        worker_terminated=worker_terminated,
        safe_error_code=None,
        sequence=sequence,
        event_kind=event_kind,
        step_id="step",
        attempt_sequence=attempt_sequence,
        tool_evidence_schema_version=version,
        idempotency_kind=idempotency_kind,
        idempotency_key_digest=key,
        replay_supported=replay_supported,
        compensation_state=compensation_state,
        outcome_classification=outcome,
        provider_started=event_kind == "COMPLETED",
        succeeded=succeeded,
    )


def pair(**completed_changes):
    started = evidence(1, event_kind="STARTED")
    completed = replace(
        evidence(
            2,
            event_kind="COMPLETED",
            worker_terminated=True,
            succeeded=True,
            outcome="SUCCEEDED",
        ),
        **completed_changes,
    )
    return (started, completed)


def decide(items, *, step_succeeded=False):
    return ToolRecoveryDecisionEngine.decide(
        tuple(items), projection(step_succeeded=step_succeeded)
    )


def test_no_side_effect_completed_success_requires_no_action():
    result = decide(pair(), step_succeeded=True)
    assert result[0].status is Status.NO_ACTION_REQUIRED


def test_no_side_effect_incomplete_is_safe_candidate_but_never_automatic():
    result = decide((evidence(1, event_kind="STARTED"),))
    assert result[0].status is Status.SAFE_RETRY_CANDIDATE
    assert result[0].retry_candidate
    assert not result[0].automatic_action_allowed


@pytest.mark.parametrize(
    ("key", "replay_supported", "expected"),
    [
        (KEY, True, Status.SAFE_RETRY_CANDIDATE),
        (None, True, Status.MANUAL_RECONCILIATION),
        (KEY, False, Status.MANUAL_RECONCILIATION),
    ],
)
def test_idempotent_with_key_matrix(key, replay_supported, expected):
    item = evidence(
        1,
        event_kind="STARTED",
        side_effect_kind="EXTERNAL_STATE_MUTATION",
        idempotency_kind="IDEMPOTENT_WITH_KEY",
        key=key,
        replay_supported=replay_supported,
    )
    assert decide((item,))[0].status is expected


def test_non_idempotent_committed_and_completed_is_do_not_retry():
    items = pair(
        side_effect_kind="EXTERNAL_STATE_MUTATION",
        idempotency_kind="NON_IDEMPOTENT",
        side_effect_state="COMMITTED",
    )
    assert decide(items, step_succeeded=True)[0].status is Status.DO_NOT_RETRY


def test_non_idempotent_committed_without_completion_is_manual():
    item = evidence(
        1,
        event_kind="STARTED",
        side_effect_kind="EXTERNAL_STATE_MUTATION",
        idempotency_kind="NON_IDEMPOTENT",
        side_effect_state="COMMITTED",
    )
    assert decide((item,))[0].status is Status.MANUAL_RECONCILIATION


@pytest.mark.parametrize(
    "changes",
    [
        {"retry_disposition": "OUTCOME_UNKNOWN"},
        {"outcome_classification": "POST_COMMIT_RESPONSE_FAILURE"},
        {
            "execution_detached": True,
            "worker_terminated": False,
        },
        {
            "execution_detached": False,
            "worker_terminated": True,
            "side_effect_state": "UNKNOWN",
        },
        {"compensation_state": "FAILED"},
    ],
)
def test_unknown_detached_and_compensation_failures_require_manual(changes):
    item = replace(evidence(1, event_kind="STARTED"), **changes)
    assert decide((item,))[0].status is Status.MANUAL_RECONCILIATION


def test_legacy_incomplete_evidence_is_insufficient():
    item = evidence(
        1,
        event_kind="STARTED",
        version=None,
        side_effect_kind=None,
        idempotency_kind=None,
    )
    assert decide((item,))[0].status is Status.INSUFFICIENT_EVIDENCE


def test_same_tool_name_different_invocations_are_not_merged():
    items = (
        evidence(1, event_kind="STARTED", invocation=INVOCATION_A),
        evidence(
            2,
            event_kind="STARTED",
            invocation=INVOCATION_B,
            attempt=ATTEMPT_B,
        ),
    )
    decisions = decide(items)
    assert len(decisions) == 2


def test_multiple_attempts_under_one_invocation_are_siblings():
    items = (
        evidence(1, event_kind="STARTED", attempt=ATTEMPT_A),
        evidence(
            2,
            event_kind="COMPLETED",
            attempt=ATTEMPT_A,
            worker_terminated=True,
            succeeded=False,
            outcome="FAILED",
        ),
        evidence(
            3,
            event_kind="STARTED",
            attempt=ATTEMPT_B,
            attempt_sequence=1,
        ),
        evidence(
            4,
            event_kind="COMPLETED",
            attempt=ATTEMPT_B,
            attempt_sequence=1,
            worker_terminated=True,
            succeeded=True,
            outcome="SUCCEEDED",
        ),
    )
    decision = decide(items, step_succeeded=True)[0]
    assert decision.status is Status.NO_ACTION_REQUIRED
    assert decision.attempt_sequences == (0, 1)


def test_duplicate_completed_fails_closed():
    items = pair() + (
        replace(pair()[1], sequence=3),
    )
    assert decide(items)[0].status is Status.MANUAL_RECONCILIATION


def test_completed_without_started_fails_closed():
    completed = evidence(
        2,
        event_kind="COMPLETED",
        worker_terminated=True,
        succeeded=True,
        outcome="SUCCEEDED",
    )
    assert decide((completed,))[0].status is Status.MANUAL_RECONCILIATION


def test_missing_invocation_identity_is_never_joined_by_tool_name():
    items = (
        evidence(1, event_kind="STARTED", invocation=None),
        evidence(
            2,
            event_kind="COMPLETED",
            invocation=None,
            worker_terminated=True,
        ),
    )
    decisions = decide(items)
    assert len(decisions) == 2
    assert all(
        item.status is Status.INSUFFICIENT_EVIDENCE for item in decisions
    )
