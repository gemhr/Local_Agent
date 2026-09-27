"""业务侧 Agent、Workflow 与 Tool 注册合同。

这些值只描述业务意图和符号绑定，不包含 Runtime 执行对象或状态。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from types import MappingProxyType
from typing import Any, Callable, Mapping

_SAFE_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SAFE_AGENT_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}$")


def _freeze_json(value: Any, *, path: str = "value") -> Any:
    """复制并冻结 JSON 值，拒绝任意 Python 对象。"""
    if value is None or type(value) in (bool, int, float, str):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise ValueError(f"{path} 必须是有限 JSON 数值")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} 的对象键必须是字符串")
            frozen[key] = _freeze_json(item, path=f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, path=f"{path}[]") for item in value)
    raise ValueError(f"{path} 必须只包含 JSON 值")


def _schema(value: Mapping[str, Any] | None, field_name: str) -> Mapping[str, Any] | None:
    if value is None:
        return None
    frozen = _freeze_json(value, path=field_name)
    if not isinstance(frozen, Mapping):
        raise ValueError(f"{field_name} 必须是 JSON object schema")
    _validate_schema_subset(frozen, field_name)
    json.dumps(_thaw_json(frozen), ensure_ascii=False, allow_nan=False)
    return frozen


def _validate_schema_subset(schema: Mapping[str, Any], path: str) -> None:
    """拒绝 matcher 不实现的 JSON Schema 约束，避免声明被静默忽略。"""
    supported = {"type", "enum", "properties", "required", "additionalProperties", "items"}
    unknown = set(schema) - supported
    if unknown:
        raise ValueError(f"{path} 包含不支持的 schema keyword: {sorted(unknown)[0]}")
    if "type" in schema and (
        not isinstance(schema["type"], str)
        or schema["type"] not in {
            "object", "array", "string", "integer", "number", "boolean", "null"
        }
    ):
        raise ValueError(f"{path}.type 不受支持")
    if "enum" in schema and (
        not isinstance(schema["enum"], tuple) or not schema["enum"]
    ):
        raise ValueError(f"{path}.enum 必须是非空数组")
    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, Mapping):
            raise ValueError(f"{path}.properties 必须是对象")
        for name, child in properties.items():
            if not isinstance(child, Mapping):
                raise ValueError(f"{path}.properties.{name} 必须是 schema 对象")
            _validate_schema_subset(child, f"{path}.properties.{name}")
    required = schema.get("required")
    if required is not None and (
        not isinstance(required, tuple)
        or any(not isinstance(name, str) or not name for name in required)
    ):
        raise ValueError(f"{path}.required 必须是字符串数组")
    if "additionalProperties" in schema and type(schema["additionalProperties"]) is not bool:
        raise ValueError(f"{path}.additionalProperties 只支持 bool")
    items = schema.get("items")
    if items is not None:
        if not isinstance(items, Mapping):
            raise ValueError(f"{path}.items 必须是 schema 对象")
        _validate_schema_subset(items, f"{path}.items")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class ExecutionBinding:
    """Agent 的符号执行选择；不接受 resolver/callback。"""

    kind: str = "direct"
    reference: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"direct", "dynamic", "workflow"}:
            raise ValueError("execution binding kind 不受支持")
        if self.kind == "workflow":
            if not isinstance(self.reference, str) or _SAFE_ID.fullmatch(self.reference) is None:
                raise ValueError("workflow binding 必须引用合法 workflow_id")
        elif self.reference is not None:
            raise ValueError("direct/dynamic binding 不接受 reference")


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    agent_id: str
    agent_version: str
    display_name: str
    role: str
    instructions: str = field(repr=False)
    entry_allowed: bool = True
    delegation_allowed: bool = False
    capabilities: frozenset[str] = frozenset()
    accepted_input_types: frozenset[str] = frozenset({"text"})
    produced_result_types: frozenset[str] = frozenset({"text"})
    execution_binding: ExecutionBinding = field(default_factory=ExecutionBinding)
    allowed_tools: frozenset[str] = frozenset()
    model_profile_id: str = "default"
    retrieval_profile_id: str | None = None
    memory_profile_id: str | None = None
    input_schema: Mapping[str, Any] | None = field(default=None, repr=False)
    output_schema: Mapping[str, Any] | None = field(default=None, repr=False)
    business_options: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.agent_id, str) or _SAFE_AGENT_ID.fullmatch(self.agent_id) is None:
            raise ValueError("agent_id 必须是安全标识")
        if not isinstance(self.agent_version, str) or _SAFE_VERSION.fullmatch(self.agent_version) is None:
            raise ValueError("agent_version 必须是安全、非空版本标识")
        for name, value in (("display_name", self.display_name), ("role", self.role), ("instructions", self.instructions)):
            if not isinstance(value, str) or (name != "instructions" and not value.strip()):
                raise ValueError(f"{name} 必须是有效字符串")
        for name, value in (("capabilities", self.capabilities), ("allowed_tools", self.allowed_tools),
                            ("accepted_input_types", self.accepted_input_types),
                            ("produced_result_types", self.produced_result_types)):
            if not isinstance(value, frozenset) or any(not isinstance(item, str) or _SAFE_ID.fullmatch(item) is None for item in value):
                raise ValueError(f"{name} 必须是安全标识的 frozenset")
        if not self.accepted_input_types or not self.produced_result_types:
            raise ValueError("Agent 必须声明输入与输出类型")
        if not isinstance(self.execution_binding, ExecutionBinding):
            raise TypeError("execution_binding 必须是 ExecutionBinding")
        if type(self.entry_allowed) is not bool or type(self.delegation_allowed) is not bool:
            raise TypeError("entry_allowed/delegation_allowed 必须是 bool")
        if not self.entry_allowed and not self.delegation_allowed:
            raise ValueError("Agent 必须声明 entry 或 delegation 意图")
        for name in ("model_profile_id", "retrieval_profile_id", "memory_profile_id"):
            item = getattr(self, name)
            if item is not None and (not isinstance(item, str) or _SAFE_ID.fullmatch(item) is None):
                raise ValueError(f"{name} 必须是安全符号引用")
        object.__setattr__(self, "input_schema", _schema(self.input_schema, "input_schema"))
        object.__setattr__(self, "output_schema", _schema(self.output_schema, "output_schema"))
        frozen_options = _freeze_json(self.business_options, path="business_options")
        if not isinstance(frozen_options, Mapping):
            raise ValueError("business_options 必须是 JSON object")
        object.__setattr__(self, "business_options", frozen_options)


@dataclass(frozen=True, slots=True)
class AgentRegistration:
    """公开的启动注册提交值。"""

    definition: AgentDefinition

    def __post_init__(self) -> None:
        if not isinstance(self.definition, AgentDefinition):
            raise TypeError("definition 必须是 AgentDefinition")


@dataclass(frozen=True, slots=True)
class WorkflowTask:
    task_id: str
    agent_id: str
    instruction: str
    input_type: str = "text"
    capabilities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        for name, value in (("task_id", self.task_id), ("agent_id", self.agent_id), ("input_type", self.input_type)):
            pattern = _SAFE_AGENT_ID if name == "agent_id" else _SAFE_ID
            if not isinstance(value, str) or pattern.fullmatch(value) is None:
                raise ValueError(f"{name} 必须是安全标识")
        if not isinstance(self.instruction, str) or not self.instruction.strip():
            raise ValueError("instruction 不能为空")
        if not isinstance(self.capabilities, frozenset) or any(_SAFE_ID.fullmatch(item) is None for item in self.capabilities):
            raise ValueError("capabilities 必须是安全标识集合")


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    workflow_id: str
    workflow_version: str
    tasks: tuple[WorkflowTask, ...] = ()
    synthesis_required: bool = False

    def __post_init__(self) -> None:
        for name in ("workflow_id", "workflow_version"):
            value = getattr(self, name)
            pattern = _SAFE_VERSION if name == "workflow_version" else _SAFE_ID
            if not isinstance(value, str) or pattern.fullmatch(value) is None:
                raise ValueError(f"{name} 必须是安全标识")
        if not isinstance(self.tasks, tuple) or any(not isinstance(task, WorkflowTask) for task in self.tasks):
            raise TypeError("tasks 必须是 WorkflowTask tuple")
        if type(self.synthesis_required) is not bool:
            raise TypeError("synthesis_required 必须是 bool")
        if len({task.task_id for task in self.tasks}) != len(self.tasks):
            raise ValueError("Workflow task_id 不允许重复")
        if len(self.tasks) > 1 and not self.synthesis_required:
            raise ValueError("多个 delegated tasks 必须声明 synthesis_required")
        if len(self.tasks) == 1 and not self.synthesis_required:
            raise ValueError("单个 delegated task 当前必须声明 synthesis_required")
        if not self.tasks and self.synthesis_required:
            raise ValueError("空 Workflow 不得要求 synthesis")


@dataclass(frozen=True, slots=True)
class BusinessPlanningDecision:
    """Planner 返回的纯任务意图，不含 Runtime Plan/Step。"""

    tasks: tuple[WorkflowTask, ...] = ()
    direct_answer: bool = True
    synthesis_required: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.tasks, tuple) or any(not isinstance(task, WorkflowTask) for task in self.tasks):
            raise TypeError("tasks 必须是 WorkflowTask tuple")
        if type(self.direct_answer) is not bool or type(self.synthesis_required) is not bool:
            raise TypeError("decision flags 必须是 bool")
        if self.direct_answer and self.tasks:
            raise ValueError("direct answer 不能同时包含 delegated tasks")
        if len(self.tasks) > 1 and not self.synthesis_required:
            raise ValueError("多个 delegated tasks 必须要求 synthesis")


@dataclass(frozen=True, slots=True)
class ToolInvocationContext:
    operation_id: str
    remaining_seconds: float
    cancellation_check: Callable[[], None] = field(repr=False, compare=False)

    def raise_if_cancelled(self) -> None:
        self.cancellation_check()


@dataclass(frozen=True, slots=True)
class BusinessToolDefinition:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    handler: Callable[[Mapping[str, Any], ToolInvocationContext], Any] = field(repr=False, compare=False)
    handler_binding_id: str | None = None
    granted_agent_ids: frozenset[str] = frozenset()
    risk_facts: frozenset[str] = frozenset()
    side_effect_kind: str = "UNKNOWN"
    idempotency: str = "NON_IDEMPOTENT"
    resource_selector: Callable[[Mapping[str, Any]], tuple[str, ...]] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _SAFE_ID.fullmatch(self.name) is None:
            raise ValueError("Tool name 必须是安全标识")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("Tool description 不能为空")
        if not callable(self.handler):
            raise TypeError("handler 必须可调用")
        if self.handler_binding_id is not None and (
            not isinstance(self.handler_binding_id, str)
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:+-]{0,255}", self.handler_binding_id) is None
        ):
            raise ValueError("handler_binding_id 必须是非空安全绑定标识")
        if not isinstance(self.granted_agent_ids, frozenset) or any(_SAFE_AGENT_ID.fullmatch(item) is None for item in self.granted_agent_ids):
            raise ValueError("granted_agent_ids 必须是安全 Agent ID 集合")
        if not isinstance(self.risk_facts, frozenset) or any(_SAFE_ID.fullmatch(item.lower()) is None for item in self.risk_facts):
            raise ValueError("risk_facts 必须是安全标识集合")
        if self.side_effect_kind not in {"READ_ONLY", "IDEMPOTENT", "NON_IDEMPOTENT", "UNKNOWN"}:
            raise ValueError("side_effect_kind 不受支持")
        if self.idempotency not in {"IDEMPOTENT", "NON_IDEMPOTENT", "UNKNOWN"}:
            raise ValueError("idempotency 不受支持")
        object.__setattr__(self, "input_schema", _schema(self.input_schema, "input_schema"))


__all__ = [
    "AgentDefinition", "AgentRegistration", "BusinessPlanningDecision", "BusinessToolDefinition",
    "ExecutionBinding", "ToolInvocationContext", "WorkflowDefinition", "WorkflowTask",
]
