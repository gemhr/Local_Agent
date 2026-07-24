from dataclasses import FrozenInstanceError
from math import nan

import pytest

from core.runtime import (
    OperationIdempotency,
    RetryDisposition,
    ToolErrorCategory,
    ToolExecutionSpec,
    ToolInvocation,
    ToolOutputValidationError,
    ToolSideEffectKind,
    ToolSideEffectState,
    build_tool_output,
    retry_disposition_for,
)


def test_invocation_is_frozen_recursively_and_digest_is_stable():
    first = ToolInvocation.create(
        tool_name="read",
        arguments={"b": [2, {"x": True}], "a": 1},
        invocation_id="invocation-1",
    )
    second = ToolInvocation.create(
        tool_name="read",
        arguments={"a": 1, "b": [2, {"x": True}]},
        invocation_id="invocation-2",
    )
    assert first.arguments_digest == second.arguments_digest
    with pytest.raises(TypeError):
        first.arguments["a"] = 2
    with pytest.raises(FrozenInstanceError):
        first.tool_name = "other"


@pytest.mark.parametrize(
    "arguments",
    [{"x": object()}, {"x": nan}, {1: "bad"}, {"x": b"bytes"}],
)
def test_invocation_rejects_non_json_safe_values(arguments):
    with pytest.raises(ValueError):
        ToolInvocation.create(tool_name="read", arguments=arguments)


def test_spec_rejects_bool_and_uses_conservative_unknown_defaults():
    spec = ToolExecutionSpec(tool_name="unknown")
    assert spec.side_effect_kind == ToolSideEffectKind.UNKNOWN
    assert spec.idempotency == OperationIdempotency.UNKNOWN
    with pytest.raises(ValueError):
        ToolExecutionSpec(tool_name="bad", max_output_bytes=True)
    with pytest.raises(ValueError):
        ToolExecutionSpec(tool_name="bad", default_timeout_seconds=float("inf"))


def test_output_limit_is_utf8_safe_and_safe_dict_hides_content():
    output = build_tool_output("甲乙丙", "text/plain", 4)
    assert output.content == "甲"
    assert output.original_size_bytes == 9
    assert output.returned_size_bytes == 3
    assert output.truncated
    assert "content" not in output.to_safe_dict()
    assert output.to_safe_dict(include_content=True)["content"] == "甲"


def test_retry_disposition_uses_all_safety_facts():
    assert retry_disposition_for(
        category=ToolErrorCategory.TRANSIENT,
        idempotency=OperationIdempotency.READ_ONLY,
        idempotency_key=None,
        side_effect_state=ToolSideEffectState.NOT_STARTED,
    ) == RetryDisposition.SAFE
    assert retry_disposition_for(
        category=ToolErrorCategory.TRANSIENT,
        idempotency=OperationIdempotency.IDEMPOTENT_WITH_KEY,
        idempotency_key="stable",
        side_effect_state=ToolSideEffectState.NOT_STARTED,
    ) == RetryDisposition.SAFE_WITH_IDEMPOTENCY_KEY
    assert retry_disposition_for(
        category=ToolErrorCategory.TRANSIENT,
        idempotency=OperationIdempotency.NON_IDEMPOTENT,
        idempotency_key=None,
        side_effect_state=ToolSideEffectState.NOT_STARTED,
    ) == RetryDisposition.UNSAFE
    assert retry_disposition_for(
        category=ToolErrorCategory.TIMEOUT,
        idempotency=OperationIdempotency.IDEMPOTENT,
        idempotency_key=None,
        side_effect_state=ToolSideEffectState.UNKNOWN,
    ) == RetryDisposition.OUTCOME_UNKNOWN


def test_committed_retry_requires_key_replay_support_and_post_commit_category():
    allowed = retry_disposition_for(
        category=ToolErrorCategory.POST_COMMIT_RESPONSE_FAILURE,
        idempotency=OperationIdempotency.IDEMPOTENT_WITH_KEY,
        idempotency_key="stable",
        side_effect_state=ToolSideEffectState.COMMITTED,
        supports_idempotency_replay=True,
    )
    assert allowed == RetryDisposition.SAFE_WITH_IDEMPOTENCY_KEY
    assert retry_disposition_for(
        category=ToolErrorCategory.POST_COMMIT_RESPONSE_FAILURE,
        idempotency=OperationIdempotency.IDEMPOTENT_WITH_KEY,
        idempotency_key="stable",
        side_effect_state=ToolSideEffectState.COMMITTED,
        supports_idempotency_replay=False,
    ) == RetryDisposition.UNSAFE
    assert retry_disposition_for(
        category=ToolErrorCategory.TRANSIENT,
        idempotency=OperationIdempotency.IDEMPOTENT_WITH_KEY,
        idempotency_key="stable",
        side_effect_state=ToolSideEffectState.COMMITTED,
        supports_idempotency_replay=True,
    ) == RetryDisposition.UNSAFE


def test_committed_retry_rejects_digest_mismatch_partial_output_and_failed_compensation():
    base = dict(
        category=ToolErrorCategory.POST_COMMIT_RESPONSE_FAILURE,
        idempotency=OperationIdempotency.IDEMPOTENT_WITH_KEY,
        idempotency_key="stable",
        side_effect_state=ToolSideEffectState.COMMITTED,
        supports_idempotency_replay=True,
    )
    assert retry_disposition_for(
        **base, arguments_digest_matches=False
    ) == RetryDisposition.UNSAFE
    assert retry_disposition_for(
        **base, output_started=True
    ) == RetryDisposition.UNSAFE
    assert retry_disposition_for(
        **base,
        compensation_attempted=True,
        compensation_succeeded=False,
    ) == RetryDisposition.UNSAFE


def test_json_output_never_uses_arbitrary_byte_truncation():
    content = '{"items":[1,2],"name":"甲"}'
    output = build_tool_output(content, "application/json", 1024)
    assert output.content == content
    assert not output.truncated
    assert output.content_type == "application/json"
    import json

    assert json.loads(output.content)["items"] == [1, 2]

    with pytest.raises(ToolOutputValidationError) as caught:
        build_tool_output(content, "application/json", 8)
    assert caught.value.safe_error_code == "TOOL_OUTPUT_TOO_LARGE"
    assert caught.value.safe_metadata["digest"] == output.digest
    assert "content" not in caught.value.safe_metadata


def test_invalid_json_and_unknown_content_type_are_rejected_safely():
    with pytest.raises(ToolOutputValidationError) as invalid:
        build_tool_output('{"broken"', "application/json", 100)
    assert invalid.value.safe_error_code == "TOOL_OUTPUT_INVALID_JSON"
    with pytest.raises(ToolOutputValidationError) as unsupported:
        build_tool_output("binary", "application/octet-stream", 100)
    assert unsupported.value.safe_error_code == "TOOL_OUTPUT_CONTENT_TYPE_UNSUPPORTED"


def test_safe_invocation_serialization_contains_only_digests():
    invocation = ToolInvocation.create(
        tool_name="write",
        arguments={"secret": "must-not-appear"},
        idempotency_key="idem-secret",
        resource_key="resource-secret",
    )
    safe = invocation.to_safe_dict()
    serialized = str(safe)
    assert "must-not-appear" not in serialized
    assert "idem-secret" not in serialized
    assert "resource-secret" not in serialized
