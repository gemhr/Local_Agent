#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WP3 Agent execution adapter contract and process-scoped factory.

The factory maps a stable ``execution_adapter_id`` (declared by the Agent
Registry) to an ``AgentExecutionAdapter`` implementation. It never stores user
requests, Run state or results, and adapter instances never hold Run-scoped
raw data.
"""

from __future__ import annotations

import asyncio
from enum import Enum
from types import MappingProxyType
from typing import Iterable, Protocol, runtime_checkable

from core.runtime.agent_registry import AgentRegistry
from core.runtime.budget import BudgetExceededError
from core.runtime.cancellation import RunCancelledError
from core.runtime.context import RunContext, RunDeadlineExceededError
from core.runtime.event_emitter import StepEventEmitter
from core.runtime.history_policy import HistoryPolicy
from core.runtime.invocation_bindings import InvocationRole
from core.runtime.planning import ExecutionKind, TaskCapabilityRequirements
from core.runtime.resource_authorization import ResourceAuthorizationError
from core.runtime.step_result import (
    ResultContentType,
    ResultDisposition,
    SecurityDenialCode,
    StepResult,
)
from core.runtime.step_result_store import DependencyResultView
from core.runtime.tool_governance import ToolGovernanceError, ToolGovernanceErrorCode


class AgentAdapterErrorCode(str, Enum):
    UNKNOWN_ADAPTER = "UNKNOWN_ADAPTER"
    INVALID_ADAPTER = "INVALID_ADAPTER"
    DUPLICATE_ADAPTER = "DUPLICATE_ADAPTER"
    ADAPTER_NOT_RESOLVABLE = "ADAPTER_NOT_RESOLVABLE"
    AGENT_ROUTER_CALL_FAILED = "AGENT_ROUTER_CALL_FAILED"
    AGENT_ROUTER_RESULT_INVALID = "AGENT_ROUTER_RESULT_INVALID"
    REQUEST_INVALID = "REQUEST_INVALID"


class AgentAdapterError(RuntimeError):
    """Safe adapter error that never carries raw instruction or result."""

    def __init__(
        self,
        error_code: AgentAdapterErrorCode,
        safe_message: str,
    ) -> None:
        self.error_code = error_code
        self.safe_message = safe_message
        super().__init__(f"{safe_message} (error_code={error_code.value})")


class AgentExecutionRequest:
    """Raw-bearing per-Step request built by MultiAgentDriver.

    Intentionally not a dataclass: ``dataclasses.asdict`` cannot export the
    instruction or dependency content, repr is redacted, and pickling is
    rejected.
    """

    __slots__ = (
        "_step_id",
        "_agent_id",
        "_instruction",
        "_invocation_role",
        "_history_policy",
        "_execution_kind",
        "_input_type",
        "_capability_requirements",
        "_content_type",
        "_dependency_results",
        "_event_emitter",
        "_fault_controller",
        "_locked",
    )

    def __init__(
        self,
        *,
        step_id: str,
        agent_id: str,
        instruction: str,
        invocation_role: InvocationRole = InvocationRole.ENTRY,
        history_policy: HistoryPolicy = HistoryPolicy.AGENT_SCOPE,
        execution_kind: ExecutionKind,
        input_type: str,
        capability_requirements: TaskCapabilityRequirements,
        content_type: ResultContentType = ResultContentType.TEXT,
        dependency_results: DependencyResultView | None = None,
        event_emitter: StepEventEmitter | None = None,
        fault_controller=None,
    ) -> None:
        if not isinstance(step_id, str) or not step_id.strip():
            raise AgentAdapterError(
                AgentAdapterErrorCode.REQUEST_INVALID,
                "step_id 不能为空",
            )
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise AgentAdapterError(
                AgentAdapterErrorCode.REQUEST_INVALID,
                "agent_id 不能为空",
            )
        if not isinstance(instruction, str) or not instruction.strip():
            raise AgentAdapterError(
                AgentAdapterErrorCode.REQUEST_INVALID,
                "instruction 不能为空",
            )
        if not isinstance(invocation_role, InvocationRole):
            raise AgentAdapterError(
                AgentAdapterErrorCode.REQUEST_INVALID,
                "invocation_role 必须是 InvocationRole",
            )
        if not isinstance(history_policy, HistoryPolicy):
            raise AgentAdapterError(
                AgentAdapterErrorCode.REQUEST_INVALID,
                "history_policy 必须是 HistoryPolicy",
            )
        if not isinstance(execution_kind, ExecutionKind):
            raise AgentAdapterError(
                AgentAdapterErrorCode.REQUEST_INVALID,
                "execution_kind 必须合法",
            )
        if not isinstance(input_type, str) or not input_type.strip():
            raise AgentAdapterError(
                AgentAdapterErrorCode.REQUEST_INVALID,
                "input_type 不能为空",
            )
        if not isinstance(capability_requirements, TaskCapabilityRequirements):
            raise AgentAdapterError(
                AgentAdapterErrorCode.REQUEST_INVALID,
                "capability_requirements 必须合法",
            )
        if not isinstance(content_type, ResultContentType):
            raise AgentAdapterError(
                AgentAdapterErrorCode.REQUEST_INVALID,
                "content_type 必须合法",
            )
        if dependency_results is not None and not isinstance(
            dependency_results, DependencyResultView
        ):
            raise AgentAdapterError(
                AgentAdapterErrorCode.REQUEST_INVALID,
                "dependency_results 必须是只读视图",
            )
        if event_emitter is not None and not isinstance(
            event_emitter, StepEventEmitter
        ):
            raise AgentAdapterError(
                AgentAdapterErrorCode.REQUEST_INVALID,
                "event_emitter 必须合法",
            )
        object.__setattr__(self, "_step_id", step_id.strip())
        object.__setattr__(self, "_agent_id", agent_id.strip())
        object.__setattr__(self, "_instruction", instruction)
        object.__setattr__(self, "_invocation_role", invocation_role)
        object.__setattr__(self, "_history_policy", history_policy)
        object.__setattr__(self, "_execution_kind", execution_kind)
        object.__setattr__(self, "_input_type", input_type.strip())
        object.__setattr__(
            self, "_capability_requirements", capability_requirements
        )
        object.__setattr__(self, "_content_type", content_type)
        object.__setattr__(self, "_dependency_results", dependency_results)
        object.__setattr__(self, "_event_emitter", event_emitter)
        object.__setattr__(self, "_fault_controller", fault_controller)
        object.__setattr__(self, "_locked", True)

    def __setattr__(self, name, value) -> None:
        if getattr(self, "_locked", False):
            raise AttributeError("AgentExecutionRequest 是不可变对象")
        object.__setattr__(self, name, value)

    @property
    def step_id(self) -> str:
        return self._step_id

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def instruction(self) -> str:
        return self._instruction

    @property
    def invocation_role(self) -> InvocationRole:
        return self._invocation_role

    @property
    def history_policy(self) -> HistoryPolicy:
        return self._history_policy

    @property
    def execution_kind(self) -> ExecutionKind:
        return self._execution_kind

    @property
    def input_type(self) -> str:
        return self._input_type

    @property
    def capability_requirements(self) -> TaskCapabilityRequirements:
        return self._capability_requirements

    @property
    def content_type(self) -> ResultContentType:
        return self._content_type

    @property
    def dependency_results(self) -> DependencyResultView | None:
        return self._dependency_results

    @property
    def event_emitter(self) -> StepEventEmitter | None:
        return self._event_emitter

    @property
    def fault_controller(self):
        return self._fault_controller

    def __repr__(self) -> str:
        return (
            "AgentExecutionRequest("
            f"step_id={self.step_id!r}, agent_id={self.agent_id!r}, "
            f"invocation_role={self.invocation_role.value!r}, "
            f"execution_kind={self.execution_kind.value!r}, "
            f"input_type={self.input_type!r}, "
            f"content_type={self.content_type.value!r}, "
            f"dependency_count={len(self.dependency_results) if self.dependency_results is not None else 0}, "
            "instruction=<redacted>, dependency_results=<redacted>)"
        )

    def __getstate__(self):
        raise TypeError("AgentExecutionRequest 不允许序列化")


_GOVERNANCE_DENIAL_CODES = {
    ToolGovernanceErrorCode.PERMISSION_DENIED:
        SecurityDenialCode.TOOL_PERMISSION_DENIED,
    ToolGovernanceErrorCode.APPROVAL_REQUIRED:
        SecurityDenialCode.TOOL_APPROVAL_REQUIRED,
    ToolGovernanceErrorCode.UNKNOWN_PRINCIPAL:
        SecurityDenialCode.TOOL_GOVERNANCE_UNKNOWN_PRINCIPAL,
    ToolGovernanceErrorCode.POLICY_MISSING:
        SecurityDenialCode.TOOL_GOVERNANCE_POLICY_MISSING,
    ToolGovernanceErrorCode.RISK_UNCLASSIFIED:
        SecurityDenialCode.TOOL_RISK_UNCLASSIFIED,
}


class AgentAdapterResult:
    """Typed adapter result; exists only inside the Driver call stack."""

    __slots__ = (
        "_content_type",
        "_content",
        "_complete",
        "_result_disposition",
        "_security_denial_code",
        "_locked",
    )

    def __init__(
        self,
        content_type: ResultContentType,
        content: str,
        complete: bool = True,
        *,
        result_disposition: ResultDisposition = ResultDisposition.NORMAL,
        security_denial_code: SecurityDenialCode | None = None,
    ) -> None:
        if not isinstance(content_type, ResultContentType):
            raise AgentAdapterError(
                AgentAdapterErrorCode.AGENT_ROUTER_RESULT_INVALID,
                "adapter result content_type 必须合法",
            )
        if not isinstance(content, str) or not content.strip():
            raise AgentAdapterError(
                AgentAdapterErrorCode.AGENT_ROUTER_RESULT_INVALID,
                "adapter result content 不能为空",
            )
        if type(complete) is not bool:
            raise AgentAdapterError(
                AgentAdapterErrorCode.AGENT_ROUTER_RESULT_INVALID,
                "adapter result complete 必须是 bool",
            )
        if not isinstance(result_disposition, ResultDisposition):
            raise AgentAdapterError(
                AgentAdapterErrorCode.AGENT_ROUTER_RESULT_INVALID,
                "adapter result disposition 必须合法",
            )
        if security_denial_code is not None and not isinstance(
            security_denial_code, SecurityDenialCode
        ):
            raise AgentAdapterError(
                AgentAdapterErrorCode.AGENT_ROUTER_RESULT_INVALID,
                "adapter security denial code 必须合法",
            )
        if (
            result_disposition is ResultDisposition.NORMAL
            and security_denial_code is not None
        ) or (
            result_disposition is ResultDisposition.SECURITY_DENIED
            and security_denial_code is None
        ):
            raise AgentAdapterError(
                AgentAdapterErrorCode.AGENT_ROUTER_RESULT_INVALID,
                "adapter result disposition 与 security denial code 不一致",
            )
        object.__setattr__(self, "_content_type", content_type)
        object.__setattr__(self, "_content", content)
        object.__setattr__(self, "_complete", complete)
        object.__setattr__(self, "_result_disposition", result_disposition)
        object.__setattr__(self, "_security_denial_code", security_denial_code)
        object.__setattr__(self, "_locked", True)

    def __setattr__(self, name, value) -> None:
        if getattr(self, "_locked", False):
            raise AttributeError("AgentAdapterResult 是不可变对象")
        object.__setattr__(self, name, value)

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

    def __repr__(self) -> str:
        denial_code = (
            self.security_denial_code.value if self.security_denial_code else None
        )
        return (
            "AgentAdapterResult("
            f"content_type={self.content_type.value!r}, "
            f"char_count={len(self.content)}, complete={self.complete!r}, "
            f"result_disposition={self.result_disposition.value!r}, "
            f"security_denial_code={denial_code!r}, "
            "content=<redacted>)"
        )

    def __getstate__(self):
        raise TypeError("AgentAdapterResult 不允许序列化")

    def to_step_result(
        self,
        *,
        step_id: str,
        producer_agent_id: str,
    ) -> StepResult:
        return StepResult(
            step_id=step_id,
            producer_agent_id=producer_agent_id,
            content_type=self.content_type,
            content=self.content,
            complete=self.complete,
            result_disposition=self.result_disposition,
            security_denial_code=self.security_denial_code,
        )


@runtime_checkable
class AgentExecutionAdapter(Protocol):
    """Typed adapter contract.

    The concrete production adapters are synchronous because the unified
    ``AgentRouter.complete_single_agent`` contract is synchronous and is
    executed inside the bounded blocking executor owned by the Runtime.
    """

    def execute(
        self,
        request: AgentExecutionRequest,
        run_context: RunContext,
    ) -> AgentAdapterResult:
        ...


class AgentRouterSingleAgentAdapter:
    """Generic adapter for the unified single-Agent router path.

    It never branches on concrete Agent IDs, never writes Store or AgentState,
    always calls with ``persist=False``, reuses the existing Model/Tool/
    Retrieval events, and returns a complete typed result.
    """

    def __init__(self, router) -> None:
        self._router = router

    def execute(
        self,
        request: AgentExecutionRequest,
        run_context: RunContext,
    ) -> AgentAdapterResult:
        if not isinstance(request, AgentExecutionRequest):
            raise AgentAdapterError(
                AgentAdapterErrorCode.REQUEST_INVALID,
                "adapter 需要 AgentExecutionRequest",
            )
        try:
            text = self._router.complete_single_agent(
                request.agent_id,
                request.instruction,
                run_context=run_context,
                capability_requirements=request.capability_requirements,
                persist=False,
                history_policy=request.history_policy,
                raise_security_denial=True,
                event_emitter=request.event_emitter,
                fault_controller=request.fault_controller,
            )
        except (
            asyncio.CancelledError,
            RunCancelledError,
            RunDeadlineExceededError,
            BudgetExceededError,
        ):
            raise
        except ToolGovernanceError as denied:
            denial_code = _GOVERNANCE_DENIAL_CODES.get(denied.error_code)
            if denial_code is None:
                raise AgentAdapterError(
                    AgentAdapterErrorCode.AGENT_ROUTER_CALL_FAILED,
                    "Governance 返回了非 runtime denial 错误",
                ) from None
            return AgentAdapterResult(
                request.content_type,
                denied.safe_message,
                complete=True,
                result_disposition=ResultDisposition.SECURITY_DENIED,
                security_denial_code=denial_code,
            )
        except ResourceAuthorizationError as denied:
            return AgentAdapterResult(
                request.content_type,
                denied.safe_message,
                complete=True,
                result_disposition=ResultDisposition.SECURITY_DENIED,
                security_denial_code=SecurityDenialCode.TOOL_RESOURCE_DENIED,
            )
        except Exception:
            raise AgentAdapterError(
                AgentAdapterErrorCode.AGENT_ROUTER_CALL_FAILED,
                "专业 Agent 调用失败",
            ) from None
        if not isinstance(text, str) or not text.strip():
            raise AgentAdapterError(
                AgentAdapterErrorCode.AGENT_ROUTER_RESULT_INVALID,
                "专业 Agent 返回了非法结果",
            )
        return AgentAdapterResult(request.content_type, text, complete=True)


class AgentAdapterFactory:
    """Process-scoped immutable adapter resolver keyed by stable adapter ID."""

    __slots__ = ("_adapters", "_adapter_ids", "_locked")

    def __init__(
        self,
        registry: AgentRegistry,
        adapters: Iterable[tuple[str, AgentExecutionAdapter]],
    ) -> None:
        if not isinstance(registry, AgentRegistry):
            raise TypeError("AgentAdapterFactory 需要 AgentRegistry")
        mapping: dict[str, AgentExecutionAdapter] = {}
        for adapter_id, adapter in adapters:
            if (
                not isinstance(adapter_id, str)
                or not adapter_id.strip()
            ):
                raise AgentAdapterError(
                    AgentAdapterErrorCode.INVALID_ADAPTER,
                    "adapter ID 必须是非空符号标识",
                )
            if adapter is None or not callable(
                getattr(adapter, "execute", None)
            ):
                raise AgentAdapterError(
                    AgentAdapterErrorCode.INVALID_ADAPTER,
                    "adapter 必须实现 execute",
                )
            if adapter_id in mapping:
                raise AgentAdapterError(
                    AgentAdapterErrorCode.DUPLICATE_ADAPTER,
                    "adapter ID 不允许重复",
                )
            mapping[adapter_id] = adapter
        if not mapping:
            raise AgentAdapterError(
                AgentAdapterErrorCode.INVALID_ADAPTER,
                "adapter 映射不能为空",
            )
        unresolved: list[str] = []
        for agent_id in registry.agent_ids:
            registration = registry.resolve(agent_id)
            if not registration.enabled:
                continue
            if registration.execution_adapter_id not in mapping:
                unresolved.append(registration.execution_adapter_id)
        if unresolved:
            raise AgentAdapterError(
                AgentAdapterErrorCode.ADAPTER_NOT_RESOLVABLE,
                "Registry 存在无法解析的 adapter ID",
            )
        object.__setattr__(self, "_adapters", MappingProxyType(mapping))
        object.__setattr__(self, "_adapter_ids", tuple(mapping))
        object.__setattr__(self, "_locked", True)

    def __setattr__(self, name, value) -> None:
        if getattr(self, "_locked", False):
            raise AttributeError("AgentAdapterFactory 是不可变对象")
        object.__setattr__(self, name, value)

    @property
    def adapter_ids(self) -> tuple[str, ...]:
        return self._adapter_ids

    def resolve(self, adapter_id: str) -> AgentExecutionAdapter:
        adapter = self._adapters.get(adapter_id)
        if adapter is None:
            raise AgentAdapterError(
                AgentAdapterErrorCode.UNKNOWN_ADAPTER,
                "未知 execution_adapter_id",
            )
        return adapter

    def __repr__(self) -> str:
        return f"AgentAdapterFactory(adapter_ids={self._adapter_ids!r})"


__all__ = [
    "AgentAdapterError",
    "AgentAdapterErrorCode",
    "AgentAdapterFactory",
    "AgentAdapterResult",
    "AgentExecutionAdapter",
    "AgentExecutionRequest",
    "AgentRouterSingleAgentAdapter",
]
