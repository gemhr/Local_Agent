from __future__ import annotations

from core.runtime import (
    FaultTrigger,
    InMemoryRunEventJournal,
    RecoveryStatus,
    RecoveryValidator,
    RunRegistry,
)
from tests._recovery_fixtures import recovery_plan, recovery_snapshot


def test_recovery_capability_is_validation_only_not_execution_or_replay() -> None:
    assessment = RecoveryValidator(
        journal=InMemoryRunEventJournal()
    ).assess_snapshot(
        snapshot=recovery_snapshot(),
        current_plan=recovery_plan(),
    )

    assert assessment.status is RecoveryStatus.RESUMABLE
    assert assessment.resume_prerequisites_satisfied is True
    assert assessment.automatic_resume_supported is False
    assert assessment.model_replay_allowed is False
    assert assessment.tool_replay_allowed is False
    assert assessment.retrieval_replay_allowed is False
    assert assessment.output_reconstruction_supported is False


def test_random_chaos_and_cross_process_registry_are_not_implicit_capabilities() -> None:
    assert "RANDOM" not in {item.name for item in FaultTrigger}
    first = RunRegistry()
    second = RunRegistry()
    assert first is not second
    assert first.observability_snapshot()["active_runs"] == 0
    assert second.observability_snapshot()["active_runs"] == 0
