#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tool Governance v1（INTERNAL_RC，非 PUBLIC_STABLE）。

WP2-B 最小治理边界，严格按 `20_codex_decision.md` 冻结：

- ``ToolPolicyCatalog``：静态 policy facts 唯一事实源（APPLICATION_SCOPE /
  process-local，construct -> register -> validate -> freeze -> runtime read-only）。
- ``ToolGovernanceService``：唯一 invocation-time Authority，只回答
  permission / effective risk / approval requirement；AgentRouter 只调用它。
- 两级 Gate：静态 Permission（``authorize_tool``）-> build/spec -> invocation
  Risk/Approval（``evaluate_invocation``）。任一非 ``ALLOW`` 均在
  ``ToolExecutionService`` 之前 fail closed。

本模块不复制 ``ToolExecutionSpec`` 字段；dynamic execution truth 仍唯一来自
``ToolAdapter.spec_for(invocation)``。Governance 只处理 Agent ID、canonical
Tool name、固定 enum/code、risk classification 与内部 run/step scope；不保存
raw arguments / path / prompt / output / policy allowlist。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from types import MappingProxyType

from core.runtime.agent_registry import AgentRegistry, AgentRegistryError
from core.runtime.retry import OperationIdempotency
from core.runtime.tool_contract import (
    ToolExecutionSpec,
    ToolInvocation,
    ToolSideEffectKind,
)
from core.runtime.tool_registry import ToolRegistry, ToolRegistration

_SAFE_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# 当前生产 Agent inventory 冻结值（与 DEFAULT_AGENT_REGISTRY 一致，由测试断言
# 阻止 drift；Catalog freeze 亦只读校验每个 referenced Agent 存在且 enabled）。
PRODUCTION_AGENT_IDS = frozenset(
    {
        "core_router",
        "data_analyst",
        "code_expert",
        "knowledge_expert",
        "synthesis_agent",
    }
)


class ToolRiskFact(str, Enum):
    """静态 Tool 风险事实；只记录 execution spec 无法表达的 facts。"""

    ARBITRARY_LOCAL_FILESYSTEM_READ = "ARBITRARY_LOCAL_FILESYSTEM_READ"
    SYSTEM_INFORMATION_READ = "SYSTEM_INFORMATION_READ"


class ToolRiskLevel(str, Enum):
    """治理层 effective risk tier；只实现 LOW/MEDIUM/HIGH。"""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ToolGovernanceOutcome(str, Enum):
    """统一 governance result；禁止用 bool/None 表达。"""

    ALLOW = "ALLOW"
    DENY = "DENY"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"


class ToolGovernanceErrorCode(str, Enum):
    """Governance 固定安全错误码（runtime decisions + construction/lifecycle）。"""

    # runtime decisions
    PERMISSION_DENIED = "TOOL_PERMISSION_DENIED"
    APPROVAL_REQUIRED = "TOOL_APPROVAL_REQUIRED"
    UNKNOWN_PRINCIPAL = "TOOL_GOVERNANCE_UNKNOWN_PRINCIPAL"
    POLICY_MISSING = "TOOL_GOVERNANCE_POLICY_MISSING"
    RISK_UNCLASSIFIED = "TOOL_RISK_UNCLASSIFIED"
    # construction / lifecycle
    INVALID = "TOOL_GOVERNANCE_INVALID"
    DUPLICATE = "TOOL_GOVERNANCE_DUPLICATE"
    NOT_FROZEN = "TOOL_GOVERNANCE_NOT_FROZEN"
    FROZEN = "TOOL_GOVERNANCE_FROZEN"


class ToolGovernanceError(RuntimeError):
    """Governance typed failure；只携带固定 code + 固定 safe message。

    用于 AgentRouter seam 终止当前 Tool attempt 或 Catalog/Service 生命周期
    失败。它不是 ``ToolExecutionError``（Tool 未开始执行），也不是
    ``ToolRegistryError``（existence 与 authorization 是两个维度）。
    """

    def __init__(self, error_code: ToolGovernanceErrorCode, safe_message: str) -> None:
        if not isinstance(error_code, ToolGovernanceErrorCode):
            raise TypeError("error_code 必须是 ToolGovernanceErrorCode")
        if not isinstance(safe_message, str) or not safe_message.strip():
            raise ValueError("safe_message 必须是固定非空字符串")
        self.error_code = error_code
        self.safe_message = safe_message
        super().__init__(f"{safe_message} (error_code={error_code.value})")


@dataclass(frozen=True, slots=True)
class ToolGovernanceContext:
    """frozen 治理 context；只保存 WHO + execution scope，不保存任何正文。"""

    principal_agent_id: str
    run_id: str
    step_id: str

    def __post_init__(self) -> None:
        for name, value in (
            ("principal_agent_id", self.principal_agent_id),
            ("run_id", self.run_id),
            ("step_id", self.step_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ToolGovernanceError(
                    ToolGovernanceErrorCode.INVALID,
                    "governance context 字段必须是非空字符串",
                )


@dataclass(frozen=True, slots=True)
class ToolGovernanceDecision:
    """frozen 决策；不保存 raw args / path / prompt / output / policy 内部。"""

    outcome: ToolGovernanceOutcome
    risk_level: ToolRiskLevel | None = None
    risk_facts: tuple[ToolRiskFact, ...] = ()
    safe_error_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ToolGovernanceOutcome):
            raise TypeError("outcome 必须是 ToolGovernanceOutcome")
        if self.risk_level is not None and not isinstance(
            self.risk_level, ToolRiskLevel
        ):
            raise TypeError("risk_level 必须是 ToolRiskLevel 或 None")
        if not isinstance(self.risk_facts, tuple) or any(
            not isinstance(fact, ToolRiskFact) for fact in self.risk_facts
        ):
            raise TypeError("risk_facts 只能包含 ToolRiskFact")
        if self.safe_error_code is not None and (
            not isinstance(self.safe_error_code, str) or not self.safe_error_code.strip()
        ):
            raise ValueError("safe_error_code 必须是固定非空字符串")


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    """immutable per-Tool policy record。

    只表达：canonical tool_name、explicit allowed_agent_ids、static risk facts、
    approval threshold。不加入 role / capability / tenant / path allowlist /
    network policy / sandbox / approval token / dynamic config。
    """

    tool_name: str
    allowed_agent_ids: frozenset[str]
    risk_facts: tuple[ToolRiskFact, ...] = ()
    approval_required_threshold: ToolRiskLevel = ToolRiskLevel.HIGH

    def __post_init__(self) -> None:
        if not isinstance(self.tool_name, str) or _SAFE_TOOL_NAME.fullmatch(
            self.tool_name
        ) is None:
            raise ToolGovernanceError(
                ToolGovernanceErrorCode.INVALID,
                "Tool policy 必须引用合法 Tool name",
            )
        if not isinstance(self.allowed_agent_ids, frozenset) or not self.allowed_agent_ids:
            raise ToolGovernanceError(
                ToolGovernanceErrorCode.INVALID,
                "Tool policy 的 allowed_agent_ids 必须是非空集合",
            )
        if any(
            not isinstance(agent_id, str) or not agent_id.strip()
            for agent_id in self.allowed_agent_ids
        ):
            raise ToolGovernanceError(
                ToolGovernanceErrorCode.INVALID,
                "allowed agent id 必须是合法标识",
            )
        if not isinstance(self.risk_facts, tuple) or any(
            not isinstance(fact, ToolRiskFact) for fact in self.risk_facts
        ):
            raise ToolGovernanceError(
                ToolGovernanceErrorCode.INVALID,
                "risk facts 必须合法",
            )
        if not isinstance(self.approval_required_threshold, ToolRiskLevel):
            raise ToolGovernanceError(
                ToolGovernanceErrorCode.INVALID,
                "approval threshold 必须合法",
            )


class ToolPolicyCatalog:
    """静态 Tool governance policy 事实源（APPLICATION_SCOPE / process-local）。

    生命周期：``construct -> register -> validate -> freeze -> runtime read-only``。
    冻结后不可变；不支持 hot reload / remote policy / runtime mutation。
    """

    __slots__ = ("_tool_registry", "_agent_registry", "_by_name", "_ordered", "_frozen")

    def __init__(
        self,
        *,
        tool_registry: ToolRegistry,
        agent_registry: AgentRegistry,
    ) -> None:
        if not isinstance(tool_registry, ToolRegistry):
            raise ToolGovernanceError(
                ToolGovernanceErrorCode.INVALID,
                "ToolPolicyCatalog 需要 ToolRegistry",
            )
        if not isinstance(agent_registry, AgentRegistry):
            raise ToolGovernanceError(
                ToolGovernanceErrorCode.INVALID,
                "ToolPolicyCatalog 需要 AgentRegistry",
            )
        self._tool_registry = tool_registry
        self._agent_registry = agent_registry
        self._by_name: dict[str, ToolPolicy] = {}
        self._ordered: list[ToolPolicy] = []
        self._frozen = False

    @property
    def frozen(self) -> bool:
        return self._frozen

    def register(self, policy: ToolPolicy) -> None:
        if self._frozen:
            raise ToolGovernanceError(
                ToolGovernanceErrorCode.FROZEN,
                "ToolPolicyCatalog 已冻结，不允许注册",
            )
        if not isinstance(policy, ToolPolicy):
            raise ToolGovernanceError(
                ToolGovernanceErrorCode.INVALID,
                "必须注册 ToolPolicy",
            )
        if policy.tool_name in self._by_name:
            raise ToolGovernanceError(
                ToolGovernanceErrorCode.DUPLICATE,
                "Tool 名称不允许重复 policy",
            )
        self._by_name[policy.tool_name] = policy
        self._ordered.append(policy)

    def freeze(self) -> None:
        """幂等冻结；冻结前完成 coverage/reference 校验，不得部分 freeze。"""
        if self._frozen:
            return
        self._validate()
        self._frozen = True
        self._by_name = MappingProxyType(self._by_name)
        self._ordered = tuple(self._ordered)

    def _validate(self) -> None:
        # 每个 frozen 生产 Tool 恰好一条 policy。
        for registration in self._tool_registry.registrations():
            if registration.descriptor.name not in self._by_name:
                raise ToolGovernanceError(
                    ToolGovernanceErrorCode.INVALID,
                    "注册 Tool 缺少 governance policy",
                )
        for policy in self._ordered:
            if not self._tool_registry.contains(policy.tool_name):
                raise ToolGovernanceError(
                    ToolGovernanceErrorCode.INVALID,
                    "Tool policy 引用未注册 Tool",
                )
            for agent_id in policy.allowed_agent_ids:
                try:
                    self._agent_registry.resolve(agent_id)
                except AgentRegistryError:
                    raise ToolGovernanceError(
                        ToolGovernanceErrorCode.INVALID,
                        "Tool policy 引用了未知或未启用 Agent",
                    ) from None

    def _require_frozen(self) -> None:
        if not self._frozen:
            raise ToolGovernanceError(
                ToolGovernanceErrorCode.NOT_FROZEN,
                "ToolPolicyCatalog 尚未冻结，禁止读取",
            )

    def find(self, tool_name: str) -> ToolPolicy | None:
        """可选查找：未知返回 None，由 Service 决定 fail-closed 行为。"""
        self._require_frozen()
        return self._by_name.get(tool_name)

    def policies(self) -> tuple[ToolPolicy, ...]:
        self._require_frozen()
        return self._ordered

    def contains(self, tool_name: str) -> bool:
        self._require_frozen()
        return tool_name in self._by_name


_RISK_RANK = {
    ToolRiskLevel.LOW: 1,
    ToolRiskLevel.MEDIUM: 2,
    ToolRiskLevel.HIGH: 3,
}

# Architecture Decision 冻结的 exact full-combination allowlist（P1-01 修复）。
# key = (frozenset(static_risk_facts), side_effect_kind, idempotency)。
# 只有下列 5 个完整组合被批准；其它任何组合一律 TOOL_RISK_UNCLASSIFIED
# fail closed。不实现通用 risk algebra（不取 max / 不按 static 或 dynamic
# baseline 推断）。static facts 与 dynamic execution facts 分别“已知”不代表
# 完整组合已被冻结。
_FULL_RISK_COMBINATIONS: dict[
    tuple[frozenset[ToolRiskFact], ToolSideEffectKind, OperationIdempotency],
    ToolRiskLevel,
] = {
    (
        frozenset({ToolRiskFact.ARBITRARY_LOCAL_FILESYSTEM_READ}),
        ToolSideEffectKind.NONE,
        OperationIdempotency.READ_ONLY,
    ): ToolRiskLevel.MEDIUM,
    (
        frozenset({ToolRiskFact.SYSTEM_INFORMATION_READ}),
        ToolSideEffectKind.NONE,
        OperationIdempotency.READ_ONLY,
    ): ToolRiskLevel.LOW,
    (
        frozenset(),
        ToolSideEffectKind.NONE,
        OperationIdempotency.READ_ONLY,
    ): ToolRiskLevel.LOW,
    (
        frozenset(),
        ToolSideEffectKind.LOCAL_STATE_MUTATION,
        OperationIdempotency.IDEMPOTENT_WITH_KEY,
    ): ToolRiskLevel.MEDIUM,
    (
        frozenset(),
        ToolSideEffectKind.LOCAL_STATE_MUTATION,
        OperationIdempotency.NON_IDEMPOTENT,
    ): ToolRiskLevel.HIGH,
}

# 用户可见固定 safe denial；不包含 raw principal / args / path / policy allowlist。
_USER_VISIBLE_DENIAL = {
    "TOOL_PERMISSION_DENIED": "Tool 调用未执行：当前 Agent 无权限（TOOL_PERMISSION_DENIED）",
    "TOOL_APPROVAL_REQUIRED": "Tool 调用未执行：需要审批，但当前版本不支持审批授予（TOOL_APPROVAL_REQUIRED）",
    "TOOL_GOVERNANCE_UNKNOWN_PRINCIPAL": "Tool 调用未执行：当前执行主体无法识别（TOOL_GOVERNANCE_UNKNOWN_PRINCIPAL）",
    "TOOL_GOVERNANCE_POLICY_MISSING": "Tool 调用未执行：缺少工具治理策略（TOOL_GOVERNANCE_POLICY_MISSING）",
    "TOOL_RISK_UNCLASSIFIED": "Tool 调用未执行：无法确定该调用的风险等级（TOOL_RISK_UNCLASSIFIED）",
}


def governance_denial_message(error_code: str) -> str:
    """把 runtime denial code 映射为固定中文 safe message；未知 code 即编程错误。"""
    return _USER_VISIBLE_DENIAL[error_code]


def _decision(
    outcome: ToolGovernanceOutcome,
    *,
    risk_level: ToolRiskLevel | None = None,
    risk_facts: tuple[ToolRiskFact, ...] = (),
    safe_error_code: ToolGovernanceErrorCode | None = None,
) -> ToolGovernanceDecision:
    return ToolGovernanceDecision(
        outcome=outcome,
        risk_level=risk_level,
        risk_facts=risk_facts,
        safe_error_code=(
            safe_error_code.value if safe_error_code is not None else None
        ),
    )


class ToolGovernanceService:
    """唯一 invocation-time Authority；只解释 Catalog 与 ToolExecutionSpec。

    不维护 Tool implementation / adapter map / description map（那不是第二个
    ToolRegistry）；不把 Agent capability 解释成 authorization。
    """

    __slots__ = ("_catalog", "_agent_registry")

    def __init__(
        self,
        catalog: ToolPolicyCatalog,
        agent_registry: AgentRegistry,
    ) -> None:
        if not isinstance(catalog, ToolPolicyCatalog) or not catalog.frozen:
            raise ToolGovernanceError(
                ToolGovernanceErrorCode.INVALID,
                "ToolGovernanceService 需要已冻结 ToolPolicyCatalog",
            )
        if not isinstance(agent_registry, AgentRegistry):
            raise ToolGovernanceError(
                ToolGovernanceErrorCode.INVALID,
                "ToolGovernanceService 需要 AgentRegistry",
            )
        self._catalog = catalog
        self._agent_registry = agent_registry

    def authorize_tool(
        self,
        context: ToolGovernanceContext,
        registration: ToolRegistration,
    ) -> ToolGovernanceDecision:
        """静态 Permission 阶段：只回答 ALLOW / DENY。

        不调用 build_invocation / spec_for / ToolExecutionService / model。
        """
        if not isinstance(context, ToolGovernanceContext):
            raise TypeError("context 必须是 ToolGovernanceContext")
        if not isinstance(registration, ToolRegistration):
            raise TypeError("registration 必须是 ToolRegistration")
        policy = self._catalog.find(registration.descriptor.name)
        if policy is None:
            return _decision(
                ToolGovernanceOutcome.DENY,
                safe_error_code=ToolGovernanceErrorCode.POLICY_MISSING,
            )
        if not self._principal_known(context.principal_agent_id):
            return _decision(
                ToolGovernanceOutcome.DENY,
                risk_facts=policy.risk_facts,
                safe_error_code=ToolGovernanceErrorCode.UNKNOWN_PRINCIPAL,
            )
        if context.principal_agent_id not in policy.allowed_agent_ids:
            return _decision(
                ToolGovernanceOutcome.DENY,
                risk_facts=policy.risk_facts,
                safe_error_code=ToolGovernanceErrorCode.PERMISSION_DENIED,
            )
        return _decision(
            ToolGovernanceOutcome.ALLOW,
            risk_facts=policy.risk_facts,
        )

    def evaluate_invocation(
        self,
        context: ToolGovernanceContext,
        registration: ToolRegistration,
        invocation: ToolInvocation,
        execution_spec: ToolExecutionSpec,
    ) -> ToolGovernanceDecision:
        """invocation Risk/Approval 阶段：按 exact full-combination allowlist
        计算唯一 effective risk 与 approval。

        只有 Architecture Decision 冻结的完整组合
        ``(frozenset(static_risk_facts), side_effect_kind, idempotency)``
        会被分类；任何其它组合 -> ``TOOL_RISK_UNCLASSIFIED`` fail closed
        （不取 max、不按 baseline 推断）。
        """
        if not isinstance(context, ToolGovernanceContext):
            raise TypeError("context 必须是 ToolGovernanceContext")
        if not isinstance(registration, ToolRegistration):
            raise TypeError("registration 必须是 ToolRegistration")
        if not isinstance(invocation, ToolInvocation):
            raise TypeError("invocation 必须是 ToolInvocation")
        if not isinstance(execution_spec, ToolExecutionSpec):
            raise TypeError("execution_spec 必须是 ToolExecutionSpec")
        policy = self._catalog.find(registration.descriptor.name)
        if policy is None:
            return _decision(
                ToolGovernanceOutcome.DENY,
                safe_error_code=ToolGovernanceErrorCode.POLICY_MISSING,
            )
        full_key = (
            frozenset(policy.risk_facts),
            execution_spec.side_effect_kind,
            execution_spec.idempotency,
        )
        effective = _FULL_RISK_COMBINATIONS.get(full_key)
        if effective is None:
            return _decision(
                ToolGovernanceOutcome.DENY,
                risk_facts=policy.risk_facts,
                safe_error_code=ToolGovernanceErrorCode.RISK_UNCLASSIFIED,
            )
        if _RISK_RANK[effective] >= _RISK_RANK[policy.approval_required_threshold]:
            return _decision(
                ToolGovernanceOutcome.APPROVAL_REQUIRED,
                risk_level=effective,
                risk_facts=policy.risk_facts,
                safe_error_code=ToolGovernanceErrorCode.APPROVAL_REQUIRED,
            )
        return _decision(
            ToolGovernanceOutcome.ALLOW,
            risk_level=effective,
            risk_facts=policy.risk_facts,
        )

    def _principal_known(self, agent_id: str) -> bool:
        try:
            self._agent_registry.resolve(agent_id)
            return True
        except AgentRegistryError:
            return False


def register_default_tool_policies(catalog: ToolPolicyCatalog) -> None:
    """注册四条生产 Tool policy（5×4 explicit ALLOW，无 implicit default allow）。

    risk facts / approval rule 严格按 Architecture Decision §31 / §33 / §43 冻结。
    """
    policies = (
        (
            "list_files",
            (ToolRiskFact.ARBITRARY_LOCAL_FILESYSTEM_READ,),
        ),
        (
            "analyze_excel",
            (ToolRiskFact.ARBITRARY_LOCAL_FILESYSTEM_READ,),
        ),
        (
            "get_system_status",
            (ToolRiskFact.SYSTEM_INFORMATION_READ,),
        ),
        (
            "complex_workflow_simulator",
            (),
        ),
    )
    for tool_name, risk_facts in policies:
        catalog.register(
            ToolPolicy(
                tool_name=tool_name,
                allowed_agent_ids=PRODUCTION_AGENT_IDS,
                risk_facts=risk_facts,
                approval_required_threshold=ToolRiskLevel.HIGH,
            )
        )


__all__ = [
    "PRODUCTION_AGENT_IDS",
    "ToolGovernanceContext",
    "ToolGovernanceDecision",
    "ToolGovernanceError",
    "ToolGovernanceErrorCode",
    "ToolGovernanceOutcome",
    "ToolGovernanceService",
    "ToolPolicy",
    "ToolPolicyCatalog",
    "ToolRiskFact",
    "ToolRiskLevel",
    "governance_denial_message",
    "register_default_tool_policies",
]
