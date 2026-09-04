#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""受限 Demo Workspace 文件工具的 Runtime Adapter（WP2）。

``WorkspaceReadToolAdapter`` / ``WorkspaceWriteToolAdapter`` 只把业务输入
（相对路径 + 可选文本内容）映射为不可变 ``ToolInvocation``，执行一次并映射
安全结果；路径 containment 与 UTF-8 边界由 ``tools.demo_workspace`` 拥有，
side effect / idempotency 事实由 ``spec_for()`` 派生，不由模型自报。
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from core.runtime.retry import OperationIdempotency
from core.runtime.tool_adapters import (
    ToolAdapter,
    ToolAdapterContext,
    ToolAdapterInvocationError,
    ToolAdapterResponse,
)
from core.runtime.tool_contract import (
    ToolErrorCategory,
    ToolExecutionPhase,
    ToolExecutionSpec,
    ToolInvocation,
    ToolSideEffectKind,
    ToolSideEffectState,
    thaw_json,
)
from tools import demo_workspace


def _validation_error(safe_message: str) -> ToolAdapterInvocationError:
    return ToolAdapterInvocationError(
        category=ToolErrorCategory.VALIDATION,
        safe_error_code="TOOL_VALIDATION_ERROR",
        safe_message=safe_message,
        phase=ToolExecutionPhase.VALIDATION,
    )


class WorkspaceReadToolAdapter(ToolAdapter):
    """读取 Demo Workspace 内一个相对路径 UTF-8 文本文件。"""

    is_async = False

    def __init__(
        self,
        *,
        tool_name: str = "workspace_read_file",
        workspace_root: str | None = None,
        max_read_bytes: int = demo_workspace.DEFAULT_MAX_READ_BYTES,
        default_timeout_seconds: float = 3.0,
        max_output_bytes: int = 16_384,
        max_concurrency: int = 4,
    ) -> None:
        self._tool_name = tool_name
        self._workspace_root = (
            workspace_root or demo_workspace.default_demo_workspace_root()
        )
        self._max_read_bytes = max_read_bytes
        self.spec = ToolExecutionSpec(
            tool_name=tool_name,
            side_effect_kind=ToolSideEffectKind.NONE,
            idempotency=OperationIdempotency.READ_ONLY,
            requires_resource_key=False,
            supports_cooperative_cancellation=False,
            supports_side_effect_checkpoint=False,
            default_timeout_seconds=default_timeout_seconds,
            max_output_bytes=max_output_bytes,
            max_concurrency=max_concurrency,
        )

    def llm_input_schema(self) -> dict[str, object]:
        """Model-facing schema：只有业务字段 path；不暴露 root / 治理事实。"""
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Demo Workspace 内的相对路径，例如 example.txt 或 "
                        "notes/result.txt；不要传绝对路径或包含 .. 的路径"
                    ),
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        }

    def _parse_arguments(self, argument_text: str) -> Mapping[str, Any]:
        try:
            payload = json.loads(argument_text)
        except (json.JSONDecodeError, TypeError):
            raise _validation_error("workspace read 参数必须是 JSON object。") from None
        if not isinstance(payload, dict):
            raise _validation_error("workspace read 参数必须是 JSON object。")
        payload = dict(payload)
        path = payload.get("path")
        if not isinstance(path, str) or not path.strip():
            raise _validation_error("workspace read 缺少业务字段 path。")
        return payload

    def build_invocation(self, argument_text: str) -> ToolInvocation:
        payload = self._parse_arguments(argument_text)
        return ToolInvocation.create(
            tool_name=self.spec.tool_name,
            arguments={"path": payload["path"]},
        )

    def spec_for(self, invocation: ToolInvocation) -> ToolExecutionSpec:
        if invocation.tool_name != self.spec.tool_name:
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.VALIDATION,
                safe_error_code="TOOL_NAME_MISMATCH",
                safe_message="Tool Invocation 与 Adapter 名称不匹配。",
                phase=ToolExecutionPhase.VALIDATION,
            )
        return self.spec

    def invoke_once(
        self, invocation: ToolInvocation, context: ToolAdapterContext
    ) -> ToolAdapterResponse:
        context.raise_if_cancelled()
        arguments = thaw_json(invocation.arguments)
        try:
            content = demo_workspace.read_workspace_text_file(
                self._workspace_root,
                arguments.get("path"),
                max_bytes=self._max_read_bytes,
            )
        except demo_workspace.WorkspacePathError:
            raise _validation_error(
                "workspace read 的 path 越出 Demo Workspace 边界，已拒绝。"
            ) from None
        except FileNotFoundError:
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.NOT_FOUND,
                safe_error_code="TOOL_WORKSPACE_FILE_NOT_FOUND",
                safe_message="Demo Workspace 中不存在该文件。",
                phase=ToolExecutionPhase.INVOCATION,
            ) from None
        except IsADirectoryError:
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.VALIDATION,
                safe_error_code="TOOL_WORKSPACE_NOT_A_FILE",
                safe_message="该 workspace path 是目录而不是文件。",
                phase=ToolExecutionPhase.INVOCATION,
            ) from None
        except OverflowError:
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.OUTPUT_TOO_LARGE,
                safe_error_code="TOOL_WORKSPACE_FILE_TOO_LARGE",
                safe_message="文件超过 workspace 读取大小上限。",
                phase=ToolExecutionPhase.OUTPUT,
            ) from None
        except (UnicodeDecodeError, ValueError):
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.OUTPUT_INVALID,
                safe_error_code="TOOL_WORKSPACE_NOT_UTF8_TEXT",
                safe_message="文件不是有效的 UTF-8 文本。",
                phase=ToolExecutionPhase.OUTPUT,
            ) from None
        except OSError:
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.INTERNAL,
                safe_error_code="TOOL_WORKSPACE_READ_FAILED",
                safe_message="workspace 文件读取失败。",
                phase=ToolExecutionPhase.INVOCATION,
            ) from None
        return ToolAdapterResponse(
            content=content,
            content_type="text/plain",
            safe_summary="workspace 只读文件已完成。",
        )


class WorkspaceWriteToolAdapter(ToolAdapter):
    """把给定文本 set/overwrite 到 Demo Workspace 内相对路径文件。"""

    is_async = False

    def __init__(
        self,
        *,
        tool_name: str = "workspace_write_file",
        workspace_root: str | None = None,
        max_write_bytes: int = demo_workspace.DEFAULT_MAX_WRITE_BYTES,
        default_timeout_seconds: float = 3.0,
        max_output_bytes: int = 4096,
        max_concurrency: int = 1,
    ) -> None:
        self._tool_name = tool_name
        self._workspace_root = (
            workspace_root or demo_workspace.default_demo_workspace_root()
        )
        self._max_write_bytes = max_write_bytes
        # 静态 spec 只表达保守基线；调用分类由 spec_for(invocation) 派生。
        self.spec = ToolExecutionSpec(
            tool_name=tool_name,
            side_effect_kind=ToolSideEffectKind.NONE,
            idempotency=OperationIdempotency.READ_ONLY,
            requires_resource_key=False,
            supports_cooperative_cancellation=False,
            supports_side_effect_checkpoint=False,
            default_timeout_seconds=default_timeout_seconds,
            max_output_bytes=max_output_bytes,
            max_concurrency=max_concurrency,
        )

    def llm_input_schema(self) -> dict[str, object]:
        """Model-facing schema：业务字段 path + content；不暴露治理事实。"""
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Demo Workspace 内的相对路径，例如 result.txt 或 "
                        "notes/result.txt；不要传绝对路径或包含 .. 的路径"
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "要写入文件的完整 UTF-8 文本内容（整体覆盖）",
                },
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        }

    def _parse_arguments(self, argument_text: str) -> Mapping[str, Any]:
        try:
            payload = json.loads(argument_text)
        except (json.JSONDecodeError, TypeError):
            raise _validation_error("workspace write 参数必须是 JSON object。") from None
        if not isinstance(payload, dict):
            raise _validation_error("workspace write 参数必须是 JSON object。")
        payload = dict(payload)
        if not isinstance(payload.get("path"), str) or not payload["path"].strip():
            raise _validation_error("workspace write 缺少业务字段 path。")
        if not isinstance(payload.get("content"), str):
            raise _validation_error("workspace write 缺少业务字段 content。")
        return payload

    def build_invocation(self, argument_text: str) -> ToolInvocation:
        payload = self._parse_arguments(argument_text)
        return ToolInvocation.create(
            tool_name=self.spec.tool_name,
            arguments={"path": payload["path"], "content": payload["content"]},
        )

    def spec_for(self, invocation: ToolInvocation) -> ToolExecutionSpec:
        """写工具的 side effect / idempotency 唯一事实源。

        set/overwrite 语义下同一 path+content 重放产生相同最终状态，因此
        分类为 ``LOCAL_STATE_MUTATION + IDEMPOTENT``（受控幂等写）。
        """
        if invocation.tool_name != self.spec.tool_name:
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.VALIDATION,
                safe_error_code="TOOL_NAME_MISMATCH",
                safe_message="Tool Invocation 与 Adapter 名称不匹配。",
                phase=ToolExecutionPhase.VALIDATION,
            )
        arguments = thaw_json(invocation.arguments)
        if not isinstance(arguments.get("path"), str) or not isinstance(
            arguments.get("content"), str
        ):
            raise _validation_error("workspace write 参数缺少业务字段。")
        from dataclasses import replace

        return replace(
            self.spec,
            side_effect_kind=ToolSideEffectKind.LOCAL_STATE_MUTATION,
            idempotency=OperationIdempotency.IDEMPOTENT,
        )

    def invoke_once(
        self, invocation: ToolInvocation, context: ToolAdapterContext
    ) -> ToolAdapterResponse:
        context.raise_if_cancelled()
        arguments = thaw_json(invocation.arguments)
        try:
            context.before_side_effect()
            written = demo_workspace.write_workspace_text_file(
                self._workspace_root,
                arguments.get("path"),
                arguments.get("content"),
                max_bytes=self._max_write_bytes,
            )
        except demo_workspace.WorkspacePathError:
            raise _validation_error(
                "workspace write 的 path 越出 Demo Workspace 边界，已拒绝。"
            ) from None
        except OverflowError:
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.OUTPUT_TOO_LARGE,
                safe_error_code="TOOL_WORKSPACE_CONTENT_TOO_LARGE",
                safe_message="内容超过 workspace 写入大小上限。",
                phase=ToolExecutionPhase.INVOCATION,
            ) from None
        except TypeError:
            raise _validation_error("workspace write 内容必须是字符串。") from None
        except OSError:
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.INTERNAL,
                safe_error_code="TOOL_WORKSPACE_WRITE_FAILED",
                safe_message="workspace 文件写入失败。",
                phase=ToolExecutionPhase.INVOCATION,
                side_effect_state=ToolSideEffectState.UNKNOWN,
            ) from None
        return ToolAdapterResponse(
            content=json.dumps(
                {"written_bytes": written},
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ),
            content_type="application/json",
            safe_summary="workspace 写入已完成。",
            side_effect_state=ToolSideEffectState.COMMITTED,
        )


__all__ = [
    "WorkspaceReadToolAdapter",
    "WorkspaceWriteToolAdapter",
]
