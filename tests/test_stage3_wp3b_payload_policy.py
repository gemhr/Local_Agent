from __future__ import annotations

from dataclasses import fields, replace
import inspect

import pytest
from pydantic import TypeAdapter, ValidationError

import server
from core.request_payload import REQUEST_PAYLOAD_POLICY, RequestPayloadPolicy


POLICY = REQUEST_PAYLOAD_POLICY


def _endpoint_parameter(endpoint, name: str) -> TypeAdapter:
    """用 production endpoint 的 Annotated contract 做 Layer 1 验证。"""
    return TypeAdapter(inspect.signature(endpoint).parameters[name].annotation)


def test_exact_frozen_policy_constants() -> None:
    class_defaults = {
        definition.name: definition.default
        for definition in fields(RequestPayloadPolicy)
    }
    assert POLICY.HTTP_BODY_MAX_BYTES == 1_048_576
    assert POLICY.CHAT_QUERY_MAX_CHARS == 32_768
    assert POLICY.CHAT_FILE_PATH_MAX_CHARS == 4_096
    assert POLICY.AGENT_ID_MAX_CHARS == 64
    assert POLICY.RUN_ID_MAX_CHARS == 45
    assert POLICY.SEARCH_KEYWORD_MAX_CHARS == 1_024
    assert class_defaults["HISTORY_LIMIT_DEFAULT"] == 10
    assert POLICY.HISTORY_LIMIT_DEFAULT == 10
    assert (POLICY.HISTORY_LIMIT_MIN, POLICY.HISTORY_LIMIT_MAX) == (1, 100)
    assert class_defaults["HISTORY_OFFSET_DEFAULT"] == 0
    assert POLICY.HISTORY_OFFSET_DEFAULT == 0
    assert (POLICY.HISTORY_OFFSET_MIN, POLICY.HISTORY_OFFSET_MAX) == (0, 100_000)
    assert POLICY.DELETE_MESSAGE_IDS_MAX_COUNT == 1_000
    assert (POLICY.MESSAGE_ID_MIN, POLICY.MESSAGE_ID_MAX) == (
        1,
        9_223_372_036_854_775_807,
    )


@pytest.mark.parametrize("invalid", [True, 0, -1, 1.5, "1048576", 2_000_000])
def test_policy_rejects_runtime_body_limit_override(invalid) -> None:
    with pytest.raises(ValueError):
        replace(POLICY, HTTP_BODY_MAX_BYTES=invalid)
    assert RequestPayloadPolicy() is not POLICY
    assert RequestPayloadPolicy() == POLICY


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("HISTORY_LIMIT_DEFAULT", 11),
        ("HISTORY_LIMIT_DEFAULT", True),
        ("HISTORY_OFFSET_DEFAULT", 1),
        ("HISTORY_OFFSET_DEFAULT", False),
    ],
)
def test_policy_rejects_history_default_override(field: str, invalid) -> None:
    with pytest.raises(ValueError):
        replace(POLICY, **{field: invalid})


@pytest.mark.parametrize(
    ("field", "limit"),
    [
        ("query", POLICY.CHAT_QUERY_MAX_CHARS),
        ("file_path", POLICY.CHAT_FILE_PATH_MAX_CHARS),
        ("agent_id", POLICY.AGENT_ID_MAX_CHARS),
    ],
)
def test_chat_string_fields_below_exact_and_above(field: str, limit: int) -> None:
    base = {"agent_id": "general", "query": "x"}
    for size in (limit - 1, limit):
        assert getattr(server.ChatRequest.model_validate({**base, field: "a" * size}), field) == "a" * size
    with pytest.raises(ValidationError):
        server.ChatRequest.model_validate({**base, field: "a" * (limit + 1)})


@pytest.mark.parametrize("character", ["a", "界", "😀"])
def test_query_limit_uses_python_characters(character: str) -> None:
    exact = character * POLICY.CHAT_QUERY_MAX_CHARS
    assert len(exact) == POLICY.CHAT_QUERY_MAX_CHARS
    assert server.ChatRequest(agent_id="general", query=exact).query == exact
    with pytest.raises(ValidationError):
        server.ChatRequest(
            agent_id="general",
            query=exact + character,
        )


def test_query_compatibility_and_small_unknown_field() -> None:
    request = server.ChatRequest.model_validate(
        {
            "agent_id": "general",
            "query": "",
            "file_path": "C:/data/example.txt",
            "unexpected": "ignored",
        }
    )
    assert request.query == ""
    assert "unexpected" not in request.model_dump()
    assert server.ChatRequest(agent_id="general", query="   ").query == "   "
    assert server.ChatRequest(agent_id="general", query="A\x00B").query == "A\x00B"


@pytest.mark.parametrize(
    "run_id",
    [
        "12345678123456781234567812345678",
        "12345678-1234-5678-1234-567812345678",
        "{12345678-1234-5678-1234-567812345678}",
        "urn:uuid:12345678-1234-5678-1234-567812345678",
        "malformed-but-within-limit",
    ],
)
def test_chat_run_id_schema_preserves_string_forms(run_id: str) -> None:
    assert len(run_id) <= POLICY.RUN_ID_MAX_CHARS
    assert server.ChatRequest(agent_id="general", query="x", run_id=run_id).run_id == run_id


def test_chat_run_id_schema_rejects_above_limit() -> None:
    with pytest.raises(ValidationError):
        server.ChatRequest(
            agent_id="general",
            query="x",
            run_id="r" * (POLICY.RUN_ID_MAX_CHARS + 1),
        )


def test_search_keyword_below_exact_above_and_empty() -> None:
    adapter = _endpoint_parameter(server.search_endpoint, "keyword")
    for value in ("", "k" * (POLICY.SEARCH_KEYWORD_MAX_CHARS - 1), "k" * POLICY.SEARCH_KEYWORD_MAX_CHARS):
        assert adapter.validate_python(value) == value
    with pytest.raises(ValidationError):
        adapter.validate_python("k" * (POLICY.SEARCH_KEYWORD_MAX_CHARS + 1))


def test_history_agent_id_below_exact_and_above() -> None:
    adapter = _endpoint_parameter(server.get_history_endpoint, "agent_id")
    for size in (POLICY.AGENT_ID_MAX_CHARS - 1, POLICY.AGENT_ID_MAX_CHARS):
        value = "a" * size
        assert adapter.validate_python(value) == value
    with pytest.raises(ValidationError):
        adapter.validate_python("a" * (POLICY.AGENT_ID_MAX_CHARS + 1))


def test_history_route_defaults_are_owned_by_policy() -> None:
    signature = inspect.signature(server.get_history_endpoint)
    assert signature.parameters["limit"].default == POLICY.HISTORY_LIMIT_DEFAULT
    assert signature.parameters["offset"].default == POLICY.HISTORY_OFFSET_DEFAULT


@pytest.mark.parametrize("value", [0, 1, 100, 101, -1])
def test_history_limit_layer1_boundaries(value: int) -> None:
    adapter = _endpoint_parameter(server.get_history_endpoint, "limit")
    if POLICY.HISTORY_LIMIT_MIN <= value <= POLICY.HISTORY_LIMIT_MAX:
        assert adapter.validate_python(value) == value
    else:
        with pytest.raises(ValidationError):
            adapter.validate_python(value)


@pytest.mark.parametrize("value", [-1, 0, 100_000, 100_001])
def test_history_offset_layer1_boundaries(value: int) -> None:
    adapter = _endpoint_parameter(server.get_history_endpoint, "offset")
    if POLICY.HISTORY_OFFSET_MIN <= value <= POLICY.HISTORY_OFFSET_MAX:
        assert adapter.validate_python(value) == value
    else:
        with pytest.raises(ValidationError):
            adapter.validate_python(value)


def test_delete_message_ids_count_boundaries() -> None:
    assert server.DeleteMemoryRequest(message_ids=[]).message_ids == []
    exact = list(range(1, POLICY.DELETE_MESSAGE_IDS_MAX_COUNT + 1))
    assert server.DeleteMemoryRequest(message_ids=exact).message_ids == exact
    with pytest.raises(ValidationError):
        server.DeleteMemoryRequest(message_ids=exact + [POLICY.DELETE_MESSAGE_IDS_MAX_COUNT + 1])


@pytest.mark.parametrize("valid", [POLICY.MESSAGE_ID_MIN, POLICY.MESSAGE_ID_MAX])
def test_delete_message_id_valid_boundaries(valid: int) -> None:
    assert server.DeleteMemoryRequest(message_ids=[valid]).message_ids == [valid]


@pytest.mark.parametrize("invalid", [-1, 0, POLICY.MESSAGE_ID_MAX + 1])
def test_delete_message_id_invalid_boundaries(invalid: int) -> None:
    with pytest.raises(ValidationError):
        server.DeleteMemoryRequest(message_ids=[invalid])
