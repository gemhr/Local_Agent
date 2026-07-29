#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Deterministic, strict JSON helpers for runtime snapshots."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
from math import isfinite
from typing import Any


def require_int(value: object, field_name: str, *, minimum: int | None = None) -> int:
    """Validate an integer while rejecting bool, which is an int subclass."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{field_name} must be >= {minimum}")
    return value


def require_finite_number(
    value: object, field_name: str, *, minimum: float | None = None
) -> int | float:
    """Validate a finite JSON number while rejecting bool."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a number")
    if not isfinite(value):
        raise ValueError(f"{field_name} must be finite")
    if minimum is not None and value < minimum:
        raise ValueError(f"{field_name} must be >= {minimum}")
    return value


def require_utc(value: object, field_name: str) -> datetime:
    """Require a timezone-aware datetime whose offset is UTC."""
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValueError(f"{field_name} must be a timezone-aware UTC datetime")
    return value.astimezone(timezone.utc)


def parse_utc(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO 8601 UTC datetime") from exc
    return require_utc(parsed, field_name)


def to_primitive(value: Any) -> Any:
    """Convert supported immutable contract values to JSON primitives."""
    if isinstance(value, Enum):
        return to_primitive(value.value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("snapshot values must not contain NaN or Infinity")
        return value
    if isinstance(value, datetime):
        return require_utc(value, "datetime").isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: to_primitive(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("snapshot mapping keys must be strings")
            result[key] = to_primitive(item)
        return result
    if isinstance(value, (tuple, list)):
        return [to_primitive(item) for item in value]
    raise TypeError(f"unsupported snapshot value type: {type(value).__name__}")


def canonical_json(value: object) -> str:
    """Return the single v1 canonical JSON representation."""
    return json.dumps(
        to_primitive(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def text_digest(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("value must be a string")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def strict_json_loads(value: str) -> object:
    """Parse JSON while rejecting the non-standard NaN/Infinity constants."""
    if not isinstance(value, str):
        raise TypeError("JSON payload must be a string")

    def reject_constant(_: str) -> None:
        raise ValueError("snapshot JSON must not contain NaN or Infinity")

    return json.loads(value, parse_constant=reject_constant)


def snapshot_to_json(snapshot: object) -> str:
    """Serialize a RunSnapshot without importing the contract at module load time."""
    from core.runtime.snapshot_contract import RunSnapshot

    if not isinstance(snapshot, RunSnapshot):
        raise TypeError("snapshot must be a RunSnapshot")
    snapshot.verify_digest()
    return canonical_json(snapshot.to_payload())


def snapshot_from_json(value: str):
    """Deserialize and verify a v1 RunSnapshot."""
    from core.runtime.snapshot_contract import RunSnapshot

    return RunSnapshot.from_payload(strict_json_loads(value))


__all__ = [
    "canonical_json",
    "parse_utc",
    "require_finite_number",
    "require_int",
    "require_utc",
    "sha256_digest",
    "snapshot_from_json",
    "snapshot_to_json",
    "strict_json_loads",
    "text_digest",
    "to_primitive",
]
