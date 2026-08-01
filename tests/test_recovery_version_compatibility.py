from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

import core.runtime.checkpoint as checkpoint_module
from core.runtime import (
    CheckpointKind,
    CheckpointMode,
    CheckpointStatus,
    FaultPoint,
    InMemoryRunEventJournal,
    InMemorySnapshotStore,
    JournalRecord,
    RecoveryReason,
    RecoveryStatus,
    RecoveryValidator,
    RuntimeEventType,
    ToolStartedPayload,
)
from core.runtime.snapshot_serialization import snapshot_from_json, snapshot_to_json
from tests._recovery_fixtures import (
    NOW,
    recovery_plan,
    recovery_snapshot,
    runtime_event,
)
from tests._snapshot_recovery_fault_fixtures import operation_controller
from tests.test_checkpoint_integration import _coordinator


def test_current_snapshot_v1_round_trip_preserves_bytes_and_digest():
    snapshot = recovery_snapshot()
    encoded = snapshot_to_json(snapshot)
    decoded = snapshot_from_json(encoded)

    assert decoded.snapshot_schema_version == 1
    assert snapshot_to_json(decoded) == encoded
    assert decoded.payload_digest == snapshot.payload_digest
    assert decoded == snapshot


def test_unknown_high_snapshot_version_fails_before_current_field_interpretation():
    snapshot = recovery_snapshot()
    object.__setattr__(snapshot, "snapshot_schema_version", 999)
    result = RecoveryValidator(journal=InMemoryRunEventJournal()).assess_snapshot(
        snapshot=snapshot, current_plan=recovery_plan()
    )
    assert result.status is RecoveryStatus.INCOMPATIBLE_SCHEMA
    assert result.reasons == (RecoveryReason.SNAPSHOT_SCHEMA_UNSUPPORTED,)
    assert result.reduced_projection is None


def test_stored_v1_snapshot_is_not_filled_from_current_agent_state_or_rewritten():
    snapshot = recovery_snapshot()
    encoded_before = snapshot_to_json(snapshot)
    digest_before = snapshot.payload_digest
    store = InMemorySnapshotStore()
    store.save(snapshot)

    result = RecoveryValidator(
        snapshot_store=store, journal=InMemoryRunEventJournal()
    ).validate(snapshot_id=snapshot.snapshot_id, current_plan=recovery_plan())

    stored = store.get(snapshot.snapshot_id)
    assert result.status is RecoveryStatus.RESUMABLE
    assert stored is not None
    assert snapshot_to_json(stored) == encoded_before
    assert stored.payload_digest == digest_before
    assert len(store.list_for_run(snapshot.run_id, 10)) == 1


@pytest.mark.parametrize("event_schema_version", [1, 2])
def test_historical_event_v1_v2_without_new_tool_result_fields_stays_unknown(
    event_schema_version,
):
    event = replace(
        runtime_event(
            1,
            RuntimeEventType.TOOL_STARTED,
            ToolStartedPayload("legacy-writer"),
            step_id="step",
            step_sequence=1,
        ),
        schema_version=event_schema_version,
    )
    journal = InMemoryRunEventJournal()
    journal.append(event)

    result = RecoveryValidator(journal=journal).assess_snapshot(
        snapshot=recovery_snapshot(), current_plan=recovery_plan()
    )

    assert result.status is RecoveryStatus.REQUIRES_RECONCILIATION
    assert result.tool_evidence[0].tool_evidence_schema_version is None
    assert result.tool_evidence[0].side_effect_kind is None
    assert result.tool_evidence[0].idempotency_kind is None
    assert RecoveryReason.TOOL_EVIDENCE_INSUFFICIENT in result.reasons
    assert not result.tool_replay_allowed


class UnknownEventVersionJournal:
    def __init__(self):
        self.record = JournalRecord.from_event(
            runtime_event(
                1,
                RuntimeEventType.TOOL_STARTED,
                ToolStartedPayload("legacy-writer"),
            )
        )
        object.__setattr__(self.record, "event_schema_version", 999)

    def last_sequence(self, run_id):
        return 1

    def read_after(self, run_id, sequence, limit):
        return (self.record,)


def test_unknown_event_version_fails_closed_without_migration_writeback():
    journal = UnknownEventVersionJournal()
    result = RecoveryValidator(journal=journal).assess_snapshot(
        snapshot=recovery_snapshot(), current_plan=recovery_plan()
    )
    assert result.status is RecoveryStatus.UNSUPPORTED
    assert result.reasons == (RecoveryReason.EVENT_SCHEMA_UNSUPPORTED,)
    assert journal.record.event_schema_version == 999
    assert result.tool_evidence == ()


@pytest.mark.asyncio
async def test_disabled_controller_preserves_exact_snapshot_bytes_digest_and_row_count(
    monkeypatch,
):
    store = InMemorySnapshotStore()
    coordinator, _, _ = _coordinator(store)
    activity_provider = coordinator.checkpoint_coordinator.activity_provider
    fixed_activity = activity_provider.capture()
    activity_provider.capture = lambda: fixed_activity
    budget_ledger = coordinator.checkpoint_coordinator.budget_ledger
    fixed_budget = budget_ledger.snapshot()
    budget_ledger.snapshot = lambda: fixed_budget

    class FixedDateTime:
        @staticmethod
        def now(tz):
            return NOW

    monkeypatch.setattr(checkpoint_module, "datetime", FixedDateTime)
    monkeypatch.setattr(
        checkpoint_module,
        "uuid4",
        lambda: SimpleNamespace(hex="fixed-snapshot-id"),
    )

    normal = await coordinator.create_checkpoint(
        mode=CheckpointMode.REQUIRE_QUIESCENT,
        checkpoint_kind=CheckpointKind.STEP_BOUNDARY,
        timeout=1,
    )
    normal_snapshot = store.get(normal.snapshot_id)
    controller = operation_controller(FaultPoint.SNAPSHOT_BEFORE_SAVE, enabled=False)
    disabled = await coordinator.create_checkpoint(
        mode=CheckpointMode.REQUIRE_QUIESCENT,
        checkpoint_kind=CheckpointKind.STEP_BOUNDARY,
        timeout=1,
        fault_controller=controller,
    )
    disabled_snapshot = store.get(disabled.snapshot_id)

    assert normal.status is CheckpointStatus.SAVED
    assert disabled.status is CheckpointStatus.SAVED
    assert snapshot_to_json(disabled_snapshot) == snapshot_to_json(normal_snapshot)
    assert disabled_snapshot.payload_digest == normal_snapshot.payload_digest
    assert normal.snapshot_publication_evidence.snapshot_version is None
    assert disabled.snapshot_publication_evidence.snapshot_version is None
    assert len(store.list_for_run("run", 10)) == 1
    counter = controller.snapshot().counters[0]
    assert (counter.match_count, counter.hit_count) == (0, 0)
