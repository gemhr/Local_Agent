"""启动时校验业务注册并编译到唯一只读 Runtime AgentRegistry。"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field, replace
import hashlib
import json
from types import MappingProxyType
from typing import Iterable, Mapping

from core.agent_platform.contracts import (
    AgentDefinition,
    AgentRegistration,
    BusinessToolDefinition,
    ExecutionBinding,
    WorkflowDefinition,
)
from core.runtime.agent_registry import (
    AgentRegistry,
    AgentRegistryError,
    AgentRegistryErrorCode,
    CompiledAgentRegistration,
    ResultContentType,
)
from core.runtime.planning import OutputPolicy


class AgentRegistrationCompileError(ValueError):
    """启动注册输入无效；消息不包含业务正文。"""


@dataclass(frozen=True, slots=True)
class AgentRegistrationBundle:
    """Composition Root 的单批注册输入与可信 Provider/权限清单。"""

    registrations: tuple[AgentRegistration, ...]
    workflows: tuple[WorkflowDefinition, ...] = ()
    tools: tuple[BusinessToolDefinition, ...] = ()
    model_profile_ids: frozenset[str] = frozenset({"default"})
    retrieval_profile_ids: frozenset[str] = frozenset()
    memory_profile_ids: frozenset[str] = frozenset()
    tool_grants: Mapping[str, frozenset[str]] = dataclass_field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("registrations", "workflows", "tools"):
            value = getattr(self, name)
            expected = {"registrations": AgentRegistration, "workflows": WorkflowDefinition,
                        "tools": BusinessToolDefinition}[name]
            if not isinstance(value, tuple) or any(not isinstance(item, expected) for item in value):
                raise TypeError(f"{name} 必须是对应类型的 tuple")
        frozen_grants: dict[str, frozenset[str]] = {}
        for name, ids in self.tool_grants.items():
            if not isinstance(ids, frozenset):
                raise TypeError("tool_grants 值必须是 frozenset")
            frozen_grants[name] = ids
        object.__setattr__(self, "tool_grants", MappingProxyType(frozen_grants))


@dataclass(frozen=True, slots=True)
class CompiledAgentCatalog:
    """非可解析的编译产物集合；唯一可解析 catalog 是 agent_registry。"""

    agent_registry: AgentRegistry
    tool_grants: Mapping[str, frozenset[str]]
    workflows: Mapping[str, WorkflowDefinition]
    tools: Mapping[str, BusinessToolDefinition]


def compile_agent_catalog(
    bundle: AgentRegistrationBundle,
    *,
    builtin_registrations: Iterable[CompiledAgentRegistration] = (),
    actual_model_profile_ids: frozenset[str] = frozenset({"default"}),
    actual_tool_names: frozenset[str] | None = None,
    platform_tool_permission_seed: Mapping[str, frozenset[str]] | None = None,
    actual_tool_registrations: Mapping[str, object] | None = None,
    tool_permission_limits: Mapping[str, frozenset[str]] | None = None,
) -> CompiledAgentCatalog:
    """一次性验证并编译注册；所有未知符号/权限声明均 fail closed。"""
    if not isinstance(bundle, AgentRegistrationBundle):
        raise TypeError("bundle 必须是 AgentRegistrationBundle")
    raw_builtin = tuple(builtin_registrations)

    workflows: dict[str, WorkflowDefinition] = {}
    for workflow in bundle.workflows:
        if workflow.workflow_id in workflows:
            raise AgentRegistrationCompileError("workflow_id 重复")
        if workflow.synthesis_required and not any(item.synthesis_only for item in raw_builtin):
            raise AgentRegistrationCompileError("Workflow synthesis Agent 不可用")
        workflows[workflow.workflow_id] = workflow
    tools: dict[str, BusinessToolDefinition] = {}
    for tool in bundle.tools:
        if tool.name in tools:
            raise AgentRegistrationCompileError("Tool name 重复")
        tools[tool.name] = tool

    registrations_by_name = dict(actual_tool_registrations or {})
    if actual_tool_names is None and tools:
        from core.agent_platform.business_tool_adapter import compile_business_tool_registration
        registrations_by_name.update({
            name: compile_business_tool_registration(definition)
            for name, definition in tools.items()
        })

    # BusinessToolDefinition grants 与 operator-provided grants 是两项独立授权，
    # 最终权限取交集，Agent allowed_tools 再做第三次显式收窄。
    grants: dict[str, frozenset[str]] = {}
    # Grant 只表达授权，工具是否存在必须由真实注册项证明。
    known_tool_names = set(actual_tool_names if actual_tool_names is not None else tools)
    if not set(tools).issubset(known_tool_names):
        raise AgentRegistrationCompileError("Business Tool 未进入实际 Tool registrations")
    platform_seed = dict(platform_tool_permission_seed or {})
    if actual_tool_names is not None and set(registrations_by_name) != set(actual_tool_names):
        raise AgentRegistrationCompileError("实际 Tool registrations inventory 不一致")
    ghost_grants = (set(bundle.tool_grants) | set(platform_seed)) - known_tool_names
    if ghost_grants:
        raise AgentRegistrationCompileError("平台 grant 引用了未知 Tool")
    for name in known_tool_names:
        definition_grant = tools[name].granted_agent_ids if name in tools else None
        platform_grant = frozenset(platform_seed.get(name, frozenset())) | frozenset(bundle.tool_grants.get(name, frozenset()))
        if name not in tools:
            effective = platform_grant
        elif definition_grant is None or platform_grant is None:
            effective = frozenset()
        else:
            effective = definition_grant & platform_grant
        if name in (tool_permission_limits or {}):
            effective &= tool_permission_limits[name]
        grants[name] = frozenset(effective)

    by_id: dict[str, AgentDefinition] = {}
    for registration in bundle.registrations:
        definition = registration.definition
        if definition.agent_id in by_id:
            raise AgentRegistrationCompileError("agent_id 重复")
        by_id[definition.agent_id] = definition

    # Builtin compiled records participate in the same ID uniqueness validation.
    builtin: tuple[CompiledAgentRegistration, ...] = tuple(
        _compile_builtin_seed(registration, grants, registrations_by_name)
        for registration in raw_builtin
    )
    builtin_ids = {registration.agent_id for registration in builtin}
    if builtin_ids & by_id.keys():
        raise AgentRegistrationCompileError("业务 Agent 与 builtin agent_id 重复")
    known_agent_ids = builtin_ids | by_id.keys()

    for definition in by_id.values():
        if definition.model_profile_id not in actual_model_profile_ids:
            raise AgentRegistrationCompileError("Agent 引用了未知 model profile")
        if definition.retrieval_profile_id is not None:
            raise AgentRegistrationCompileError("当前未支持 Agent retrieval profile binding")
        if definition.memory_profile_id is not None:
            raise AgentRegistrationCompileError("当前未支持 Agent memory profile binding")
        if definition.execution_binding.kind == "workflow":
            workflow = workflows.get(definition.execution_binding.reference or "")
            if workflow is None:
                raise AgentRegistrationCompileError("Agent 引用了未知 Workflow")
            for task in workflow.tasks:
                if task.agent_id not in known_agent_ids:
                    raise AgentRegistrationCompileError("Workflow 引用了未知 Agent")
                task_definition = by_id.get(task.agent_id)
                task_registration = next((item for item in builtin if item.agent_id == task.agent_id), None)
                if task_definition is not None:
                    if not task_definition.delegation_allowed:
                        raise AgentRegistrationCompileError("Workflow task Agent 未声明 delegation 能力")
                    accepted_input_types = task_definition.accepted_input_types
                    capabilities = task_definition.capabilities
                elif task_registration is not None:
                    if not task_registration.delegation_allowed:
                        raise AgentRegistrationCompileError("Workflow task Agent 未声明 delegation 能力")
                    accepted_input_types = task_registration.accepted_input_types
                    capabilities = task_registration.capabilities
                else:
                    raise AgentRegistrationCompileError("Workflow task Agent binding 不可用")
                if task.input_type not in accepted_input_types:
                    raise AgentRegistrationCompileError("Workflow task input_type 不受支持")
                if not task.capabilities.issubset(capabilities):
                    raise AgentRegistrationCompileError("Workflow task capability 不受支持")
                if len(workflow.tasks) > 1 and task_registration is not None and not task_registration.supports_parallel:
                    raise AgentRegistrationCompileError("Workflow task Agent 不支持 parallel")
        for tool_name in definition.allowed_tools:
            if tool_name not in known_tool_names:
                raise AgentRegistrationCompileError("Agent 引用了未知 Tool")
            if definition.agent_id not in grants.get(tool_name, frozenset()):
                raise AgentRegistrationCompileError("Agent Tool 权限未获平台注册授权")

    # 最终 authorization set 是显式 grant 与每个 Agent allowed_tools 的交集。
    grants = {
        name: frozenset(
            agent_id for agent_id in granted_agents
            if agent_id in builtin_ids
            or (agent_id in by_id and name in by_id[agent_id].allowed_tools)
        )
        for name, granted_agents in grants.items()
    }

    compiled: list[CompiledAgentRegistration] = list(builtin)
    for definition in by_id.values():
        bound_workflow = (
            workflows.get(definition.execution_binding.reference or "")
            if definition.execution_binding.kind == "workflow"
            else None
        )
        allowed_tools = frozenset(definition.allowed_tools)
        if actual_tool_names is not None and not allowed_tools.issubset(registrations_by_name):
            raise AgentRegistrationCompileError("Agent Tool binding 缺少实际 Tool registration identity")
        tool_identity_payload = {
            "agent_id": definition.agent_id,
            "tools": sorted(allowed_tools),
            "tool_bindings": [_tool_registration_identity(registrations_by_name[name]) for name in sorted(allowed_tools)],
            "model_profile": definition.model_profile_id,
            "retrieval_profile": definition.retrieval_profile_id,
            "memory_profile": definition.memory_profile_id,
        }
        toolset_identity = hashlib.sha256(
            json.dumps(tool_identity_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        # Runtime output/execution policy 由平台按当前模型 Agent 形状生成。
        result_types = frozenset(
            ResultContentType.STRUCTURED if kind == "structured" else ResultContentType.TEXT
            for kind in definition.produced_result_types
            if kind in {"text", "structured"}
        )
        if len(result_types) != len(definition.produced_result_types):
            raise AgentRegistrationCompileError("Agent output type 当前不支持")
        compiled.append(CompiledAgentRegistration(
            agent_id=definition.agent_id,
            execution_adapter_id="agent_router_adapter",
            display_name=definition.display_name,
            role=definition.role,
            avatar="avatar_router.png",
            enabled=True,
            entry_allowed=definition.entry_allowed,
            entry_output_policy=(OutputPolicy.FINAL_PASSTHROUGH if definition.entry_allowed else OutputPolicy.INTERNAL),
            model_direct_allowed=definition.execution_binding.kind == "dynamic",
            delegation_allowed=definition.delegation_allowed,
            delegated_output_policy=OutputPolicy.INTERNAL,
            allows_single_delegated_passthrough=False,
            synthesis_only=False,
            supports_parallel=definition.delegation_allowed,
            accepted_input_types=definition.accepted_input_types,
            produced_result_types=result_types,
            capabilities=definition.capabilities,
            deterministic_aliases=(definition.agent_id,),
            definition=definition,
            workflow=bound_workflow,
            actual_allowed_tools=allowed_tools,
            toolset_identity=toolset_identity,
        ))

    try:
        registry = AgentRegistry(compiled)
    except AgentRegistryError as exc:
        raise AgentRegistrationCompileError(exc.safe_message) from None
    return CompiledAgentCatalog(
        agent_registry=registry,
        tool_grants=MappingProxyType(grants),
        workflows=MappingProxyType(workflows),
        tools=MappingProxyType(tools),
    )


def compile_agent_registry(
    bundle: AgentRegistrationBundle,
    *,
    builtin_registrations: Iterable[CompiledAgentRegistration] = (),
) -> AgentRegistry:
    """返回唯一可解析 catalog，便于 Runtime consumers 继续复用其既有 Contract。"""
    return compile_agent_catalog(bundle, builtin_registrations=builtin_registrations).agent_registry


def _compile_builtin_seed(
    registration: CompiledAgentRegistration,
    grants: Mapping[str, frozenset[str]],
    actual_tool_registrations: Mapping[str, object],
) -> CompiledAgentRegistration:
    """给内置目录补齐明确的 builtin identity，再纳入统一 AgentRegistry。"""
    if not isinstance(registration, CompiledAgentRegistration):
        raise AgentRegistrationCompileError("builtin seed 必须是 CompiledAgentRegistration")
    if registration.definition is not None:
        definition = registration.definition
        if not isinstance(definition, AgentDefinition):
            raise AgentRegistrationCompileError("builtin seed definition 类型无效")
        return registration
    definition = AgentDefinition(
        agent_id=registration.agent_id,
        agent_version="builtin-1",
        display_name=registration.display_name,
        role=registration.role,
        # Builtin behavior may still have internal prompt specialization. This value
        # preserves the current declared role until that behavior is moved to seed data.
        instructions=registration.role,
        entry_allowed=registration.entry_allowed,
        delegation_allowed=registration.delegation_allowed,
        capabilities=registration.capabilities,
        accepted_input_types=registration.accepted_input_types,
        produced_result_types=frozenset(value.value for value in registration.produced_result_types),
        allowed_tools=frozenset(
            name for name, agent_ids in grants.items() if registration.agent_id in agent_ids
        ),
        model_profile_id="default",
        execution_binding=ExecutionBinding(
            kind="dynamic" if registration.model_direct_allowed else "direct"
        ),
    )
    allowed_tools = definition.allowed_tools
    toolset_identity = hashlib.sha256(json.dumps({
        "agent_id": definition.agent_id,
        "tools": sorted(allowed_tools),
        "tool_bindings": [_tool_registration_identity(actual_tool_registrations[name]) for name in sorted(allowed_tools) if name in actual_tool_registrations],
        "model_profile": definition.model_profile_id,
        "retrieval_profile": definition.retrieval_profile_id,
        "memory_profile": definition.memory_profile_id,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return replace(
        registration,
        execution_adapter_id=(
            registration.execution_adapter_id
            if registration.synthesis_only
            else "agent_router_adapter"
        ),
        definition=definition,
        actual_allowed_tools=allowed_tools,
        toolset_identity=toolset_identity,
    )


def _tool_registration_identity(registration: object) -> dict[str, object]:
    """只序列化稳定 Tool descriptor/schema/spec/provider identity。"""
    descriptor = registration.descriptor
    adapter = registration.adapter
    spec = adapter.spec
    server_id = getattr(adapter, "server_id", None)
    remote_name = getattr(adapter, "remote_name", None)
    return {
        "name": descriptor.name,
        "description": descriptor.description,
        "llm_instructions": descriptor.llm_instructions,
        "input_schema": adapter.llm_input_schema(),
        "provider_kind": "mcp" if isinstance(server_id, str) and isinstance(remote_name, str) else "local",
        "provider_identity": server_id if isinstance(server_id, str) else f"{type(adapter).__module__}.{type(adapter).__qualname__}",
        "remote_tool_id": remote_name if isinstance(remote_name, str) else descriptor.name,
        "handler_binding_id": getattr(adapter, "handler_binding_id", None),
        "execution_spec": {
            "side_effect_kind": spec.side_effect_kind.value,
            "idempotency": spec.idempotency.value,
            "requires_resource_key": spec.requires_resource_key,
            "supports_cooperative_cancellation": spec.supports_cooperative_cancellation,
            "supports_side_effect_checkpoint": spec.supports_side_effect_checkpoint,
            "default_timeout_seconds": spec.default_timeout_seconds,
            "max_output_bytes": spec.max_output_bytes,
            "max_concurrency": spec.max_concurrency,
            "supports_idempotency_replay": spec.supports_idempotency_replay,
        },
    }


__all__ = [
    "AgentRegistrationBundle", "AgentRegistrationCompileError", "CompiledAgentCatalog",
    "compile_agent_catalog", "compile_agent_registry",
]
