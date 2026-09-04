#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tool 文件系统资源授权（INTERNAL_RC）。

本模块只解释应用启动时冻结的只读根策略。它不替代 Windows ACL、Tool
Permission 或完整 Sandbox，也不执行文件系统业务操作。
"""

from __future__ import annotations

import ntpath
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from core.runtime.tool_contract import ToolInvocation, thaw_json
from core.runtime.tool_registry import ToolRegistry


RESOURCE_DENIAL_MESSAGE = (
    "Tool 调用未执行：请求的资源不在允许访问范围内（TOOL_RESOURCE_DENIED）"
)
_SAFE_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_ARGUMENT_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class ResourceKind(str, Enum):
    DIRECTORY = "DIRECTORY"
    FILE = "FILE"


class ResourceOperation(str, Enum):
    READ = "READ"


@dataclass(frozen=True, slots=True)
class ResourceAccessRequest:
    tool_name: str
    resource: str
    resource_kind: ResourceKind
    operation: ResourceOperation


class ResourceAuthorizationOutcome(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"


@dataclass(frozen=True, slots=True)
class ResourceAuthorizationDecision:
    outcome: ResourceAuthorizationOutcome
    safe_error_code: str | None = None


class ResourceAuthorizationErrorCode(str, Enum):
    TOOL_RESOURCE_DENIED = "TOOL_RESOURCE_DENIED"


class ResourceAuthorizationError(RuntimeError):
    """仅携带固定错误码和固定安全文本的资源拒绝。"""

    def __init__(self) -> None:
        self.error_code = ResourceAuthorizationErrorCode.TOOL_RESOURCE_DENIED
        self.safe_error_code = self.error_code.value
        self.safe_message = RESOURCE_DENIAL_MESSAGE
        super().__init__(self.safe_message)


@dataclass(frozen=True, slots=True)
class FilesystemResourcePolicy:
    """应用级多个 canonical Windows READ roots。"""

    allowed_read_roots: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.allowed_read_roots, tuple) or any(
            not isinstance(root, str) or not root for root in self.allowed_read_roots
        ):
            raise TypeError("allowed_read_roots 必须是非空字符串组成的 tuple")


@dataclass(frozen=True, slots=True)
class ToolResourceExtractorDescriptor:
    tool_name: str
    argument_key: str
    resource_kind: ResourceKind
    operation: ResourceOperation

    def __post_init__(self) -> None:
        if _SAFE_TOOL_NAME.fullmatch(self.tool_name) is None:
            raise ValueError("resource extractor tool_name 非法")
        if _SAFE_ARGUMENT_KEY.fullmatch(self.argument_key) is None:
            raise ValueError("resource extractor argument_key 非法")
        if not isinstance(self.resource_kind, ResourceKind):
            raise TypeError("resource_kind 必须是 ResourceKind")
        if not isinstance(self.operation, ResourceOperation):
            raise TypeError("operation 必须是 ResourceOperation")


class ToolResourceExtractorCatalog:
    """Tool-specific resource extractor 的启动期 builder / 运行期只读目录。"""

    __slots__ = ("_descriptors", "_frozen")

    def __init__(self) -> None:
        self._descriptors: Mapping[str, ToolResourceExtractorDescriptor] | dict[
            str, ToolResourceExtractorDescriptor
        ] = {}
        self._frozen = False

    @property
    def frozen(self) -> bool:
        return self._frozen

    def register(self, descriptor: ToolResourceExtractorDescriptor) -> None:
        if self._frozen:
            raise RuntimeError("ToolResourceExtractorCatalog 已冻结")
        if not isinstance(descriptor, ToolResourceExtractorDescriptor):
            raise TypeError("descriptor 类型非法")
        if descriptor.tool_name in self._descriptors:
            raise ValueError("resource extractor 不允许重复注册")
        self._descriptors[descriptor.tool_name] = descriptor

    def validate(self, tool_registry: ToolRegistry) -> None:
        if self._frozen:
            raise RuntimeError("已冻结 catalog 不允许重新校验")
        if not isinstance(tool_registry, ToolRegistry) or not tool_registry.frozen:
            raise RuntimeError("ToolRegistry 必须先冻结")
        # 只覆盖 legacy 任意路径 READ 型文件工具；workspace 工具接受相对
        # path 且由 adapter 内置 resolve containment 强制 Demo Root 边界，
        # 不进入本 application-wide read-roots 授权面。
        required = {"list_files", "analyze_excel"}
        if set(self._descriptors) != required:
            raise RuntimeError("File Tool resource extractor coverage 不完整")
        for descriptor in self._descriptors.values():
            tool_registry.require(descriptor.tool_name)
            if descriptor.argument_key != "argument_text":
                raise RuntimeError("File Tool resource argument key 非法")

    def freeze(self) -> None:
        if not self._frozen:
            self._descriptors = MappingProxyType(dict(self._descriptors))
            self._frozen = True

    def extract(self, invocation: ToolInvocation) -> ResourceAccessRequest | None:
        if not self._frozen:
            raise RuntimeError("ToolResourceExtractorCatalog 尚未冻结")
        descriptor = self._descriptors.get(invocation.tool_name)
        if descriptor is None:
            return None
        arguments = thaw_json(invocation.arguments)
        resource = arguments.get(descriptor.argument_key)
        if not isinstance(resource, str):
            resource = ""
        return ResourceAccessRequest(
            tool_name=descriptor.tool_name,
            resource=resource,
            resource_kind=descriptor.resource_kind,
            operation=descriptor.operation,
        )

    def matches(self, request: ResourceAccessRequest) -> bool:
        """确认 request 的 Tool/kind/operation 仍与 frozen descriptor 一致。"""
        if not self._frozen or not isinstance(request, ResourceAccessRequest):
            return False
        descriptor = self._descriptors.get(request.tool_name)
        return bool(
            descriptor is not None
            and descriptor.resource_kind is request.resource_kind
            and descriptor.operation is request.operation
        )


class ResourceAuthorizationService:
    """应用级文件系统资源授权 Authority。"""

    __slots__ = ("_policy", "_extractors")

    def __init__(
        self,
        policy: FilesystemResourcePolicy,
        extractors: ToolResourceExtractorCatalog,
    ) -> None:
        if not isinstance(policy, FilesystemResourcePolicy):
            raise TypeError("policy 必须是 FilesystemResourcePolicy")
        if not isinstance(extractors, ToolResourceExtractorCatalog) or not extractors.frozen:
            raise RuntimeError("resource extractor catalog 必须先冻结")
        self._policy = policy
        self._extractors = extractors

    def extract(self, invocation: ToolInvocation) -> ResourceAccessRequest | None:
        return self._extractors.extract(invocation)

    def authorize(self, request: ResourceAccessRequest) -> ResourceAuthorizationDecision:
        if not isinstance(request, ResourceAccessRequest) or not self._extractors.matches(request):
            return self._deny()
        if request.operation is not ResourceOperation.READ:
            return self._deny()
        candidate_text = request.resource
        if not _is_drive_qualified_local_path(candidate_text):
            return self._deny()
        try:
            candidate = Path(candidate_text).resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            return self._deny()
        try:
            kind_matches = (
                candidate.is_dir()
                if request.resource_kind is ResourceKind.DIRECTORY
                else candidate.is_file()
                if request.resource_kind is ResourceKind.FILE
                else False
            )
        except OSError:
            kind_matches = False
        if not kind_matches:
            return self._deny()
        normalized_candidate = _normalize_windows_path(str(candidate))
        for root in self._policy.allowed_read_roots:
            normalized_root = _normalize_windows_path(root)
            try:
                if ntpath.commonpath((normalized_root, normalized_candidate)) == normalized_root:
                    return ResourceAuthorizationDecision(ResourceAuthorizationOutcome.ALLOW)
            except ValueError:
                continue
        return self._deny()

    def require_authorized(self, request: ResourceAccessRequest) -> None:
        if self.authorize(request).outcome is not ResourceAuthorizationOutcome.ALLOW:
            raise ResourceAuthorizationError()

    @staticmethod
    def _deny() -> ResourceAuthorizationDecision:
        return ResourceAuthorizationDecision(
            ResourceAuthorizationOutcome.DENY,
            ResourceAuthorizationErrorCode.TOOL_RESOURCE_DENIED.value,
        )


def _normalize_windows_path(value: str) -> str:
    return ntpath.normcase(ntpath.normpath(value))


def _is_drive_qualified_local_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    if value != value.strip() or value[0] in "'\"" or value[-1] in "'\"":
        return False
    normalized_separators = value.replace("/", "\\")
    lowered = normalized_separators.lower()
    if lowered.startswith(("\\\\", "\\?\\", "\\.\\")):
        return False
    drive, tail = ntpath.splitdrive(normalized_separators)
    return bool(
        re.fullmatch(r"[A-Za-z]:", drive)
        and tail.startswith("\\")
        and not tail.startswith("\\\\")
    )


__all__ = [
    "FilesystemResourcePolicy",
    "RESOURCE_DENIAL_MESSAGE",
    "ResourceAccessRequest",
    "ResourceAuthorizationDecision",
    "ResourceAuthorizationError",
    "ResourceAuthorizationErrorCode",
    "ResourceAuthorizationOutcome",
    "ResourceAuthorizationService",
    "ResourceKind",
    "ResourceOperation",
    "ToolResourceExtractorCatalog",
    "ToolResourceExtractorDescriptor",
]
