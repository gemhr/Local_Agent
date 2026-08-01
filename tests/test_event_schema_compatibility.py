from __future__ import annotations

from dataclasses import replace

from core.runtime import (
    TOOL_EVIDENCE_SCHEMA_VERSION,
    RuntimeEventType,
    ToolCompletedPayload,
    canonical_json_digest,
    safe_key_digest,
)
from core.runtime.events import validate_journal_payload
from tests._recovery_fixtures import runtime_event


def completed_payload(result):
    return ToolCompletedPayload(
        tool_name="writer",
        succeeded=True,
        retry_index=0,
        side_effect_state="COMMITTED",
        retry_disposition="UNSAFE",
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
        result_present=True,
        result_digest=canonical_json_digest(result),
    )


def test_new_tool_result_digest_is_canonical_stable_and_content_sensitive():
    first = completed_payload({"b": [2, 3], "a": 1})
    reordered = completed_payload({"a": 1, "b": [2, 3]})
    changed = completed_payload({"a": 1, "b": [2, 4]})
    assert first.result_digest == reordered.result_digest
    assert first.result_digest != changed.result_digest
    projection = runtime_event(
        1, RuntimeEventType.TOOL_COMPLETED, first
    ).to_journal_dict()["safe_payload"]
    assert projection["result_present"] is True
    assert projection["result_digest"] == first.result_digest
    assert "{'b': [2, 3]" not in str(projection)


def test_empty_result_and_absent_result_remain_distinguishable():
    empty = completed_payload({})
    absent = replace(empty, result_present=False, result_digest=None)
    assert empty.result_present is True and empty.result_digest is not None
    assert absent.result_present is False and absent.result_digest is None


def test_historical_v1_v2_tool_evidence_without_result_fields_stays_readable():
    payload = runtime_event(
        1,
        RuntimeEventType.TOOL_COMPLETED,
        completed_payload({"safe": True}),
    ).to_journal_dict()["safe_payload"]
    payload.pop("result_present")
    payload.pop("result_digest")
    for schema_version in (1, 2):
        validate_journal_payload(RuntimeEventType.TOOL_COMPLETED, dict(payload))
        event = replace(
            runtime_event(
                1,
                RuntimeEventType.TOOL_COMPLETED,
                ToolCompletedPayload("legacy", True),
            ),
            schema_version=schema_version,
        )
        assert event.schema_version == schema_version
        assert "result_present" not in payload
