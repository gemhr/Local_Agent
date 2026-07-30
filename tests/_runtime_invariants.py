from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from core.runtime import RuntimeEvent, RuntimeEventType


_BUSINESS_EVENT_TYPES = frozenset(
    {
        RuntimeEventType.OUTPUT_DELTA,
        RuntimeEventType.MODEL_STARTED,
        RuntimeEventType.MODEL_COMPLETED,
        RuntimeEventType.TOOL_STARTED,
        RuntimeEventType.TOOL_COMPLETED,
        RuntimeEventType.RETRIEVAL_STARTED,
        RuntimeEventType.RETRIEVAL_COMPLETED,
        RuntimeEventType.STEP_STARTED,
        RuntimeEventType.STEP_COMPLETED,
    }
)


@dataclass(frozen=True, slots=True)
class RuntimeInvariantReport:
    """Derived test diagnostics; never owns or retains live runtime state."""

    runtime_selection_count: int
    run_context_count: int
    cancellation_source_count: int
    channel_count: int
    sequence_owner_count: int
    registry_registration_count: int
    root_span_count: int
    terminal_count: int
    post_terminal_business_event_count: int
    active_registry_count: int
    active_channel_count: int
    active_span_count: int
    pending_watcher_count: int
    pending_producer_count: int

    @property
    def violations(self) -> tuple[str, ...]:
        expected_one = {
            "runtime_selection": self.runtime_selection_count,
            "run_context": self.run_context_count,
            "cancellation_source": self.cancellation_source_count,
            "channel": self.channel_count,
            "sequence_owner": self.sequence_owner_count,
            "registry_registration": self.registry_registration_count,
            "root_span": self.root_span_count,
            "terminal": self.terminal_count,
        }
        violations = [
            f"{name}_count"
            for name, value in expected_one.items()
            if value != 1
        ]
        expected_zero = {
            "post_terminal_business_event": (
                self.post_terminal_business_event_count
            ),
            "active_registry": self.active_registry_count,
            "active_channel": self.active_channel_count,
            "active_span": self.active_span_count,
            "pending_watcher": self.pending_watcher_count,
            "pending_producer": self.pending_producer_count,
        }
        violations.extend(
            f"{name}_count"
            for name, value in expected_zero.items()
            if value != 0
        )
        return tuple(violations)

    @property
    def valid(self) -> bool:
        return not self.violations

    def assert_valid(self) -> None:
        assert self.valid, self.violations


def build_runtime_invariant_report(
    events: Iterable[RuntimeEvent],
    *,
    runtime_selection_count: int = 1,
    run_context_count: int = 1,
    cancellation_source_count: int = 1,
    channel_count: int = 1,
    sequence_owner_count: int = 1,
    registry_registration_count: int = 1,
    root_span_count: int = 1,
    active_registry_count: int = 0,
    active_channel_count: int = 0,
    active_span_count: int = 0,
    pending_watcher_count: int = 0,
    pending_producer_count: int = 0,
) -> RuntimeInvariantReport:
    materialized = tuple(events)
    terminals = [
        index
        for index, event in enumerate(materialized)
        if event.event_type is RuntimeEventType.RUN_COMPLETED
    ]
    terminal_index = terminals[0] if terminals else len(materialized)
    post_terminal_business = sum(
        event.event_type in _BUSINESS_EVENT_TYPES
        for event in materialized[terminal_index + 1 :]
    )
    return RuntimeInvariantReport(
        runtime_selection_count=runtime_selection_count,
        run_context_count=run_context_count,
        cancellation_source_count=cancellation_source_count,
        channel_count=channel_count,
        sequence_owner_count=sequence_owner_count,
        registry_registration_count=registry_registration_count,
        root_span_count=root_span_count,
        terminal_count=len(terminals),
        post_terminal_business_event_count=post_terminal_business,
        active_registry_count=active_registry_count,
        active_channel_count=active_channel_count,
        active_span_count=active_span_count,
        pending_watcher_count=pending_watcher_count,
        pending_producer_count=pending_producer_count,
    )
