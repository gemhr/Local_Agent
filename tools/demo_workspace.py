#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""受限 Demo Workspace 文件工具的业务语义与路径安全边界（WP2）。

只服务 Phase8 演示用途：``workspace_read_file`` / ``workspace_write_file``
两个本地工具的相对路径解析与 UTF-8 文本读写。所有路径都必须通过
canonical resolve + root containment 校验，任何逃逸一律 fail closed；
本模块不是通用 Sandbox，也不替代 Windows ACL。
"""

from __future__ import annotations

from pathlib import Path
import ntpath
import os


# 固定生产 Demo Root：<repo>/data/demo_workspace。
DEMO_WORKSPACE_DIRNAME = os.path.join("data", "demo_workspace")

# 读/写内容的简单大小边界：避免巨大文件进入 Tool Output 或写入路径。
DEFAULT_MAX_READ_BYTES = 1_048_576
DEFAULT_MAX_WRITE_BYTES = 1_048_576


class WorkspacePathError(ValueError):
    """Workspace 路径逃逸或非法；只携带固定安全文案，不携带原始路径。"""


def default_demo_workspace_root() -> str:
    """返回仓库内固定 Demo Root 的绝对路径（与 Settings.project_root 同源）。"""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo_root, DEMO_WORKSPACE_DIRNAME)


def resolve_workspace_path(root: str | Path, relative: object) -> Path:
    """把模型提供的相对路径解析为 Demo Root 内的 canonical 绝对路径。

    安全边界（fail closed）：

    - 非字符串 / 空 / 含 NUL 的 ``relative`` 直接拒绝；
    - 绝对路径、盘符路径、UNC 路径、drive-relative 路径在
      ``root / relative`` 合并时会覆盖 root，随后 canonical containment
      校验失败（不以 ``".." in path`` 字符串判断）；
    - ``..`` 与 mixed separator 由 ``Path.resolve()`` canonical 化后按
      containment 判定，嵌套逃逸无法通过；
    - Windows symlink / junction 指向 Root 外时 resolved containment 拒绝；
    - 不回退 cwd、不回退 repo root、不自动纠正成其他文件。
    """
    if not isinstance(relative, str):
        raise WorkspacePathError("workspace path 必须是字符串")
    normalized = relative.strip()
    if not normalized or "\x00" in normalized:
        raise WorkspacePathError("workspace path 非法")
    # 显式拒绝盘符路径 / UNC / 设备路径（含 drive-relative "C:foo"）。
    slash_normalized = normalized.replace("/", "\\")
    if (
        "\\" in slash_normalized
        and ":" in slash_normalized.split("\\")[0]
        or slash_normalized.startswith(("\\\\", "\\?\\", "\\."))
        or ntpath.splitdrive(slash_normalized)[0]
    ):
        raise WorkspacePathError("workspace path 越出 Demo Workspace 边界")
    try:
        root_resolved = Path(root).resolve(strict=False)
        candidate = (root_resolved / normalized).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        raise WorkspacePathError("workspace path 无法安全解析") from None
    if candidate == root_resolved or root_resolved not in candidate.parents:
        raise WorkspacePathError("workspace path 越出 Demo Workspace 边界")
    return candidate


def read_workspace_text_file(
    root: str | Path,
    relative: str,
    *,
    max_bytes: int = DEFAULT_MAX_READ_BYTES,
) -> str:
    """读取 Demo Root 内一个 UTF-8 文本文件的全部内容。

    Raises:
        WorkspacePathError: 路径逃逸或非法。
        FileNotFoundError: 目标不存在。
        IsADirectoryError: 目标是目录。
        ValueError: 内容不是 UTF-8 文本。
        OSError: 文件超过读取上限或其他 I/O 失败。
    """
    candidate = resolve_workspace_path(root, relative)
    if not candidate.exists():
        raise FileNotFoundError(candidate.name)
    if not candidate.is_file():
        raise IsADirectoryError(candidate.name)
    size = candidate.stat().st_size
    if size > max_bytes:
        raise OverflowError("workspace file exceeds read limit")
    return candidate.read_text(encoding="utf-8")


def write_workspace_text_file(
    root: str | Path,
    relative: str,
    content: object,
    *,
    max_bytes: int = DEFAULT_MAX_WRITE_BYTES,
) -> int:
    """以 set/overwrite 语义把给定文本写入 Demo Root 内相对路径文件。

    同一 ``path + content`` 重复执行最终文件状态相同（天然幂等写）；
    不存在的父目录会在 Root 内安全创建；不实现 append / mode / delete /
    rename。返回写入的 UTF-8 字节数。

    Raises:
        WorkspacePathError: 路径逃逸或非法。
        TypeError: content 不是字符串或超过写入上限。
        OSError: I/O 失败。
    """
    candidate = resolve_workspace_path(root, relative)
    if not isinstance(content, str):
        raise TypeError("workspace content 必须是字符串")
    encoded = content.encode("utf-8")
    if len(encoded) > max_bytes:
        raise OverflowError("workspace content exceeds write limit")
    # candidate 已通过 containment；父目录必然位于 Root 内。
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text(content, encoding="utf-8")
    return len(encoded)


__all__ = [
    "DEMO_WORKSPACE_DIRNAME",
    "DEFAULT_MAX_READ_BYTES",
    "DEFAULT_MAX_WRITE_BYTES",
    "WorkspacePathError",
    "default_demo_workspace_root",
    "read_workspace_text_file",
    "resolve_workspace_path",
    "write_workspace_text_file",
]
