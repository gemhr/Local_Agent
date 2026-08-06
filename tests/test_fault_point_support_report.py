from __future__ import annotations

from core.runtime import (
    FaultAction,
    FaultPoint,
    FaultPointSupportStatus,
    build_fault_point_support_report,
)


def test_support_report_classifies_every_fault_point_from_real_seams():
    report = build_fault_point_support_report()

    assert report.total_fault_points == len(FaultPoint) == 53
    assert report.supported_count == 44
    assert report.contract_only_count == 9
    assert report.not_applicable_count == 0
    assert {entry.fault_point for entry in report.entries} == set(FaultPoint)


def test_supported_points_have_owner_location_actions_and_test_evidence():
    report = build_fault_point_support_report()
    supported = tuple(
        entry
        for entry in report.entries
        if entry.support_status is FaultPointSupportStatus.SUPPORTED
    )

    assert all(entry.physical_owner != "contract" for entry in supported)
    assert all(entry.physical_location != "no_runtime_seam" for entry in supported)
    assert all(entry.supported_actions for entry in supported)
    full = (
        FaultAction.RAISE_TYPED_ERROR,
        FaultAction.DELAY,
        FaultAction.BLOCK_UNTIL_RELEASED,
    )
    sync_seam_points = {
        FaultPoint.PLANNING_BEFORE_RESOLVE,
        FaultPoint.PLANNING_BEFORE_PLAN_CREATED,
        FaultPoint.STEP_BEFORE_DRIVER_EXECUTE,
        FaultPoint.STORE_BEFORE_WRITE_PREPARED,
        FaultPoint.STORE_BEFORE_MARK_READABLE,
        FaultPoint.STORE_BEFORE_DEPENDENCY_READ,
        FaultPoint.EXECUTOR_BEFORE_SUBMIT,
        FaultPoint.OUTPUT_BEFORE_PUBLISH,
        FaultPoint.MEMORY_BEFORE_EXCHANGE_BEGIN,
        FaultPoint.MEMORY_BEFORE_USER_INSERT,
        FaultPoint.MEMORY_BEFORE_ASSISTANT_INSERT,
        FaultPoint.MEMORY_BEFORE_EXCHANGE_COMMIT,
    }
    for entry in supported:
        if entry.fault_point in sync_seam_points:
            assert entry.supported_actions == (FaultAction.RAISE_TYPED_ERROR,)
        else:
            assert entry.supported_actions == full
    assert all(entry.test_ids for entry in supported)


def test_contract_only_points_are_not_counted_as_supported_coverage():
    report = build_fault_point_support_report()
    expected = {
        FaultPoint.MODEL_AFTER_PROVIDER_SUCCESS,
        FaultPoint.MODEL_BEFORE_USAGE_COMMIT,
        FaultPoint.MODEL_AFTER_USAGE_COMMIT,
        FaultPoint.TOOL_AFTER_SIDE_EFFECT_COMMIT,
        FaultPoint.RETRIEVAL_AFTER_REWRITE,
        FaultPoint.RETRIEVAL_AFTER_SEARCH,
        FaultPoint.RETRIEVAL_BEFORE_RESULT_COMMIT,
        FaultPoint.JOURNAL_BEFORE_READ,
        FaultPoint.EXECUTOR_AFTER_SUBMIT,
    }
    actual = {
        entry.fault_point
        for entry in report.entries
        if entry.support_status is FaultPointSupportStatus.CONTRACT_ONLY
    }

    assert actual == expected
    for point in expected:
        entry = report.entry_for(point)
        assert entry.supported_actions == ()
        assert entry.test_ids == ()
    assert (
        report.entry_for(FaultPoint.TOOL_AFTER_SIDE_EFFECT_COMMIT).notes_safe_code
        == "TOOL_COMMIT_CALLBACK_UNAVAILABLE"
    )


def test_support_report_is_safe_and_contains_no_paths_or_rule_ids():
    rendered = repr(build_fault_point_support_report())
    assert "C:\\" not in rendered
    assert "/tests/" not in rendered
    assert "rule_id" not in rendered
    assert "SECRET_PROMPT_TEXT" not in rendered
