from __future__ import annotations

import json
import sqlite3

import pytest

from core.runtime import (
    FaultAction,
    FaultMatchContext,
    FaultPoint,
    InMemoryRunEventJournal,
    RecoveryReason,
    RecoveryStatus,
    RecoveryValidator,
    SQLiteSnapshotStore,
)
from tests._recovery_fixtures import recovery_plan, recovery_snapshot
from tests._snapshot_recovery_fault_fixtures import operation_controller


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "expected_status", "expected_reason"),
    [
        ("digest_mismatch", RecoveryStatus.CORRUPTED, RecoveryReason.SNAPSHOT_DIGEST_INVALID),
        ("unknown_schema", RecoveryStatus.INCOMPATIBLE_SCHEMA, RecoveryReason.SNAPSHOT_SCHEMA_UNSUPPORTED),
        ("missing_field", RecoveryStatus.CORRUPTED, RecoveryReason.SNAPSHOT_DIGEST_INVALID),
        ("run_id_mismatch", RecoveryStatus.CORRUPTED, RecoveryReason.SNAPSHOT_DIGEST_INVALID),
        ("snapshot_id_mismatch", RecoveryStatus.CORRUPTED, RecoveryReason.SNAPSHOT_DIGEST_INVALID),
        ("agent_status_invalid", RecoveryStatus.CORRUPTED, RecoveryReason.SNAPSHOT_DIGEST_INVALID),
        ("step_status_invalid", RecoveryStatus.CORRUPTED, RecoveryReason.SNAPSHOT_DIGEST_INVALID),
        ("budget_invalid", RecoveryStatus.CORRUPTED, RecoveryReason.SNAPSHOT_DIGEST_INVALID),
        ("tool_activity_invalid", RecoveryStatus.CORRUPTED, RecoveryReason.SNAPSHOT_DIGEST_INVALID),
        ("truncated_payload", RecoveryStatus.CORRUPTED, RecoveryReason.SNAPSHOT_DIGEST_INVALID),
        ("sqlite_row_digest", RecoveryStatus.CORRUPTED, RecoveryReason.SNAPSHOT_DIGEST_INVALID),
    ],
)
async def test_test_only_snapshot_corruption_fixtures_fail_closed(
    tmp_path, mutation, expected_status, expected_reason
):
    path = tmp_path / f"{mutation}.db"
    snapshot = recovery_snapshot()
    store = SQLiteSnapshotStore(str(path))
    store.save(snapshot)
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT payload_json FROM runtime_snapshots WHERE snapshot_id = ?",
            (snapshot.snapshot_id,),
        ).fetchone()
        box = {"payload": json.loads(row[0]), "raw": row[0]}

        def mutate_test_fixture(_: str) -> None:
            payload = box["payload"]
            if mutation == "digest_mismatch":
                payload["payload_digest"] = "0" * 64
            elif mutation == "unknown_schema":
                payload["snapshot_schema_version"] = 999
            elif mutation == "missing_field":
                payload.pop("run_status")
            elif mutation == "run_id_mismatch":
                payload["run_id"] = "other-run"
            elif mutation == "snapshot_id_mismatch":
                payload["snapshot_id"] = "other-snapshot"
            elif mutation == "agent_status_invalid":
                payload["state_snapshot"]["run_status"] = "INVALID"
            elif mutation == "step_status_invalid":
                payload["state_snapshot"]["step_states"][0]["status"] = "INVALID"
            elif mutation == "budget_invalid":
                payload["budget_snapshot"]["reservation_count"] = -1
            elif mutation == "tool_activity_invalid":
                payload["activity_snapshot"]["tool_attempts_active"] = -1
            elif mutation == "truncated_payload":
                box["raw"] = box["raw"][: len(box["raw"]) // 2]
            elif mutation == "sqlite_row_digest":
                return
            if mutation != "truncated_payload":
                box["raw"] = json.dumps(payload)

        controller = operation_controller(
            FaultPoint.SNAPSHOT_BEFORE_READ,
            action=FaultAction.CORRUPT_TEST_FIXTURE,
            fixture_mutator=mutate_test_fixture,
        )
        await controller.execute_if_matched(
            FaultMatchContext(
                fault_point=FaultPoint.SNAPSHOT_BEFORE_READ,
                component="recovery_validator",
                operation_kind="SNAPSHOT_READ",
            ),
            allowed_actions={FaultAction.CORRUPT_TEST_FIXTURE},
        )
        if mutation == "unknown_schema":
            connection.execute(
                "UPDATE runtime_snapshots SET snapshot_schema_version = 999, payload_json = ? WHERE snapshot_id = ?",
                (box["raw"], snapshot.snapshot_id),
            )
        elif mutation == "sqlite_row_digest":
            connection.execute(
                "UPDATE runtime_snapshots SET payload_digest = ? WHERE snapshot_id = ?",
                ("f" * 64, snapshot.snapshot_id),
            )
        else:
            connection.execute(
                "UPDATE runtime_snapshots SET payload_json = ? WHERE snapshot_id = ?",
                (box["raw"], snapshot.snapshot_id),
            )

    assessment = RecoveryValidator(
        snapshot_store=store,
        journal=InMemoryRunEventJournal(),
    ).validate(snapshot_id=snapshot.snapshot_id, current_plan=recovery_plan())
    assert assessment.status is expected_status
    assert assessment.reasons == (expected_reason,)
    combined = repr(assessment) + " ".join(assessment.reason_texts())
    assert "raw-snapshot-payload" not in combined
    assert str(path) not in combined
    counter = controller.snapshot().counters[0]
    assert (counter.match_count, counter.hit_count) == (1, 1)
    store.close()
