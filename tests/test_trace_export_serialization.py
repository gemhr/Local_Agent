#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""H-3 Standalone TraceExportEnvelope serializer and numeric totality tests.

These tests exercise only the LocalAgent-side serializer Owner. No HTTP,
AgentEvalOps, Settings, server.py, dispatcher or exporter wiring is touched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import math
import sys

import pytest

from core.runtime.tracing import SpanRecord, SpanStatus
from core.runtime.trace_contract import RUNTIME_RUN_SPAN
from core.runtime.trace_contract_fingerprint import TRACE_CONTRACT_FINGERPRINT
from core.runtime.trace_export_contract import (
    MAX_V1_DURATION_INT,
    TRACE_EXPORT_CONTRACT_IDENTITY,
    TRACE_EXPORT_CONTRACT_VERSION,
    TraceExportEnvelope,
    TraceExportEnvelopeError,
    project_span,
    validate_trace_export_envelope_semantics,
)
from core.runtime.trace_export_serialization import (
    MAX_TRACE_EXPORT_PAYLOAD_BYTES,
    TraceExportSerializationError,
    WIRE_FIELDS,
    serialize_trace_export_envelope,
)

MARKER = "SERIALIZER_SECRET_DO_NOT_LEAK_7F3A"


def make_envelope(
    *,
    duration_ms: object = 5.0,
    attributes: dict[str, object] | None = None,
    status: SpanStatus = SpanStatus.OK,
    error_code: str | None = None,
    operation: str = RUNTIME_RUN_SPAN,
    step_id: str | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    **overrides: object,
) -> TraceExportEnvelope:
    started_at = started_at or datetime.now(UTC)
    completed_at = completed_at or started_at + timedelta(milliseconds=5)
    kwargs: dict[str, object] = dict(
        contract_identity=TRACE_EXPORT_CONTRACT_IDENTITY,
        contract_version=TRACE_EXPORT_CONTRACT_VERSION,
        contract_fingerprint=TRACE_CONTRACT_FINGERPRINT,
        run_id="run-1",
        trace_id="trace-1",
        span_id="span-1",
        parent_span_id=None,
        step_id=step_id,
        operation=operation,
        component="runtime",
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=duration_ms,
        status=status,
        error_code=error_code,
        attributes=dict(attributes or {}),
    )
    kwargs.update(overrides)
    return TraceExportEnvelope(**kwargs)


def tamper_envelope(
    envelope: TraceExportEnvelope, **changes: object
) -> TraceExportEnvelope:
    for name, value in changes.items():
        object.__setattr__(envelope, name, value)
    return envelope


def assert_duration_rejected(value: object) -> None:
    with pytest.raises(TraceExportEnvelopeError) as exc_info:
        make_envelope(duration_ms=value)
    assert exc_info.value.error_code == "SPAN_DURATION_INVALID"


def test_max_v1_duration_int_is_exact_code_owned_constant() -> None:
    assert MAX_V1_DURATION_INT == 2**1024 - 2**970 - 1
    assert MAX_V1_DURATION_INT > 10**308
    assert MAX_V1_DURATION_INT + 1 > MAX_V1_DURATION_INT


def test_validator_accepts_large_legal_duration_ints() -> None:
    for value in (10**308, 10**308 + 1, MAX_V1_DURATION_INT):
        envelope = make_envelope(duration_ms=value)
        assert envelope.duration_ms == value
        validate_trace_export_envelope_semantics(envelope)


def test_validator_rejects_bool_duration() -> None:
    assert_duration_rejected(True)
    assert_duration_rejected(False)


def test_validator_rejects_negative_int_duration() -> None:
    assert_duration_rejected(-1)


def test_validator_rejects_non_finite_float_duration() -> None:
    for value in (float("nan"), float("inf"), float("-inf")):
        assert_duration_rejected(value)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(MAX_V1_DURATION_INT + 1, id="MAX_PLUS_1"),
        pytest.param(10**309, id="10_309"),
        pytest.param(10**1000, id="10_1000"),
        pytest.param(10**100000, id="10_100000"),
    ],
)
def test_validator_rejects_oversized_int_duration_bounded(value: object) -> None:
    assert_duration_rejected(value)


def test_validator_no_raw_overflow_error_for_huge_int() -> None:
    # The bounded comparison path must never surface raw OverflowError.
    for value in (10**1000, 10**100000, MAX_V1_DURATION_INT + 1):
        with pytest.raises(TraceExportEnvelopeError) as exc_info:
            make_envelope(duration_ms=value)
        assert exc_info.value.error_code == "SPAN_DURATION_INVALID"


@pytest.mark.parametrize(
    "value",
    [0, 1, 2**53, 2**53 + 1, 10**308 + 1, MAX_V1_DURATION_INT],
)
def test_serializer_exact_integer_duration_token(value: int) -> None:
    envelope = make_envelope(duration_ms=value)
    payload = serialize_trace_export_envelope(envelope)
    data = json.loads(payload.decode("utf-8"))
    assert data["duration_ms"] == value
    assert isinstance(data["duration_ms"], int)
    # The JSON numeric token must be the exact decimal integer token.
    assert json.dumps(data["duration_ms"]).encode("utf-8") in payload


@pytest.mark.parametrize(
    "value",
    [0.0, -0.0, 1.0, 1.5, sys.float_info.max, sys.float_info.min, 5e-324],
)
def test_serializer_float_duration_round_trip(value: float) -> None:
    envelope = make_envelope(duration_ms=value)
    payload = serialize_trace_export_envelope(envelope)
    data = json.loads(payload.decode("utf-8"))
    loaded = data["duration_ms"]
    assert isinstance(loaded, float)
    # Shortest-round-trip stdlib JSON must reproduce the original binary64.
    assert loaded == value
    assert math.copysign(1.0, loaded) == math.copysign(1.0, value)


def test_serializer_negative_zero_token() -> None:
    envelope = make_envelope(duration_ms=-0.0)
    payload = serialize_trace_export_envelope(envelope)
    text = payload.decode("utf-8")
    # The producer may emit -0.0; it is not rewritten on the envelope.
    assert '"duration_ms":-0.0' in text
    assert math.copysign(1.0, envelope.duration_ms) == -1.0


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(True, id="bool_true"),
        pytest.param(False, id="bool_false"),
        pytest.param(-1, id="neg_one"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="pos_inf"),
        pytest.param(float("-inf"), id="neg_inf"),
        pytest.param(MAX_V1_DURATION_INT + 1, id="MAX_PLUS_1"),
        pytest.param(10**309, id="10_309"),
        pytest.param(10**1000, id="10_1000"),
        pytest.param(10**100000, id="10_100000"),
    ],
)
def test_serializer_fails_closed_on_invalid_duration_bypass(value: object) -> None:
    envelope = make_envelope()
    tamper_envelope(envelope, duration_ms=value)
    with pytest.raises(TraceExportEnvelopeError) as exc_info:
        serialize_trace_export_envelope(envelope)
    assert exc_info.value.error_code == "SPAN_DURATION_INVALID"


def test_serializer_wire_field_set_exact() -> None:
    payload = serialize_trace_export_envelope(make_envelope())
    data = json.loads(payload.decode("utf-8"))
    assert set(data) == set(WIRE_FIELDS)
    assert len(data) == len(WIRE_FIELDS)
    for forbidden in ("endpoint", "project", "api_key", "headers", "retry"):
        assert forbidden not in data


def test_serializer_enum_string() -> None:
    envelope = make_envelope(status=SpanStatus.ERROR, error_code="SPAN_FAILED")
    data = json.loads(serialize_trace_export_envelope(envelope).decode("utf-8"))
    assert data["status"] == "ERROR"
    assert data["error_code"] == "SPAN_FAILED"


def test_serializer_datetime_rfc3339_utc() -> None:
    started_at = datetime(2030, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
    completed_at = started_at + timedelta(seconds=1)
    envelope = make_envelope(started_at=started_at, completed_at=completed_at)
    data = json.loads(serialize_trace_export_envelope(envelope).decode("utf-8"))
    assert data["started_at"] == started_at.astimezone(UTC).isoformat()
    assert data["completed_at"] == completed_at.astimezone(UTC).isoformat()
    assert datetime.fromisoformat(data["started_at"]) == started_at
    assert datetime.fromisoformat(data["completed_at"]) == completed_at


def test_serializer_deterministic_bytes_and_attribute_order() -> None:
    started_at = datetime(2031, 1, 2, 3, 4, 5, tzinfo=UTC)
    attrs_a = {"plan_id": "plan-a", "step_count": 2, "plan_version": 1}
    attrs_b = {"plan_version": 1, "step_count": 2, "plan_id": "plan-a"}
    first = serialize_trace_export_envelope(
        make_envelope(attributes=attrs_a, started_at=started_at)
    )
    second = serialize_trace_export_envelope(
        make_envelope(attributes=attrs_a, started_at=started_at)
    )
    third = serialize_trace_export_envelope(
        make_envelope(attributes=attrs_b, started_at=started_at)
    )
    assert first == second
    assert first == third


def test_serializer_utf8_bytes_no_bom() -> None:
    payload = serialize_trace_export_envelope(make_envelope())
    assert isinstance(payload, bytes)
    assert not payload.startswith(b"\xef\xbb\xbf")
    payload.decode("utf-8")  # must be valid UTF-8


def test_serializer_small_ordinary_payload() -> None:
    payload = serialize_trace_export_envelope(make_envelope())
    assert len(payload) <= MAX_TRACE_EXPORT_PAYLOAD_BYTES
    assert len(payload) < 4096


def test_serializer_near_limit_legal_payload() -> None:
    # A contract-valid NON_NEGATIVE_INT attribute may be large; the wire bound
    # is the serializer's code-owned cap, not a semantic contract widening.
    envelope = make_envelope(attributes={"plan_version": 10**15800})
    payload = serialize_trace_export_envelope(envelope)
    assert len(payload) <= MAX_TRACE_EXPORT_PAYLOAD_BYTES


def test_serializer_oversized_payload_bounded_reject() -> None:
    envelope = make_envelope(attributes={"plan_version": 10**16380})
    with pytest.raises(TraceExportSerializationError) as exc_info:
        serialize_trace_export_envelope(envelope)
    assert exc_info.value.error_code == "PAYLOAD_BOUND_EXCEEDED"


def test_serializer_oversized_integer_token_precheck_bounded() -> None:
    # 10**100000 has far more than 16384 decimal digits; reject before any
    # large decimal rendering/JSON serialization.
    envelope = make_envelope(attributes={"plan_version": 10**100000})
    with pytest.raises(TraceExportSerializationError) as exc_info:
        serialize_trace_export_envelope(envelope)
    assert exc_info.value.error_code == "PAYLOAD_BOUND_EXCEEDED"


def test_serializer_error_is_content_free() -> None:
    envelope = make_envelope(attributes={"plan_version": 10**100000})
    with pytest.raises(TraceExportSerializationError) as exc_info:
        serialize_trace_export_envelope(envelope)
    assert MARKER not in str(exc_info.value)
    assert MARKER not in repr(exc_info.value)
    assert "plan_version" not in str(exc_info.value)
    assert "plan_version" not in repr(exc_info.value)


def test_projection_to_serializer_without_http() -> None:
    started_at = datetime.now(UTC)
    record = SpanRecord(
        trace_id="trace-proj-1",
        span_id="span-proj-1",
        parent_span_id=None,
        run_id="run-proj-1",
        step_id=None,
        component="runtime",
        operation=RUNTIME_RUN_SPAN,
        started_at=started_at,
        completed_at=started_at + timedelta(milliseconds=5),
        duration_ms=5.0,
        status=SpanStatus.OK,
        error_code=None,
        attributes={"plan_id": "plan-proj-1", "step_count": 1},
    )
    envelope = project_span(record)
    payload = serialize_trace_export_envelope(envelope)
    data = json.loads(payload.decode("utf-8"))
    assert data["run_id"] == "run-proj-1"
    assert data["span_id"] == "span-proj-1"
    assert data["attributes"]["plan_id"] == "plan-proj-1"
    assert data["contract_fingerprint"] == TRACE_CONTRACT_FINGERPRINT
