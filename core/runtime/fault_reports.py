"""Safe immutable support, coverage, and invariant reports for Day 24."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re

from core.runtime.fault_injection_contract import (
    DANGEROUS_FAULT_POINTS,
    FaultAction,
    FaultPoint,
)


_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_SAFE_TOKEN = re.compile(r"^[a-z][a-z0-9_.]{0,127}$")
_SUPPORTED_ACTIONS = (
    FaultAction.RAISE_TYPED_ERROR,
    FaultAction.DELAY,
    FaultAction.BLOCK_UNTIL_RELEASED,
)
_SYNC_SEAM_ACTIONS = (FaultAction.RAISE_TYPED_ERROR,)
_SYNC_SEAM_POINTS = frozenset(
    {
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
)


class FaultPointSupportStatus(str, Enum):
    SUPPORTED = "SUPPORTED"
    CONTRACT_ONLY = "CONTRACT_ONLY"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True, slots=True)
class FaultPointSupport:
    fault_point: FaultPoint
    support_status: FaultPointSupportStatus
    physical_owner: str
    physical_location: str
    dangerous_window: bool
    supported_actions: tuple[FaultAction, ...]
    test_ids: tuple[str, ...]
    notes_safe_code: str

    def __post_init__(self) -> None:
        if not isinstance(self.fault_point, FaultPoint):
            raise TypeError("fault_point must be FaultPoint")
        if not isinstance(self.support_status, FaultPointSupportStatus):
            raise TypeError("support_status must be FaultPointSupportStatus")
        for value, name in (
            (self.physical_owner, "physical_owner"),
            (self.physical_location, "physical_location"),
        ):
            if not isinstance(value, str) or _SAFE_TOKEN.fullmatch(value) is None:
                raise ValueError(f"{name} must be a safe token")
        if type(self.dangerous_window) is not bool:
            raise TypeError("dangerous_window must be bool")
        if self.dangerous_window != (
            self.fault_point in DANGEROUS_FAULT_POINTS
        ):
            raise ValueError("dangerous_window must match the contract")
        if any(not isinstance(action, FaultAction) for action in self.supported_actions):
            raise TypeError("supported_actions must contain FaultAction")
        if any(
            not isinstance(test_id, str)
            or _SAFE_TOKEN.fullmatch(test_id) is None
            for test_id in self.test_ids
        ):
            raise ValueError("test_ids must contain safe logical identifiers")
        if (
            not isinstance(self.notes_safe_code, str)
            or _SAFE_CODE.fullmatch(self.notes_safe_code) is None
        ):
            raise ValueError("notes_safe_code must be a fixed safe code")
        if self.support_status is FaultPointSupportStatus.SUPPORTED:
            if not self.supported_actions or not self.test_ids:
                raise ValueError("supported points require actions and test evidence")
        elif self.supported_actions or self.test_ids:
            raise ValueError("non-supported points cannot claim actions or tests")


@dataclass(frozen=True, slots=True)
class FaultPointSupportReport:
    entries: tuple[FaultPointSupport, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.entries, tuple):
            object.__setattr__(self, "entries", tuple(self.entries))
        points = tuple(entry.fault_point for entry in self.entries)
        if len(points) != len(set(points)):
            raise ValueError("fault points must be unique")
        if set(points) != set(FaultPoint):
            raise ValueError("support report must classify every FaultPoint")

    @property
    def total_fault_points(self) -> int:
        return len(self.entries)

    def count(self, status: FaultPointSupportStatus) -> int:
        return sum(entry.support_status is status for entry in self.entries)

    @property
    def supported_count(self) -> int:
        return self.count(FaultPointSupportStatus.SUPPORTED)

    @property
    def contract_only_count(self) -> int:
        return self.count(FaultPointSupportStatus.CONTRACT_ONLY)

    @property
    def not_applicable_count(self) -> int:
        return self.count(FaultPointSupportStatus.NOT_APPLICABLE)

    def entry_for(self, point: FaultPoint) -> FaultPointSupport:
        return next(entry for entry in self.entries if entry.fault_point is point)


@dataclass(frozen=True, slots=True)
class FaultCoverageReport:
    total_fault_points: int
    supported_count: int
    contract_only_count: int
    not_applicable_count: int
    tested_supported_count: int
    untested_supported_count: int
    dangerous_supported_count: int
    disabled_parity_covered: bool
    cancellation_covered: bool
    concurrency_covered: bool
    partial_persistence_covered: bool
    security_covered: bool

    @property
    def fully_covered(self) -> bool:
        return (
            self.untested_supported_count == 0
            and self.tested_supported_count == self.supported_count
            and self.disabled_parity_covered
            and self.cancellation_covered
            and self.concurrency_covered
            and self.partial_persistence_covered
            and self.security_covered
        )


@dataclass(frozen=True, slots=True)
class FaultRuntimeInvariantReport:
    runtime_selection_count: int
    run_context_count: int
    cancellation_source_count: int
    event_channel_count: int
    sequence_owner_count: int
    registry_registration_count: int
    root_span_count: int
    terminal_owner_count: int
    business_rerun_count: int
    cross_runtime_fallback_count: int
    automatic_compensation_count: int
    automatic_recovery_action_count: int
    terminal_journal_count: int
    terminal_channel_count: int
    sequence_reuse_count: int
    active_span_count: int
    registry_handle_count: int
    pending_watcher_count: int
    request_producer_count: int
    active_reservation_count: int
    active_permit_count: int
    detached_worker_count: int
    violation_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            if name == "violation_codes":
                continue
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if any(
            not isinstance(code, str) or _SAFE_CODE.fullmatch(code) is None
            for code in self.violation_codes
        ):
            raise ValueError("violation_codes must be fixed safe codes")

    @property
    def passed(self) -> bool:
        return not self.violation_codes


def build_fault_runtime_invariant_report(
    *,
    runtime_selection_count: int = 1,
    run_context_count: int = 1,
    cancellation_source_count: int = 1,
    event_channel_count: int = 1,
    sequence_owner_count: int = 1,
    registry_registration_count: int = 1,
    root_span_count: int = 1,
    terminal_owner_count: int = 1,
    business_rerun_count: int = 0,
    cross_runtime_fallback_count: int = 0,
    automatic_compensation_count: int = 0,
    automatic_recovery_action_count: int = 0,
    terminal_journal_count: int = 1,
    terminal_channel_count: int = 1,
    sequence_reuse_count: int = 0,
    active_span_count: int = 0,
    registry_handle_count: int = 0,
    pending_watcher_count: int = 0,
    request_producer_count: int = 0,
    active_reservation_count: int = 0,
    active_permit_count: int = 0,
    detached_worker_count: int = 0,
) -> FaultRuntimeInvariantReport:
    """Build a value-only audit report from already-derived counters."""

    values = locals()
    violations: list[str] = []
    expected_one = (
        "runtime_selection_count",
        "run_context_count",
        "cancellation_source_count",
        "event_channel_count",
        "sequence_owner_count",
        "registry_registration_count",
        "root_span_count",
        "terminal_owner_count",
    )
    for name in expected_one:
        if values[name] != 1:
            violations.append(f"FAULT_INVARIANT_{name.upper()}")
    expected_zero = (
        "business_rerun_count",
        "cross_runtime_fallback_count",
        "automatic_compensation_count",
        "automatic_recovery_action_count",
        "sequence_reuse_count",
        "active_span_count",
        "registry_handle_count",
        "pending_watcher_count",
        "request_producer_count",
        "active_reservation_count",
        "active_permit_count",
    )
    for name in expected_zero:
        if values[name] != 0:
            violations.append(f"FAULT_INVARIANT_{name.upper()}")
    for name in ("terminal_journal_count", "terminal_channel_count"):
        if values[name] > 1:
            violations.append(f"FAULT_INVARIANT_{name.upper()}")
    return FaultRuntimeInvariantReport(
        **values,
        violation_codes=tuple(violations),
    )


_SUPPORTED: dict[FaultPoint, tuple[str, str, tuple[str, ...]]] = {
    FaultPoint.MODEL_BEFORE_INVOCATION: ("model_router", "before_invocation", ("model_fault_injection",)),
    FaultPoint.MODEL_BEFORE_PROVIDER_CALL: ("model_router", "before_provider_call", ("model_fault_injection",)),
    FaultPoint.TOOL_BEFORE_INVOCATION: ("tool_service", "before_invocation", ("tool_fault_injection",)),
    FaultPoint.TOOL_BEFORE_ATTEMPT: ("tool_service", "before_attempt", ("tool_fault_injection_retry",)),
    FaultPoint.TOOL_BEFORE_PROVIDER_CALL: ("tool_attempt", "before_provider_call", ("tool_provider_boundary_fault",)),
    FaultPoint.TOOL_AFTER_PROVIDER_RETURN: ("tool_attempt", "after_provider_return", ("tool_post_provider_fault",)),
    FaultPoint.TOOL_BEFORE_SIDE_EFFECT_COMMIT: ("tool_adapter", "before_side_effect_commit", ("tool_side_effect_boundary",)),
    FaultPoint.TOOL_AFTER_AUTHORITATIVE_SIDE_EFFECT_RESOLUTION: ("tool_attempt", "after_side_effect_resolution", ("tool_post_commit_fault",)),
    FaultPoint.TOOL_BEFORE_COMPLETION_EVENT: ("tool_service", "before_completion_event", ("tool_completion_publication_fault",)),
    FaultPoint.RETRIEVAL_BEFORE_REWRITE: ("retrieval_service", "before_rewrite", ("retrieval_fault_injection",)),
    FaultPoint.RETRIEVAL_BEFORE_SEARCH: ("retrieval_service", "before_search", ("retrieval_fault_injection",)),
    FaultPoint.EVENT_BEFORE_JOURNAL_APPEND: ("event_channel", "before_journal_append", ("event_fault_injection",)),
    FaultPoint.EVENT_AFTER_JOURNAL_APPEND: ("event_channel", "after_journal_append", ("event_fault_injection",)),
    FaultPoint.EVENT_BEFORE_CHANNEL_ENQUEUE: ("event_channel", "before_channel_enqueue", ("event_fault_injection",)),
    FaultPoint.JOURNAL_BEFORE_TERMINAL_APPEND: ("event_channel", "before_terminal_append", ("journal_fault_injection",)),
    FaultPoint.CHANNEL_BEFORE_RECEIVE: ("event_channel", "before_receive", ("channel_fault_injection",)),
    FaultPoint.CHANNEL_BEFORE_DRAIN_HANDOFF: ("event_channel", "before_drain_handoff", ("channel_fault_injection",)),
    FaultPoint.SNAPSHOT_BEFORE_SAVE: ("checkpoint_coordinator", "before_snapshot_save", ("snapshot_fault_injection",)),
    FaultPoint.SNAPSHOT_AFTER_SAVE: ("checkpoint_coordinator", "after_snapshot_save", ("snapshot_partial_persistence",)),
    FaultPoint.SNAPSHOT_BEFORE_READ: ("recovery_validator", "before_snapshot_read", ("recovery_fault_injection",)),
    FaultPoint.RECOVERY_BEFORE_TAIL_READ: ("recovery_validator", "before_tail_read", ("recovery_fault_injection",)),
    FaultPoint.RECOVERY_AFTER_TAIL_READ: ("recovery_validator", "after_tail_read", ("recovery_fault_injection",)),
    FaultPoint.OBSERVABILITY_BEFORE_RECORD: ("observability_dispatcher", "before_record", ("observability_fault_injection",)),
    FaultPoint.OBSERVABILITY_BEFORE_FLUSH: ("observability_dispatcher", "before_flush", ("observability_flush_fault",)),
    FaultPoint.TRACE_BEFORE_SPAN_START: ("trace_recorder", "before_span_start", ("trace_fault_injection",)),
    FaultPoint.TRACE_BEFORE_SPAN_END: ("trace_recorder", "before_span_end", ("trace_lifecycle_fault",)),
    FaultPoint.TRACE_BEFORE_FLUSH: ("trace_recorder", "before_flush", ("trace_lifecycle_fault",)),
    FaultPoint.SHUTDOWN_BEFORE_RUN_CANCEL: ("shutdown_coordinator", "before_run_cancel", ("shutdown_run_cancel_fault",)),
    FaultPoint.SHUTDOWN_BEFORE_WORKER_DRAIN: ("shutdown_coordinator", "before_worker_drain", ("shutdown_worker_drain_fault",)),
    FaultPoint.SHUTDOWN_BEFORE_JOURNAL_CLOSE: ("application_services", "before_journal_close", ("shutdown_component_fault",)),
    FaultPoint.SHUTDOWN_BEFORE_MODEL_CLOSE: ("application_services", "before_model_close", ("shutdown_component_fault",)),
    FaultPoint.SHUTDOWN_COMPONENT_CLOSE: ("application_services", "before_component_close", ("shutdown_component_fault",)),
    FaultPoint.PLANNING_BEFORE_RESOLVE: ("run_coordinator", "before_plan_resolve", ("stage2_5_wp6_planning_faults",)),
    FaultPoint.PLANNING_BEFORE_PLAN_CREATED: ("run_coordinator", "before_plan_created_event", ("stage2_5_wp6_planning_faults",)),
    FaultPoint.STEP_BEFORE_DRIVER_EXECUTE: ("multi_agent_driver", "before_driver_execute", ("stage2_5_wp6_execution_faults",)),
    FaultPoint.STORE_BEFORE_WRITE_PREPARED: ("step_result_store", "before_write_prepared", ("stage2_5_wp6_execution_faults",)),
    FaultPoint.STORE_BEFORE_MARK_READABLE: ("step_result_store", "before_mark_readable", ("stage2_5_wp6_execution_faults",)),
    FaultPoint.STORE_BEFORE_DEPENDENCY_READ: ("step_result_store", "before_dependency_read", ("stage2_5_wp6_execution_faults",)),
    FaultPoint.EXECUTOR_BEFORE_SUBMIT: ("parallel_executor", "before_executor_submit", ("stage2_5_wp6_execution_faults", "stage2_5_wp6_starvation")),
    FaultPoint.OUTPUT_BEFORE_PUBLISH: ("output_gate", "before_output_publish", ("stage2_5_wp6_delivery_faults",)),
    FaultPoint.MEMORY_BEFORE_EXCHANGE_BEGIN: ("memory_manager", "before_exchange_begin", ("stage2_5_wp6_memory_faults",)),
    FaultPoint.MEMORY_BEFORE_USER_INSERT: ("memory_manager", "before_user_insert", ("stage2_5_wp6_memory_faults",)),
    FaultPoint.MEMORY_BEFORE_ASSISTANT_INSERT: ("memory_manager", "before_assistant_insert", ("stage2_5_wp6_memory_faults",)),
    FaultPoint.MEMORY_BEFORE_EXCHANGE_COMMIT: ("memory_manager", "before_exchange_commit", ("stage2_5_wp6_memory_faults",)),
}

_CONTRACT_ONLY: dict[FaultPoint, str] = {
    FaultPoint.MODEL_AFTER_PROVIDER_SUCCESS: "MODEL_AFTER_SUCCESS_SEAM_UNWIRED",
    FaultPoint.MODEL_BEFORE_USAGE_COMMIT: "MODEL_USAGE_COMMIT_SEAM_UNWIRED",
    FaultPoint.MODEL_AFTER_USAGE_COMMIT: "MODEL_USAGE_COMMIT_SEAM_UNWIRED",
    FaultPoint.TOOL_AFTER_SIDE_EFFECT_COMMIT: "TOOL_COMMIT_CALLBACK_UNAVAILABLE",
    FaultPoint.RETRIEVAL_AFTER_REWRITE: "RETRIEVAL_AFTER_REWRITE_SEAM_UNWIRED",
    FaultPoint.RETRIEVAL_AFTER_SEARCH: "RETRIEVAL_AFTER_SEARCH_SEAM_UNWIRED",
    FaultPoint.RETRIEVAL_BEFORE_RESULT_COMMIT: "RETRIEVAL_RESULT_COMMIT_SEAM_UNWIRED",
    FaultPoint.JOURNAL_BEFORE_READ: "JOURNAL_GENERIC_READ_SEAM_UNWIRED",
    FaultPoint.EXECUTOR_AFTER_SUBMIT: "EXECUTOR_SUBMIT_SEAM_UNWIRED",
}

_CROSS_CUTTING_TEST_EVIDENCE = {
    "disabled_parity": ("fault_disabled_full_parity",),
    "cancellation": ("shutdown_cancellation_reentry",),
    "concurrency": ("fault_chaos_matrix",),
    "partial_persistence": ("event_fault_injection", "snapshot_partial_persistence"),
    "security": ("fault_security_final",),
}


def build_fault_point_support_report() -> FaultPointSupportReport:
    entries: list[FaultPointSupport] = []
    for point in FaultPoint:
        supported = _SUPPORTED.get(point)
        if supported is not None:
            owner, location, test_ids = supported
            entries.append(
                FaultPointSupport(
                    fault_point=point,
                    support_status=FaultPointSupportStatus.SUPPORTED,
                    physical_owner=owner,
                    physical_location=location,
                    dangerous_window=point in DANGEROUS_FAULT_POINTS,
                    supported_actions=(
                        _SYNC_SEAM_ACTIONS
                        if point in _SYNC_SEAM_POINTS
                        else _SUPPORTED_ACTIONS
                    ),
                    test_ids=test_ids,
                    notes_safe_code="RUNTIME_SEAM_TESTED",
                )
            )
            continue
        entries.append(
            FaultPointSupport(
                fault_point=point,
                support_status=FaultPointSupportStatus.CONTRACT_ONLY,
                physical_owner="contract",
                physical_location="no_runtime_seam",
                dangerous_window=point in DANGEROUS_FAULT_POINTS,
                supported_actions=(),
                test_ids=(),
                notes_safe_code=_CONTRACT_ONLY[point],
            )
        )
    return FaultPointSupportReport(tuple(entries))


def build_fault_coverage_report(
    support: FaultPointSupportReport | None = None,
) -> FaultCoverageReport:
    active = support or build_fault_point_support_report()
    supported = tuple(
        entry
        for entry in active.entries
        if entry.support_status is FaultPointSupportStatus.SUPPORTED
    )
    tested = tuple(entry for entry in supported if entry.test_ids)
    return FaultCoverageReport(
        total_fault_points=active.total_fault_points,
        supported_count=active.supported_count,
        contract_only_count=active.contract_only_count,
        not_applicable_count=active.not_applicable_count,
        tested_supported_count=len(tested),
        untested_supported_count=len(supported) - len(tested),
        dangerous_supported_count=sum(entry.dangerous_window for entry in supported),
        disabled_parity_covered=bool(
            _CROSS_CUTTING_TEST_EVIDENCE["disabled_parity"]
        ),
        cancellation_covered=bool(
            _CROSS_CUTTING_TEST_EVIDENCE["cancellation"]
        ),
        concurrency_covered=bool(
            _CROSS_CUTTING_TEST_EVIDENCE["concurrency"]
        ),
        partial_persistence_covered=bool(
            _CROSS_CUTTING_TEST_EVIDENCE["partial_persistence"]
        ),
        security_covered=bool(
            _CROSS_CUTTING_TEST_EVIDENCE["security"]
        ),
    )


__all__ = [
    "FaultCoverageReport",
    "FaultPointSupport",
    "FaultPointSupportReport",
    "FaultPointSupportStatus",
    "FaultRuntimeInvariantReport",
    "build_fault_runtime_invariant_report",
    "build_fault_coverage_report",
    "build_fault_point_support_report",
]
