from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from core.runtime import (
    FAULT_PLAN_SCHEMA_VERSION,
    FaultPlan,
    JournalRecord,
    RuntimeEvent,
    RuntimeEventType,
    ToolCompletedPayload,
)
from core.runtime.snapshot_serialization import canonical_json, snapshot_from_json
from tests._recovery_fixtures import runtime_event
from tests.test_snapshot_contract import make_snapshot


def test_unknown_event_journal_snapshot_and_fault_plan_versions_fail_closed() -> None:
    event = runtime_event(
        1,
        RuntimeEventType.TOOL_COMPLETED,
        ToolCompletedPayload("tool", True),
    )
    with pytest.raises(ValueError, match="schema_version"):
        replace(event, schema_version=999)

    record = JournalRecord.from_event(event)
    with pytest.raises(ValueError, match="journal_schema_version"):
        replace(record, journal_schema_version=999)

    snapshot_payload = make_snapshot().to_payload()
    snapshot_payload["snapshot_schema_version"] = 999
    with pytest.raises(ValueError):
        snapshot_from_json(canonical_json(snapshot_payload))

    with pytest.raises(ValueError, match="FaultPlan schema_version"):
        FaultPlan(
            "plan",
            (),
            schema_version=FAULT_PLAN_SCHEMA_VERSION + 1,
            created_at=datetime(2026, 8, 1, tzinfo=UTC),
        )


def test_known_event_versions_and_missing_legacy_evidence_remain_compatible() -> None:
    current = runtime_event(
        1,
        RuntimeEventType.TOOL_COMPLETED,
        ToolCompletedPayload("tool", True),
    )
    legacy = replace(current, schema_version=1)

    assert isinstance(legacy, RuntimeEvent)
    assert legacy.schema_version == 1
    assert legacy.payload.tool_evidence_schema_version is None
    assert legacy.payload.result_present is None


def test_persistent_canonicalization_is_key_order_stable_not_repr_based() -> None:
    left = canonical_json({"b": [2, 3], "a": 1})
    right = canonical_json({"a": 1, "b": [2, 3]})

    assert left == right
    assert left == '{"a":1,"b":[2,3]}'
