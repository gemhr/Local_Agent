"""JSON Schema subset matcher shared by business projection boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def schema_matches(schema: Mapping[str, Any], value: Any) -> bool:
    """Validate the supported object/array/type/enum schema subset."""
    expected = schema.get("type")
    type_valid = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }
    if expected is not None and (expected not in type_valid or not type_valid[expected]):
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if isinstance(value, dict):
        required = schema.get("required", ())
        if any(key not in value for key in required):
            return False
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            return False
        return all(
            key not in properties or schema_matches(properties[key], item)
            for key, item in value.items()
        )
    if isinstance(value, list) and isinstance(schema.get("items"), Mapping):
        return all(schema_matches(schema["items"], item) for item in value)
    return True


__all__ = ["schema_matches"]
