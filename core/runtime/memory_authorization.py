#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Agent-private Memory requester/owner authorization boundary（WP7-B）。

本模块只实现 PRIVATE Memory 的最小授权合同。``AdvancedMemoryStore`` 仍是
SQLite persistence primitive；生产读写入口必须先经过这里，不能把传入的
``agent_id`` 当作 requester authorization proof。

PROJECT visibility 只保留冻结的 vocabulary，并在本 WP fail closed；不实现
Project identity、membership、grant 或 Shared Memory。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable


class MemoryVisibility(str, Enum):
    PRIVATE = "PRIVATE"
    PROJECT = "PROJECT"


class MemoryAuthorizationDecision(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"


class MemoryAuthorizationReason(str, Enum):
    OWNER_MATCH = "OWNER_MATCH"
    FOREIGN_PRIVATE_OWNER = "FOREIGN_PRIVATE_OWNER"
    SCOPE_MISMATCH = "SCOPE_MISMATCH"
    UNKNOWN_REQUESTER = "UNKNOWN_REQUESTER"
    UNKNOWN_OWNER = "UNKNOWN_OWNER"
    UNSUPPORTED_OPERATION = "UNSUPPORTED_OPERATION"
    UNSUPPORTED_VISIBILITY = "UNSUPPORTED_VISIBILITY"


class MemoryAuthorizationOperation(str, Enum):
    READ = "READ"
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    FORGET = "FORGET"
    UNSUPPORTED = "UNSUPPORTED"


class MemoryAuthorizationErrorCode:
    PRIVATE_MEMORY_ACCESS_DENIED = "PRIVATE_MEMORY_ACCESS_DENIED"
    PRIVATE_MEMORY_SCOPE_MISMATCH = "PRIVATE_MEMORY_SCOPE_MISMATCH"
    MEMORY_REQUESTER_MISSING = "MEMORY_REQUESTER_MISSING"
    UNSUPPORTED_MEMORY_VISIBILITY = "UNSUPPORTED_MEMORY_VISIBILITY"
    UNSUPPORTED_MEMORY_OPERATION = "UNSUPPORTED_MEMORY_OPERATION"


@dataclass(frozen=True, slots=True)
class MemoryAccessPrincipal:
    """不可变的 requester/acting-agent identity。

    这是授权所需的最小 typed contract。它不表示 delegation、project grant
    或 tool permission；来源必须由上层 Runtime 以可信的 Entry identity
    构造，不能从 Memory record 反推。
    """

    agent_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.agent_id, str) or not self.agent_id.strip():
            raise ValueError("agent_id 必须是非空字符串")
        object.__setattr__(self, "agent_id", self.agent_id.strip())


@dataclass(frozen=True, slots=True)
class MemoryAuthorizationObservation:
    """可供 WP7-E 使用的 content-minimized authorization fact。"""

    operation: str
    requester_agent_id: str | None
    owner_match: bool
    scope_match: bool
    visibility: str | None
    decision: str
    reason: str
    affected_count: int


@dataclass(frozen=True, slots=True)
class MemoryAuthorizationResult:
    """一次授权判断的 typed、安全结果；不携带 Memory 正文。"""

    operation: MemoryAuthorizationOperation
    decision: MemoryAuthorizationDecision
    reason_code: MemoryAuthorizationReason
    requester_agent_id: str | None
    owner_agent_id: str | None
    requested_memory_scope: str | None
    owner_memory_scope: str | None
    visibility: MemoryVisibility | None
    affected_count: int = 0

    @property
    def allowed(self) -> bool:
        return self.decision is MemoryAuthorizationDecision.ALLOW

    @property
    def reason(self) -> str:
        return self.reason_code.value

    @property
    def error_code(self) -> str:
        if self.reason_code is MemoryAuthorizationReason.UNKNOWN_REQUESTER:
            return MemoryAuthorizationErrorCode.MEMORY_REQUESTER_MISSING
        if self.reason_code is MemoryAuthorizationReason.SCOPE_MISMATCH:
            return MemoryAuthorizationErrorCode.PRIVATE_MEMORY_SCOPE_MISMATCH
        if self.reason_code is MemoryAuthorizationReason.UNSUPPORTED_VISIBILITY:
            return MemoryAuthorizationErrorCode.UNSUPPORTED_MEMORY_VISIBILITY
        if self.reason_code is MemoryAuthorizationReason.UNSUPPORTED_OPERATION:
            return MemoryAuthorizationErrorCode.UNSUPPORTED_MEMORY_OPERATION
        return MemoryAuthorizationErrorCode.PRIVATE_MEMORY_ACCESS_DENIED

    def observation(self) -> MemoryAuthorizationObservation:
        return MemoryAuthorizationObservation(
            operation=self.operation.value,
            requester_agent_id=self.requester_agent_id,
            owner_match=(
                self.requester_agent_id is not None
                and self.owner_agent_id is not None
                and self.requester_agent_id == self.owner_agent_id
            ),
            scope_match=(
                self.requested_memory_scope is not None
                and self.owner_memory_scope is not None
                and self.requested_memory_scope == self.owner_memory_scope
            ),
            visibility=(self.visibility.value if self.visibility is not None else None),
            decision=self.decision.value,
            reason=self.reason_code.value,
            affected_count=self.affected_count,
        )


class MemoryAuthorizationError(RuntimeError):
    """授权拒绝；只携带 safe typed decision。"""

    def __init__(self, result: MemoryAuthorizationResult) -> None:
        self.result = result
        super().__init__(
            f"private memory access denied (reason={result.reason_code.value})"
        )


class MemoryAccessAuthorizer:
    """PRIVATE Memory 的唯一 requester/owner/scope policy。"""

    def __init__(
        self,
        observation_sink: Callable[[MemoryAuthorizationObservation], None] | None = None,
    ) -> None:
        self._observation_sink = observation_sink

    def authorize_private_read(
        self,
        requester: MemoryAccessPrincipal | None,
        owner_agent_id: str | None,
        owner_memory_scope: str | None,
        *,
        requested_memory_scope: str | None = None,
        visibility: MemoryVisibility | str = MemoryVisibility.PRIVATE,
        affected_count: int = 0,
    ) -> MemoryAuthorizationResult:
        return self._authorize(
            MemoryAuthorizationOperation.READ,
            requester,
            owner_agent_id,
            owner_memory_scope,
            requested_memory_scope=requested_memory_scope,
            visibility=visibility,
            affected_count=affected_count,
        )

    def authorize_private_create(
        self,
        requester: MemoryAccessPrincipal | None,
        owner_agent_id: str | None,
        owner_memory_scope: str | None,
        *,
        requested_memory_scope: str | None = None,
        visibility: MemoryVisibility | str = MemoryVisibility.PRIVATE,
        affected_count: int = 0,
    ) -> MemoryAuthorizationResult:
        return self._authorize(
            MemoryAuthorizationOperation.CREATE,
            requester,
            owner_agent_id,
            owner_memory_scope,
            requested_memory_scope=requested_memory_scope,
            visibility=visibility,
            affected_count=affected_count,
        )

    def authorize_private_update(
        self,
        requester: MemoryAccessPrincipal | None,
        owner_agent_id: str | None,
        owner_memory_scope: str | None,
        *,
        requested_memory_scope: str | None = None,
        visibility: MemoryVisibility | str = MemoryVisibility.PRIVATE,
        affected_count: int = 0,
    ) -> MemoryAuthorizationResult:
        return self._authorize(
            MemoryAuthorizationOperation.UPDATE,
            requester,
            owner_agent_id,
            owner_memory_scope,
            requested_memory_scope=requested_memory_scope,
            visibility=visibility,
            affected_count=affected_count,
        )

    def authorize_private_forget(
        self,
        requester: MemoryAccessPrincipal | None,
        owner_agent_id: str | None,
        owner_memory_scope: str | None,
        *,
        requested_memory_scope: str | None = None,
        visibility: MemoryVisibility | str = MemoryVisibility.PRIVATE,
        affected_count: int = 0,
    ) -> MemoryAuthorizationResult:
        return self._authorize(
            MemoryAuthorizationOperation.FORGET,
            requester,
            owner_agent_id,
            owner_memory_scope,
            requested_memory_scope=requested_memory_scope,
            visibility=visibility,
            affected_count=affected_count,
        )

    def authorize(
        self,
        operation: MemoryAuthorizationOperation | str,
        requester: MemoryAccessPrincipal | None,
        owner_agent_id: str | None,
        owner_memory_scope: str | None,
        *,
        requested_memory_scope: str | None = None,
        visibility: MemoryVisibility | str = MemoryVisibility.PRIVATE,
        affected_count: int = 0,
    ) -> MemoryAuthorizationResult:
        """通用 typed 入口；未知 operation 必须 DENY。"""
        try:
            parsed_operation = MemoryAuthorizationOperation(operation)
        except (TypeError, ValueError):
            parsed_operation = MemoryAuthorizationOperation.UNSUPPORTED
        if parsed_operation is MemoryAuthorizationOperation.UNSUPPORTED:
            result = MemoryAuthorizationResult(
                operation=parsed_operation,
                decision=MemoryAuthorizationDecision.DENY,
                reason_code=MemoryAuthorizationReason.UNSUPPORTED_OPERATION,
                requester_agent_id=(
                    requester.agent_id
                    if isinstance(requester, MemoryAccessPrincipal)
                    else None
                ),
                owner_agent_id=(owner_agent_id if isinstance(owner_agent_id, str) else None),
                requested_memory_scope=(
                    requested_memory_scope
                    if isinstance(requested_memory_scope, str)
                    else None
                ),
                owner_memory_scope=(
                    owner_memory_scope if isinstance(owner_memory_scope, str) else None
                ),
                visibility=None,
                affected_count=0,
            )
            self._observe(result)
            return result
        return self._authorize(
            parsed_operation,
            requester,
            owner_agent_id,
            owner_memory_scope,
            requested_memory_scope=requested_memory_scope,
            visibility=visibility,
            affected_count=affected_count,
        )

    def _authorize(
        self,
        operation: MemoryAuthorizationOperation,
        requester: MemoryAccessPrincipal | None,
        owner_agent_id: str | None,
        owner_memory_scope: str | None,
        *,
        requested_memory_scope: str | None,
        visibility: MemoryVisibility | str,
        affected_count: int,
    ) -> MemoryAuthorizationResult:
        if requested_memory_scope is None and isinstance(owner_memory_scope, str):
            requested_memory_scope = owner_memory_scope
        normalized_owner_scope = (
            owner_memory_scope.strip()
            if isinstance(owner_memory_scope, str)
            else owner_memory_scope
        )
        normalized_requested_scope = (
            requested_memory_scope.strip()
            if isinstance(requested_memory_scope, str)
            else requested_memory_scope
        )
        parsed_visibility: MemoryVisibility | None
        try:
            parsed_visibility = MemoryVisibility(visibility)
        except (TypeError, ValueError):
            parsed_visibility = None
        requester_agent_id = (
            requester.agent_id if isinstance(requester, MemoryAccessPrincipal) else None
        )
        if parsed_visibility is None or parsed_visibility is MemoryVisibility.PROJECT:
            reason = MemoryAuthorizationReason.UNSUPPORTED_VISIBILITY
        elif not isinstance(requester, MemoryAccessPrincipal):
            reason = MemoryAuthorizationReason.UNKNOWN_REQUESTER
        elif not isinstance(owner_agent_id, str) or not owner_agent_id.strip():
            reason = MemoryAuthorizationReason.UNKNOWN_OWNER
        elif not isinstance(normalized_owner_scope, str) or not normalized_owner_scope:
            reason = MemoryAuthorizationReason.SCOPE_MISMATCH
        elif not isinstance(normalized_requested_scope, str) or not normalized_requested_scope:
            reason = MemoryAuthorizationReason.SCOPE_MISMATCH
        elif normalized_owner_scope != normalized_requested_scope:
            reason = MemoryAuthorizationReason.SCOPE_MISMATCH
        elif requester.agent_id != owner_agent_id.strip():
            reason = MemoryAuthorizationReason.FOREIGN_PRIVATE_OWNER
        else:
            reason = MemoryAuthorizationReason.OWNER_MATCH
        result = MemoryAuthorizationResult(
            operation=operation,
            decision=(
                MemoryAuthorizationDecision.ALLOW
                if reason is MemoryAuthorizationReason.OWNER_MATCH
                else MemoryAuthorizationDecision.DENY
            ),
            reason_code=reason,
            requester_agent_id=requester_agent_id,
            owner_agent_id=(owner_agent_id.strip() if isinstance(owner_agent_id, str) else None),
            requested_memory_scope=normalized_requested_scope,
            owner_memory_scope=normalized_owner_scope,
            visibility=parsed_visibility,
            affected_count=max(0, affected_count) if isinstance(affected_count, int) else 0,
        )
        self._observe(result)
        return result

    def _observe(self, result: MemoryAuthorizationResult) -> None:
        if self._observation_sink is None:
            return
        try:
            self._observation_sink(result.observation())
        except Exception:
            return


# Architecture-facing alias; both names describe the same code-owned policy.
PrivateMemoryAuthorizationPolicy = MemoryAccessAuthorizer


__all__ = [
    "MemoryAccessAuthorizer",
    "MemoryAccessPrincipal",
    "MemoryAuthorizationDecision",
    "MemoryAuthorizationErrorCode",
    "MemoryAuthorizationError",
    "MemoryAuthorizationObservation",
    "MemoryAuthorizationOperation",
    "MemoryAuthorizationReason",
    "MemoryAuthorizationResult",
    "MemoryVisibility",
    "PrivateMemoryAuthorizationPolicy",
]
