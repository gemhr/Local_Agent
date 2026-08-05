"""StepResult contract: types, completeness, safety boundaries."""

from __future__ import annotations

import dataclasses
import pickle

import pytest

from core.runtime import (
    ResultContentType,
    StepResult,
    StepResultError,
    StepResultErrorCode,
)


def test_step_result_contract_fields_and_content_type() -> None:
    result = StepResult(
        step_id="task-code",
        producer_agent_id="code_expert",
        content_type=ResultContentType.TEXT,
        content="inspection result",
        complete=True,
    )
    assert result.step_id == "task-code"
    assert result.producer_agent_id == "code_expert"
    assert result.content_type is ResultContentType.TEXT
    assert result.content == "inspection result"
    assert result.complete is True
    assert result.char_count == 17
    assert ResultContentType.MARKDOWN.value == "MARKDOWN"


@pytest.mark.parametrize(
    ("kwargs", "expected_code"),
    [
        (
            dict(
                step_id="",
                producer_agent_id="code_expert",
                content_type=ResultContentType.TEXT,
                content="x",
            ),
            StepResultErrorCode.INVALID_IDENTIFIER,
        ),
        (
            dict(
                step_id="s",
                producer_agent_id="",
                content_type=ResultContentType.TEXT,
                content="x",
            ),
            StepResultErrorCode.INVALID_IDENTIFIER,
        ),
        (
            dict(
                step_id="s",
                producer_agent_id="code_expert",
                content_type="TEXT",  # type: ignore[arg-type]
                content="x",
            ),
            StepResultErrorCode.INVALID_CONTENT_TYPE,
        ),
        (
            dict(
                step_id="s",
                producer_agent_id="code_expert",
                content_type=ResultContentType.TEXT,
                content="",
            ),
            StepResultErrorCode.EMPTY_CONTENT,
        ),
        (
            dict(
                step_id="s",
                producer_agent_id="code_expert",
                content_type=ResultContentType.TEXT,
                content=b"bytes",  # type: ignore[arg-type]
            ),
            StepResultErrorCode.INVALID_CONTENT,
        ),
        (
            dict(
                step_id="s",
                producer_agent_id="code_expert",
                content_type=ResultContentType.TEXT,
                content="x",
                complete="yes",  # type: ignore[arg-type]
            ),
            StepResultErrorCode.INCOMPLETE_RESULT,
        ),
    ],
)
def test_step_result_rejects_invalid_inputs(kwargs, expected_code) -> None:
    with pytest.raises(StepResultError) as exc_info:
        StepResult(**kwargs)
    assert exc_info.value.error_code is expected_code


def test_step_result_size_limit_fails_closed() -> None:
    with pytest.raises(StepResultError) as exc_info:
        StepResult(
            "s",
            "code_expert",
            ResultContentType.TEXT,
            "x" * 11,
            max_content_chars=10,
        )
    assert exc_info.value.error_code is StepResultErrorCode.CONTENT_TOO_LARGE


def test_step_result_safe_repr_redacts_content() -> None:
    secret = "SECRET_SPECIALIST_RESULT_DO_NOT_PERSIST"
    result = StepResult(
        "s",
        "code_expert",
        ResultContentType.MARKDOWN,
        secret,
    )
    rendered = repr(result)
    assert secret not in rendered
    assert "<redacted>" in rendered
    assert "char_count=" in rendered


def test_step_result_immutable() -> None:
    result = StepResult(
        "s", "code_expert", ResultContentType.TEXT, "x"
    )
    with pytest.raises(AttributeError):
        result.content = "changed"


def test_step_result_not_exported_by_asdict() -> None:
    result = StepResult(
        "s", "code_expert", ResultContentType.TEXT, "x"
    )
    with pytest.raises(TypeError):
        dataclasses.asdict(result)


def test_step_result_rejects_pickling() -> None:
    result = StepResult(
        "s", "code_expert", ResultContentType.TEXT, "x"
    )
    with pytest.raises(TypeError):
        pickle.dumps(result)


def test_step_result_content_never_enters_exception() -> None:
    secret = "SECRET_SPECIALIST_RESULT_DO_NOT_PERSIST"
    try:
        StepResult(
            "",
            "code_expert",
            ResultContentType.TEXT,
            secret,
        )
    except StepResultError as exc:
        rendered = f"{exc!r} {str(exc)}"
        assert secret not in rendered
    else:
        pytest.fail("expected StepResultError")


def test_step_result_error_code_enum_is_stable() -> None:
    assert StepResultErrorCode.INVALID_CONTENT.value == "INVALID_CONTENT"
    assert StepResultErrorCode.INCOMPLETE_RESULT.value == "INCOMPLETE_RESULT"
