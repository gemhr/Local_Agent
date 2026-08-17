#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Standalone code-owned TraceExportEnvelope JSON serializer.

This module is the ONLY future AgentEvalOps wire serializer Owner on the
LocalAgent side. It deliberately performs no HTTP, no compatibility projection,
no AgentEvalOps calls, no retry, no batching, no Settings reads, and no auth
header construction.

It first validates the envelope through the shared Trace Export Contract
semantic Owner (``core.runtime.trace_export_contract``), then emits a
deterministic UTF-8 JSON byte payload with the frozen wire field set.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
import json
import math
from typing import Any

from core.runtime.trace_export_contract import (
    TraceExportEnvelope,
    TraceExportEnvelopeError,
    validate_trace_export_envelope_semantics,
)

# Code-owned maximum serialized payload size in bytes.
MAX_TRACE_EXPORT_PAYLOAD_BYTES = 16384

# Bit-length guard used before rendering large Python ints.
#
# Python 3.12 has a default integer-string conversion cap of 4300 decimal
# digits.  For integers whose decimal token alone could still fit the 16384
# byte payload cap we use Decimal (stdlib) to obtain the exact decimal token.
# ``MAX_INT_BIT_LENGTH_FOR_PAYLOAD_BOUND`` is the largest bit length whose
# decimal digit count is guaranteed not to exceed 16384 digits.
_MAX_INT_BIT_LENGTH_FOR_PAYLOAD_BOUND = 54426

# Largest bit length whose decimal representation is guaranteed not to exceed
# Python's default 4300-digit integer-string conversion limit.
_MAX_STR_SAFE_INT_BIT_LENGTH = 14284

WIRE_FIELDS = (
    "contract_identity",
    "contract_version",
    "contract_fingerprint",
    "run_id",
    "trace_id",
    "span_id",
    "parent_span_id",
    "step_id",
    "operation",
    "component",
    "started_at",
    "completed_at",
    "duration_ms",
    "status",
    "error_code",
    "attributes",
)
WIRE_FIELD_SET = frozenset(WIRE_FIELDS)


class TraceExportSerializationError(RuntimeError):
    """Bounded content-free serializer failure.

    ``str``/``repr`` contain only a fixed local error code and never include
    envelope fields, attributes, IDs, raw JSON or secrets.
    """

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)

    def __repr__(self) -> str:
        return f"TraceExportSerializationError(error_code={self.error_code!r})"


def _format_int(value: int) -> str:
    """Return the exact decimal JSON integer token for a Python int."""
    if value.bit_length() <= _MAX_STR_SAFE_INT_BIT_LENGTH:
        return str(value)
    return str(Decimal(value))


def _encode_value(value: object) -> str:
    """Encode one JSON value using stdlib JSON and code-owned numeric rules."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        # bool is handled above; this is a genuine int.
        if value.bit_length() > _MAX_INT_BIT_LENGTH_FOR_PAYLOAD_BOUND:
            raise TraceExportSerializationError("PAYLOAD_BOUND_EXCEEDED")
        return _format_int(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TraceExportSerializationError("SERIALIZATION_FAILED")
        return json.dumps(value, allow_nan=False)
    if isinstance(value, Mapping):
        return _encode_object(value)
    raise TraceExportSerializationError("SERIALIZATION_FAILED")


def _encode_object(mapping: Mapping[str, object]) -> str:
    """Encode a mapping as a sorted-key compact JSON object.

    Sorting keys makes attribute insertion order irrelevant and makes duplicate
    keys structurally impossible (the serializer constructs/iterates the
    mapping itself).
    """
    parts: list[str] = []
    for key in sorted(mapping):
        if not isinstance(key, str):
            raise TraceExportSerializationError("SERIALIZATION_FAILED")
        parts.append(
            json.dumps(key, ensure_ascii=False) + ":" + _encode_value(mapping[key])
        )
    return "{" + ",".join(parts) + "}"


def _encode_datetime(value: datetime) -> str:
    """Freeze the code-owned RFC 3339 UTC datetime representation."""
    return value.astimezone(UTC).isoformat()


def _encode_enum(value: Any) -> str:
    """Serialize enum-like values to their frozen public string value."""
    return str(value.value)


def _assert_exact_wire_fields(payload: dict[str, object]) -> None:
    if tuple(payload) != WIRE_FIELDS:
        raise TraceExportSerializationError("WIRE_FIELD_SET_INVALID")


def serialize_trace_export_envelope(envelope: TraceExportEnvelope) -> bytes:
    """Serialize a validated TraceExportEnvelope to deterministic UTF-8 bytes.

    The serializer is the ONLY code-owned producer of the AgentEvalOps wire
    representation. It fails closed on invalid envelope semantics, oversized
    integer tokens and payloads larger than
    ``MAX_TRACE_EXPORT_PAYLOAD_BYTES``. No transport behavior exists here.
    """
    if not isinstance(envelope, TraceExportEnvelope):
        raise TypeError("envelope must be a TraceExportEnvelope")

    # Shared semantic Owner: no duplicate/widened contract validation.
    try:
        validate_trace_export_envelope_semantics(envelope)
    except TraceExportEnvelopeError:
        raise

    payload: dict[str, object] = {
        "contract_identity": envelope.contract_identity,
        "contract_version": envelope.contract_version,
        "contract_fingerprint": envelope.contract_fingerprint,
        "run_id": envelope.run_id,
        "trace_id": envelope.trace_id,
        "span_id": envelope.span_id,
        "parent_span_id": envelope.parent_span_id,
        "step_id": envelope.step_id,
        "operation": envelope.operation,
        "component": envelope.component,
        "started_at": _encode_datetime(envelope.started_at),
        "completed_at": _encode_datetime(envelope.completed_at),
        "duration_ms": envelope.duration_ms,
        "status": _encode_enum(envelope.status),
        "error_code": envelope.error_code,
        "attributes": dict(envelope.attributes),
    }
    _assert_exact_wire_fields(payload)

    try:
        payload_bytes = _encode_object(payload).encode("utf-8")
    except TraceExportSerializationError:
        raise
    except (TypeError, ValueError, OverflowError, ArithmeticError) as exc:
        raise TraceExportSerializationError("SERIALIZATION_FAILED") from exc

    if len(payload_bytes) > MAX_TRACE_EXPORT_PAYLOAD_BYTES:
        raise TraceExportSerializationError("PAYLOAD_BOUND_EXCEEDED")
    return payload_bytes


__all__ = [
    "MAX_TRACE_EXPORT_PAYLOAD_BYTES",
    "TraceExportSerializationError",
    "WIRE_FIELDS",
    "serialize_trace_export_envelope",
]
