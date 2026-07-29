from __future__ import annotations

from dataclasses import replace

import pytest

from core.runtime.event_journal import JournalError, JournalErrorCode
from core.runtime.event_journal_store import InMemoryRunEventJournal
from core.runtime.events import (
    TOOL_EVIDENCE_SCHEMA_VERSION,
    RuntimeEventType,
    ToolCompletedPayload,
    ToolStartedPayload,
    validate_journal_payload,
)
from core.runtime.journal_tail_reducer import LimitedJournalTailReducer
from core.runtime.tool_contract import safe_key_digest
from tests._recovery_fixtures import (
    recovery_snapshot,
    runtime_event,
)


def versioned_started(*, side_effect_kind="NONE"):
    return ToolStartedPayload(
        tool_name="writer",
        retry_index=0,
        tool_evidence_schema_version=TOOL_EVIDENCE_SCHEMA_VERSION,
        invocation_identity_digest=safe_key_digest("invocation"),
        attempt_identity_digest=safe_key_digest("attempt"),
        side_effect_kind=side_effect_kind,
        idempotency_kind="IDEMPOTENT_WITH_KEY",
        idempotency_key_digest=safe_key_digest("key"),
        replay_supported=True,
        side_effect_state="NOT_STARTED",
        compensation_state="NOT_ATTEMPTED",
        retry_disposition="PENDING",
        outcome_classification="PENDING",
        execution_detached=False,
        worker_terminated=False,
        provider_started=False,
    )


def versioned_completed():
    return ToolCompletedPayload(
        tool_name="writer",
        succeeded=True,
        retry_index=0,
        side_effect_state="COMMITTED",
        retry_disposition="UNSAFE",
        worker_terminated=True,
        execution_detached=False,
        duration_ms=1,
        status="SUCCEEDED",
        tool_evidence_schema_version=TOOL_EVIDENCE_SCHEMA_VERSION,
        invocation_identity_digest=safe_key_digest("invocation"),
        attempt_identity_digest=safe_key_digest("attempt"),
        side_effect_kind="EXTERNAL_STATE_MUTATION",
        idempotency_kind="IDEMPOTENT_WITH_KEY",
        idempotency_key_digest=safe_key_digest("key"),
        replay_supported=True,
        compensation_state="NOT_ATTEMPTED",
        outcome_classification="SUCCEEDED",
        provider_started=True,
    )


def test_versioned_tool_event_persists_only_safe_recovery_evidence():
    event = runtime_event(
        1, RuntimeEventType.TOOL_STARTED, versioned_started()
    )
    payload = event.to_journal_dict()["safe_payload"]
    safe_payload = event.to_safe_dict()["payload"]
    assert payload["tool_evidence_schema_version"] == 1
    assert payload["invocation_identity_digest"] == safe_key_digest(
        "invocation"
    )
    assert payload["idempotency_key_digest"] == safe_key_digest("key")
    assert "invocation_id" not in payload
    assert "attempt_id" not in payload
    assert "resource_key_digest" not in payload
    assert "arguments" not in payload
    assert "output" not in payload
    assert "invocation_id" not in safe_payload
    assert "attempt_id" not in safe_payload


@pytest.mark.parametrize("schema_version", [1, 2])
def test_legacy_tool_events_keep_their_original_payload_schema(schema_version):
    event = replace(
        runtime_event(
            1,
            RuntimeEventType.TOOL_STARTED,
            ToolStartedPayload(
                "writer",
                invocation_id="legacy-invocation",
                attempt_id="legacy-attempt",
            ),
        ),
        schema_version=schema_version,
    )
    payload = event.to_journal_dict()["safe_payload"]
    assert "tool_evidence_schema_version" not in payload
    assert payload["invocation_id"] == "legacy-invocation"


def test_new_evidence_is_reduced_without_registry_lookup_or_double_hashing():
    snapshot = recovery_snapshot()
    journal = InMemoryRunEventJournal()
    journal.append(
        runtime_event(
            1, RuntimeEventType.TOOL_STARTED, versioned_started()
        )
    )
    record = journal.read_after("run", 0, 10)[0]
    item = LimitedJournalTailReducer.reduce(snapshot, (record,)).tool_evidence[
        0
    ]
    assert item.invocation_identity_digest == safe_key_digest("invocation")
    assert item.side_effect_kind == "NONE"
    assert item.idempotency_kind == "IDEMPOTENT_WITH_KEY"
    assert item.idempotency_key_digest == safe_key_digest("key")


def test_same_event_id_with_changed_recovery_evidence_is_a_conflict():
    journal = InMemoryRunEventJournal()
    first = runtime_event(
        1, RuntimeEventType.TOOL_STARTED, versioned_started()
    )
    journal.append(first)
    changed = replace(
        first,
        payload=versioned_started(
            side_effect_kind="EXTERNAL_STATE_MUTATION"
        ),
    )
    with pytest.raises(JournalError) as captured:
        journal.append(changed)
    assert captured.value.error_code is JournalErrorCode.EVENT_ID_CONFLICT


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tool_evidence_schema_version", True),
        ("idempotency_key_digest", "not-a-digest"),
        ("replay_supported", 1),
    ],
)
def test_persisted_tool_evidence_rejects_malformed_recovery_fields(
    field, value
):
    event = runtime_event(
        1, RuntimeEventType.TOOL_STARTED, versioned_started()
    )
    payload = dict(event.to_journal_dict()["safe_payload"])
    payload[field] = value
    with pytest.raises(ValueError):
        validate_journal_payload(RuntimeEventType.TOOL_STARTED, payload)


def test_mixed_legacy_and_versioned_tail_preserves_unknown_vs_authoritative():
    snapshot = recovery_snapshot()
    journal = InMemoryRunEventJournal()
    journal.append(
        runtime_event(
            1,
            RuntimeEventType.TOOL_STARTED,
            ToolStartedPayload(
                "legacy",
                invocation_id="old-invocation",
                attempt_id="old-attempt",
            ),
        )
    )
    journal.append(
        runtime_event(
            2, RuntimeEventType.TOOL_COMPLETED, versioned_completed()
        )
    )
    records = journal.read_after("run", 0, 10)
    items = LimitedJournalTailReducer.reduce(snapshot, records).tool_evidence
    assert items[0].tool_evidence_schema_version is None
    assert items[0].side_effect_kind is None
    assert items[1].tool_evidence_schema_version == 1
    assert items[1].side_effect_kind == "EXTERNAL_STATE_MUTATION"
