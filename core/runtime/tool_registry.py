#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Canonical Tool Registry / Descriptor / Registration（INTERNAL_RC，非 PUBLIC_STABLE）。

进程级 Tool 身份、描述、枚举与执行绑定的唯一事实源。Scope 为
APPLICATION_SCOPE / process-local：startup 期间 ``construct -> register ->
freeze``，运行期只读。执行语义仍由既有 ``ToolExecutionSpec`` 与
``ToolExecutionService`` 拥有；本模块不复制任何 execution contract 字段。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from types import MappingProxyType
import unicodedata

from core.runtime.tool_adapters import ToolAdapter
from core.runtime.tool_contract import ToolExecutionSpec

_SAFE_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class ToolRegistryErrorCode(str, Enum):
    """Registry / startup / internal 错误码；不是 Runtime invocation error。"""

    INVALID = "TOOL_REGISTRY_INVALID"
    DUPLICATE = "TOOL_REGISTRY_DUPLICATE"
    FROZEN = "TOOL_REGISTRY_FROZEN"
    NOT_FROZEN = "TOOL_REGISTRY_NOT_FROZEN"
    NOT_REGISTERED = "TOOL_NOT_REGISTERED"


class ToolRegistryError(RuntimeError):
    """只包含稳定代码和安全说明的 Registry 错误；不携带 Tool 参数/输出/路径。"""

    def __init__(self, error_code: ToolRegistryErrorCode, safe_message: str) -> None:
        self.error_code = error_code
        self.safe_message = safe_message
        super().__init__(f"{safe_message} (error_code={error_code.value})")


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    """不可变 Tool 静态描述；只表达 identity + description，不承载执行状态。"""

    name: str
    description: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _SAFE_TOOL_NAME.fullmatch(self.name) is None:
            raise ToolRegistryError(
                ToolRegistryErrorCode.INVALID,
                "Tool name 必须匹配安全标识符",
            )
        if not isinstance(self.description, str):
            raise ToolRegistryError(
                ToolRegistryErrorCode.INVALID,
                "Tool description 必须是字符串",
            )
        stripped = self.description.strip()
        if not stripped:
            raise ToolRegistryError(
                ToolRegistryErrorCode.INVALID,
                "Tool description 不能为空",
            )
        if any(unicodedata.category(char) == "Cc" for char in stripped):
            raise ToolRegistryError(
                ToolRegistryErrorCode.INVALID,
                "Tool description 不能包含控制字符",
            )
        object.__setattr__(self, "description", stripped)


@dataclass(frozen=True, slots=True)
class ToolRegistration:
    """不可变注册记录：静态 Descriptor + application-scoped ToolAdapter 绑定。

    Descriptor 与 Adapter 的 Tool identity 必须一致（依据
    ``ToolExecutionSpec.tool_name`` 合同，invocation-independent 校验）。
    本记录不复制 ``ToolExecutionSpec`` 任何字段。
    """

    descriptor: ToolDescriptor
    adapter: ToolAdapter

    def __post_init__(self) -> None:
        if not isinstance(self.descriptor, ToolDescriptor):
            raise ToolRegistryError(
                ToolRegistryErrorCode.INVALID,
                "注册项必须携带 ToolDescriptor",
            )
        if not isinstance(self.adapter, ToolAdapter):
            raise ToolRegistryError(
                ToolRegistryErrorCode.INVALID,
                "注册项必须携带 ToolAdapter",
            )
        spec = getattr(self.adapter, "spec", None)
        if not isinstance(spec, ToolExecutionSpec):
            raise ToolRegistryError(
                ToolRegistryErrorCode.INVALID,
                "Adapter 未声明 ToolExecutionSpec，无法验证 Tool identity",
            )
        if spec.tool_name != self.descriptor.name:
            raise ToolRegistryError(
                ToolRegistryErrorCode.INVALID,
                "Descriptor 与 Adapter 的 Tool name 必须一致",
            )


class ToolRegistry:
    """进程级 Tool Registry（APPLICATION_SCOPE）。

    生命周期：``construct -> register -> freeze -> 运行期只读``。freeze 前调用
    read API、freeze 后调用 ``register`` 均 fail closed；注册顺序在冻结后保持
    deterministic。运行期不支持 add / remove / replace / hot reload。
    """

    __slots__ = ("_by_name", "_ordered", "_frozen")

    def __init__(self) -> None:
        self._by_name: dict[str, ToolRegistration] = {}
        self._ordered: list[ToolRegistration] = []
        self._frozen = False

    @property
    def frozen(self) -> bool:
        return self._frozen

    def register(self, registration: ToolRegistration) -> None:
        if self._frozen:
            raise ToolRegistryError(
                ToolRegistryErrorCode.FROZEN,
                "ToolRegistry 已冻结，不允许注册",
            )
        if not isinstance(registration, ToolRegistration):
            raise ToolRegistryError(
                ToolRegistryErrorCode.INVALID,
                "注册项必须是 ToolRegistration",
            )
        name = registration.descriptor.name
        if name in self._by_name:
            # 保留 original registration；不允许 last-write-wins。
            raise ToolRegistryError(
                ToolRegistryErrorCode.DUPLICATE,
                "Tool 名称不允许重复注册",
            )
        self._by_name[name] = registration
        self._ordered.append(registration)

    def freeze(self) -> None:
        """幂等冻结；冻结后所有 read API 可用，mutation 一律 fail closed。"""
        if self._frozen:
            return
        self._frozen = True
        self._by_name = MappingProxyType(self._by_name)
        self._ordered = tuple(self._ordered)

    def _require_frozen(self) -> None:
        if not self._frozen:
            raise ToolRegistryError(
                ToolRegistryErrorCode.NOT_FROZEN,
                "ToolRegistry 尚未冻结，禁止读取",
            )

    def resolve(self, name: str) -> ToolRegistration | None:
        """可选查找：未知返回 None，供 untrusted planner 输出使用。"""
        self._require_frozen()
        return self._by_name.get(name)

    def require(self, name: str) -> ToolRegistration:
        """必须存在：未知 fail closed（TOOL_NOT_REGISTERED），供 trusted internal 使用。"""
        self._require_frozen()
        registration = self._by_name.get(name)
        if registration is None:
            raise ToolRegistryError(
                ToolRegistryErrorCode.NOT_REGISTERED,
                "Tool 未注册",
            )
        return registration

    def registrations(self) -> tuple[ToolRegistration, ...]:
        self._require_frozen()
        return self._ordered

    def descriptors(self) -> tuple[ToolDescriptor, ...]:
        self._require_frozen()
        return tuple(item.descriptor for item in self._ordered)

    def contains(self, name: str) -> bool:
        self._require_frozen()
        return name in self._by_name


__all__ = [
    "ToolDescriptor",
    "ToolRegistration",
    "ToolRegistry",
    "ToolRegistryError",
    "ToolRegistryErrorCode",
]
