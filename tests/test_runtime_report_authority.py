from __future__ import annotations

from dataclasses import fields

import pytest

from core.runtime import (
    EventPublicationEvidence,
    FaultCoverageReport,
    FaultRuntimeInvariantReport,
    RecoveryValidator,
    RuntimeAdmissionState,
    RuntimeLifecycleState,
    ShutdownReport,
)
from tests._runtime_assembly_fixtures import make_services
from tests._tool_completion_gap_fixtures import ToolCompletionGapFixture


@pytest.mark.parametrize(
    "report_type",
    [ShutdownReport, FaultCoverageReport, FaultRuntimeInvariantReport],
)
def test_derived_reports_do_not_retain_live_runtime_owners(report_type) -> None:
    names = {item.name for item in fields(report_type)}

    assert not names.intersection(
        {"run_context", "agent_state", "event_channel", "run_registry", "services"}
    )


def test_frozen_publication_evidence_is_payload_free() -> None:
    names = {item.name for item in fields(EventPublicationEvidence)}

    assert not names.intersection(
        {"payload", "event", "prompt", "output", "arguments", "exception"}
    )


def test_test_oracle_is_rejected_by_production_recovery_validator() -> None:
    fixture = ToolCompletionGapFixture(
        True, False, False, False, None, "UNKNOWN", "OUTCOME_UNKNOWN", "EVIDENCE_LOST"
    )
    validator = RecoveryValidator(
        journal=make_services(snapshot_enabled=False).event_journal
    )

    with pytest.raises(TypeError, match="RunSnapshot"):
        validator.assess_snapshot(snapshot=fixture, current_plan=object())


def test_shutdown_completed_alias_does_not_mean_fully_closed() -> None:
    report = ShutdownReport(
        state=RuntimeAdmissionState.CLOSED,
        lifecycle_state=RuntimeLifecycleState.CLOSED,
        active_run_count=0,
        cancel_requested_count=0,
        cancelled_run_count=0,
        cancel_failed_count=0,
        gracefully_drained_count=0,
        forced_run_count=0,
        remaining_run_count=0,
        worker_drain_status="NOT_IDLE",
        active_worker_count=1,
        detached_worker_count=0,
        unknown_worker_count=0,
        observability_flush_status="COMPLETED",
        trace_flush_status="COMPLETED",
        duration_seconds=0,
        components=(),
    )

    assert report.completed is True
    assert report.orchestration_completed is True
    assert report.fully_closed is False
