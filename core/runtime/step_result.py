#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WP3 typed StepResult contract.

The raw result content is intentionally held in a non-dataclass immutable
object so that ``dataclasses.asdict`` cannot export the body, ``repr`` stays
redacted, and pickling is explicitly rejected.
"""

from __future__ import annotations

from enum import Enum


class ResultContentType(str, Enum):
    """Runtime result content kind consumed by StepResultStore and Synthesis."""

    TEXT = "TEXT"
    MARKDOWN = "MARKDOWN"


class ResultDisposition(str, Enum):
    """Result 的确定性处置语义；不从正文推断。"""

    NORMAL = "NORMAL"
    SECURITY_DENIED = "SECURITY_DENIED"


class SecurityDenialCode(str, Enum):
    """由实际 Governance / Resource Authorization 拒绝产生的固定代码。"""

    TOOL_PERMISSION_DENIED = "TOOL_PERMISSION_DENIED"
    TOOL_APPROVAL_REQUIRED = "TOOL_APPROVAL_REQUIRED"
    TOOL_GOVERNANCE_UNKNOWN_PRINCIPAL = "TOOL_GOVERNANCE_UNKNOWN_PRINCIPAL"
    TOOL_GOVERNANCE_POLICY_MISSING = "TOOL_GOVERNANCE_POLICY_MISSING"
    TOOL_RISK_UNCLASSIFIED = "TOOL_RISK_UNCLASSIFIED"
    TOOL_RESOURCE_DENIED = "TOOL_RESOURCE_DENIED"


class StepResultErrorCode(str, Enum):
    EMPTY_CONTENT = "EMPTY_CONTENT"
    INVALID_CONTENT = "INVALID_CONTENT"
    INCOMPLETE_RESULT = "INCOMPLETE_RESULT"
    INVALID_IDENTIFIER = "INVALID_IDENTIFIER"
    INVALID_CONTENT_TYPE = "INVALID_CONTENT_TYPE"
    INVALID_DISPOSITION = "INVALID_DISPOSITION"
    CONTENT_TOO_LARGE = "CONTENT_TOO_LARGE"


class StepResultError(ValueError):
    """Safe validation error that never carries raw content."""

    def __init__(self, error_code: StepResultErrorCode, safe_message: str) -> None:
        self.error_code = error_code
        self.safe_message = safe_message
        super().__init__(f"{safe_message} (error_code={error_code.value})")


class StepResult:
    """Immutable typed result produced by one Plan Step.

    Fields:
        step_id: the Plan Step that produced this result.
        producer_agent_id: the Agent that produced the result.
        content_type: ResultContentType.
        content: non-empty finite string.
        complete: must be True for any successful commit in the MVP.
    """

    __slots__ = (
        "_step_id",
        "_producer_agent_id",
        "_content_type",
        "_content",
        "_complete",
        "_result_disposition",
        "_security_denial_code",
        "_locked",
    )

    def __init__(
        self,
        step_id: str,
        producer_agent_id: str,
        content_type: ResultContentType,
        content: str,
        complete: bool = True,
        *,
        result_disposition: ResultDisposition = ResultDisposition.NORMAL,
        security_denial_code: SecurityDenialCode | None = None,
        max_content_chars: int | None = None,
    ) -> None:
        if not isinstance(step_id, str) or not step_id.strip():
            raise StepResultError(
                StepResultErrorCode.INVALID_IDENTIFIER,
                "step_id 不能为空",
            )
        if not isinstance(producer_agent_id, str) or not producer_agent_id.strip():
            raise StepResultError(
                StepResultErrorCode.INVALID_IDENTIFIER,
                "producer_agent_id 不能为空",
            )
        if not isinstance(content_type, ResultContentType):
            raise StepResultError(
                StepResultErrorCode.INVALID_CONTENT_TYPE,
                "content_type 必须合法",
            )
        if not isinstance(content, str):
            raise StepResultError(
                StepResultErrorCode.INVALID_CONTENT,
                "content 必须是非空字符串",
            )
        if not content.strip():
            raise StepResultError(
                StepResultErrorCode.EMPTY_CONTENT,
                "content 不能为空",
            )
        if type(complete) is not bool:
            raise StepResultError(
                StepResultErrorCode.INCOMPLETE_RESULT,
                "complete 必须是 bool",
            )
        if not isinstance(result_disposition, ResultDisposition):
            raise StepResultError(
                StepResultErrorCode.INVALID_DISPOSITION,
                "result_disposition 必须合法",
            )
        if security_denial_code is not None and not isinstance(
            security_denial_code, SecurityDenialCode
        ):
            raise StepResultError(
                StepResultErrorCode.INVALID_DISPOSITION,
                "security_denial_code 必须合法",
            )
        if (
            result_disposition is ResultDisposition.NORMAL
            and security_denial_code is not None
        ) or (
            result_disposition is ResultDisposition.SECURITY_DENIED
            and security_denial_code is None
        ):
            raise StepResultError(
                StepResultErrorCode.INVALID_DISPOSITION,
                "result disposition 与 security denial code 不一致",
            )
        if max_content_chars is not None and len(content) > max_content_chars:
            raise StepResultError(
                StepResultErrorCode.CONTENT_TOO_LARGE,
                "content 超过单结果大小上限",
            )
        object.__setattr__(self, "_step_id", step_id.strip())
        object.__setattr__(self, "_producer_agent_id", producer_agent_id.strip())
        object.__setattr__(self, "_content_type", content_type)
        object.__setattr__(self, "_content", content)
        object.__setattr__(self, "_complete", complete)
        object.__setattr__(self, "_result_disposition", result_disposition)
        object.__setattr__(self, "_security_denial_code", security_denial_code)
        object.__setattr__(self, "_locked", True)

    def __setattr__(self, name, value) -> None:
        if getattr(self, "_locked", False):
            raise AttributeError("StepResult 是不可变对象")
        object.__setattr__(self, name, value)

    @property
    def step_id(self) -> str:
        return self._step_id

    @property
    def producer_agent_id(self) -> str:
        return self._producer_agent_id

    @property
    def content_type(self) -> ResultContentType:
        return self._content_type

    @property
    def content(self) -> str:
        return self._content

    @property
    def complete(self) -> bool:
        return self._complete

    @property
    def result_disposition(self) -> ResultDisposition:
        return self._result_disposition

    @property
    def security_denial_code(self) -> SecurityDenialCode | None:
        return self._security_denial_code

    @property
    def char_count(self) -> int:
        return len(self._content)

    def __repr__(self) -> str:
        denial_code = (
            self.security_denial_code.value if self.security_denial_code else None
        )
        return (
            "StepResult("
            f"step_id={self.step_id!r}, "
            f"producer_agent_id={self.producer_agent_id!r}, "
            f"content_type={self.content_type.value!r}, "
            f"char_count={self.char_count}, "
            f"complete={self.complete!r}, "
            f"result_disposition={self.result_disposition.value!r}, "
            f"security_denial_code={denial_code!r}, "
            "content=<redacted>)"
        )

    def __getstate__(self):
        raise TypeError("StepResult 不允许序列化")


__all__ = [
    "ResultDisposition",
    "ResultContentType",
    "SecurityDenialCode",
    "StepResult",
    "StepResultError",
    "StepResultErrorCode",
]
