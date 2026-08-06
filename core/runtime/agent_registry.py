#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Stage 2.5 智能体身份、能力和静态权限的唯一事实源。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from types import MappingProxyType
from typing import Iterable, Mapping

from core.runtime.planning import ExecutionKind, OutputPolicy

_SAFE_AGENT_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_TYPE_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


class ResultContentType(str, Enum):
    TEXT = "text"
    STRUCTURED = "structured"


class AgentRegistryErrorCode(str, Enum):
    INVALID_REGISTRATION = "INVALID_REGISTRATION"
    DUPLICATE_AGENT = "DUPLICATE_AGENT"
    UNKNOWN_AGENT = "UNKNOWN_AGENT"
    AGENT_DISABLED = "AGENT_DISABLED"
    ENTRY_AGENT_NOT_ALLOWED = "ENTRY_AGENT_NOT_ALLOWED"
    DELEGATED_AGENT_NOT_ALLOWED = "DELEGATED_AGENT_NOT_ALLOWED"


class AgentRegistryError(LookupError):
    """只包含稳定代码和安全说明的 Registry 错误。"""

    def __init__(self, error_code: AgentRegistryErrorCode, safe_message: str) -> None:
        self.error_code = error_code
        self.safe_message = safe_message
        super().__init__(f"{safe_message} (error_code={error_code.value})")


@dataclass(frozen=True, slots=True)
class AgentRegistration:
    agent_id: str
    execution_adapter_id: str
    display_name: str
    role: str
    avatar: str
    enabled: bool
    entry_allowed: bool
    entry_output_policy: OutputPolicy
    model_direct_allowed: bool
    delegation_allowed: bool
    delegated_output_policy: OutputPolicy
    allows_single_delegated_passthrough: bool
    synthesis_only: bool
    supports_parallel: bool
    accepted_input_types: frozenset[str]
    produced_result_types: frozenset[ResultContentType]
    capabilities: frozenset[str]
    deterministic_aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if _SAFE_AGENT_ID.fullmatch(self.agent_id) is None:
            raise ValueError("agent_id 必须是安全、稳定的标识符")
        if _SAFE_TYPE_ID.fullmatch(self.execution_adapter_id) is None:
            raise ValueError("execution_adapter_id 必须是安全、稳定的符号标识")
        if not all(isinstance(value, str) and value.strip() for value in (self.display_name, self.role, self.avatar)):
            raise ValueError("Agent 展示元数据不能为空")
        for value in (self.enabled, self.entry_allowed, self.model_direct_allowed, self.delegation_allowed, self.allows_single_delegated_passthrough, self.synthesis_only, self.supports_parallel):
            if type(value) is not bool:
                raise ValueError("Agent 布尔策略字段必须是 bool")
        if not isinstance(self.entry_output_policy, OutputPolicy) or not isinstance(self.delegated_output_policy, OutputPolicy):
            raise ValueError("Agent output policy 必须合法")
        if self.entry_allowed and self.entry_output_policy is not OutputPolicy.FINAL_PASSTHROUGH:
            raise ValueError("entry Agent 只能获得 FINAL_PASSTHROUGH")
        if self.model_direct_allowed and not self.entry_allowed:
            raise ValueError("model direct Agent 必须允许 entry")
        if self.synthesis_only:
            if self.entry_allowed or not self.delegation_allowed:
                raise ValueError("synthesis Agent 必须禁止 entry 且允许受控委派")
            if self.delegated_output_policy is not OutputPolicy.FINAL_SYNTHESIS:
                raise ValueError("synthesis Agent 只能获得 FINAL_SYNTHESIS")
        elif self.delegated_output_policy is OutputPolicy.FINAL_SYNTHESIS:
            raise ValueError("非 synthesis Agent 不得获得 FINAL_SYNTHESIS")
        if self.allows_single_delegated_passthrough and (
            not self.delegation_allowed
            or self.synthesis_only
            or self.delegated_output_policy is not OutputPolicy.INTERNAL
        ):
            raise ValueError("delegated passthrough 例外必须建立在 INTERNAL specialist 上")
        if not self.accepted_input_types or any(
            not isinstance(item, str) or _SAFE_TYPE_ID.fullmatch(item) is None
            for item in self.accepted_input_types
        ):
            raise ValueError("accepted_input_types 必须是非空安全标识集合")
        if not self.produced_result_types or any(not isinstance(item, ResultContentType) for item in self.produced_result_types):
            raise ValueError("produced_result_types 必须是非空合法集合")
        if any(not isinstance(item, str) or _SAFE_TYPE_ID.fullmatch(item) is None for item in self.capabilities):
            raise ValueError("capabilities 只能包含安全标识")
        if any(not isinstance(alias, str) or not alias.strip() for alias in self.deterministic_aliases):
            raise ValueError("deterministic_aliases 只能包含非空字符串")

    @property
    def execution_kind(self) -> ExecutionKind:
        return ExecutionKind.SYNTHESIS if self.synthesis_only else ExecutionKind.AGENT


class AgentRegistry:
    """进程级只读 Registry；不保存任何 Run 或用户数据。"""

    __slots__ = ("_registrations", "_ordered_ids", "_locked")

    def __init__(self, registrations: Iterable[AgentRegistration]) -> None:
        ordered = tuple(registrations)
        mapping: dict[str, AgentRegistration] = {}
        for registration in ordered:
            if not isinstance(registration, AgentRegistration):
                raise AgentRegistryError(
                    AgentRegistryErrorCode.INVALID_REGISTRATION,
                    "Registry 只能包含合法 AgentRegistration",
                )
            if registration.agent_id in mapping:
                raise AgentRegistryError(
                    AgentRegistryErrorCode.DUPLICATE_AGENT,
                    "Agent 标识不允许重复",
                )
            mapping[registration.agent_id] = registration
        if not mapping:
            raise AgentRegistryError(
                AgentRegistryErrorCode.INVALID_REGISTRATION,
                "Registry 至少需要一个 Agent",
            )
        object.__setattr__(self, "_registrations", MappingProxyType(mapping))
        object.__setattr__(self, "_ordered_ids", tuple(mapping))
        object.__setattr__(self, "_locked", True)

    def __setattr__(self, name, value) -> None:
        if getattr(self, "_locked", False):
            raise AttributeError("AgentRegistry 是不可变对象")
        object.__setattr__(self, name, value)

    def __repr__(self) -> str:
        return f"AgentRegistry(agent_ids={self._ordered_ids!r})"

    @property
    def agent_ids(self) -> tuple[str, ...]:
        return self._ordered_ids

    def resolve(self, agent_id: str) -> AgentRegistration:
        registration = self._registrations.get(agent_id)
        if registration is None:
            raise AgentRegistryError(AgentRegistryErrorCode.UNKNOWN_AGENT, "Agent 未注册")
        if not registration.enabled:
            raise AgentRegistryError(AgentRegistryErrorCode.AGENT_DISABLED, "Agent 当前不可用")
        return registration

    def require_entry(self, agent_id: str) -> AgentRegistration:
        registration = self.resolve(agent_id)
        if not registration.entry_allowed:
            raise AgentRegistryError(
                AgentRegistryErrorCode.ENTRY_AGENT_NOT_ALLOWED,
                "Agent 不允许作为 entry",
            )
        return registration

    def require_delegated(self, agent_id: str) -> AgentRegistration:
        registration = self.resolve(agent_id)
        if not registration.delegation_allowed or registration.synthesis_only:
            raise AgentRegistryError(
                AgentRegistryErrorCode.DELEGATED_AGENT_NOT_ALLOWED,
                "Agent 不允许作为 specialist",
            )
        return registration

    def synthesis_registration(self) -> AgentRegistration:
        matches = tuple(
            registration
            for registration in self._registrations.values()
            if registration.enabled and registration.synthesis_only
        )
        if len(matches) != 1:
            raise AgentRegistryError(
                AgentRegistryErrorCode.INVALID_REGISTRATION,
                "Registry 必须有且仅有一个 synthesis Agent",
            )
        return matches[0]

    def delegated_specialist_ids(self) -> tuple[str, ...]:
        return tuple(
            agent_id
            for agent_id in self._ordered_ids
            if self._registrations[agent_id].enabled
            and self._registrations[agent_id].delegation_allowed
            and not self._registrations[agent_id].synthesis_only
        )

    def legacy_display_config(self) -> dict[str, dict[str, str]]:
        """为尚未迁移的 AgentRouter 提供同源展示配置副本。"""
        return {
            agent_id: {
                "name": registration.display_name,
                "role": registration.role,
                "avatar": registration.avatar,
            }
            for agent_id in self._ordered_ids
            if (registration := self._registrations[agent_id]).entry_allowed
        }


def _registration(
    agent_id: str,
    execution_adapter_id: str,
    display_name: str,
    role: str,
    avatar: str,
    *,
    entry_allowed: bool,
    model_direct_allowed: bool = False,
    delegation_allowed: bool,
    delegated_output_policy: OutputPolicy,
    capabilities: frozenset[str],
    aliases: tuple[str, ...],
    single_passthrough: bool = False,
    synthesis_only: bool = False,
) -> AgentRegistration:
    return AgentRegistration(
        agent_id=agent_id,
        execution_adapter_id=execution_adapter_id,
        display_name=display_name,
        role=role,
        avatar=avatar,
        enabled=True,
        entry_allowed=entry_allowed,
        entry_output_policy=(
            OutputPolicy.FINAL_PASSTHROUGH
            if entry_allowed
            else OutputPolicy.INTERNAL
        ),
        model_direct_allowed=model_direct_allowed,
        delegation_allowed=delegation_allowed,
        delegated_output_policy=delegated_output_policy,
        allows_single_delegated_passthrough=single_passthrough,
        synthesis_only=synthesis_only,
        supports_parallel=delegation_allowed and not synthesis_only,
        accepted_input_types=frozenset({"text"}),
        produced_result_types=frozenset({ResultContentType.TEXT}),
        capabilities=capabilities,
        deterministic_aliases=aliases,
    )


DEFAULT_AGENT_REGISTRY = AgentRegistry(
    (
        _registration(
            "core_router", "core_router_adapter", "Core Router", "处理通用问题，并协调辅助智能体。", "avatar_router.png",
            entry_allowed=True, model_direct_allowed=True, delegation_allowed=False,
            delegated_output_policy=OutputPolicy.INTERNAL,
            capabilities=frozenset({"general_chat", "planning"}),
            aliases=("core_router", "主智能体", "核心智能体"),
        ),
        _registration(
            "data_analyst", "data_analyst_adapter", "Data Analyst", "分析 CSV 和 Excel 文件，并总结洞见。", "avatar_excel.png",
            entry_allowed=True, delegation_allowed=True,
            delegated_output_policy=OutputPolicy.INTERNAL,
            capabilities=frozenset({"data_analysis"}),
            aliases=("data_analyst", "数据分析师", "数据专家", "数据库", "csv", "excel"),
        ),
        _registration(
            "code_expert", "code_expert_adapter", "Code Expert", "审查代码、排查问题并改进架构。", "avatar_code.png",
            entry_allowed=True, delegation_allowed=True,
            delegated_output_policy=OutputPolicy.INTERNAL,
            capabilities=frozenset({"code_reasoning"}),
            aliases=("code_expert", "代码专家"),
        ),
        _registration(
            "knowledge_expert", "knowledge_expert_adapter", "Knowledge Expert", "在可用时依据本地知识库回答问题。", "avatar_knowledge.png",
            entry_allowed=True, delegation_allowed=True,
            delegated_output_policy=OutputPolicy.INTERNAL,
            capabilities=frozenset({"rag"}),
            aliases=("knowledge_expert", "知识专家", "本地知识专家"),
            single_passthrough=True,
        ),
        _registration(
            "synthesis_agent", "synthesis_agent_adapter", "Synthesis Agent", "汇总已授权的专业结果。", "avatar_router.png",
            entry_allowed=False, delegation_allowed=True,
            delegated_output_policy=OutputPolicy.FINAL_SYNTHESIS,
            capabilities=frozenset({"synthesis", "structured_output"}),
            aliases=("synthesis_agent",), synthesis_only=True,
        ),
    )
)


__all__ = [
    "AgentRegistration",
    "AgentRegistry",
    "AgentRegistryError",
    "AgentRegistryErrorCode",
    "DEFAULT_AGENT_REGISTRY",
    "ResultContentType",
]
