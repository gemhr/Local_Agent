#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Application-level 安全 metadata 与每进程 instance identity。

本模块不读取任何环境变量；instance_id 是进程生成的一次性 UUID，不可从
配置覆盖、不跨重启复用。所有字段均为 frozen safe metadata，不含 secret。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ApplicationMetadata:
    """进程启动时冻结的安全 Application 身份与版本元数据。"""

    environment_profile: str
    environment_id: str
    service_version: str
    instance_id: str

    def to_safe_dict(self) -> dict[str, str]:
        """只输出 allowlist 安全字段。"""
        return {
            "environment_profile": self.environment_profile,
            "environment_id": self.environment_id,
            "service_version": self.service_version,
            "instance_id": self.instance_id,
        }


def new_instance_id() -> str:
    """生成一次性进程 identity（CSPRNG UUID），不跨重启复用。"""
    return uuid.uuid4().hex


def create_application_metadata(
    settings,
    *,
    instance_id: str | None = None,
) -> ApplicationMetadata:
    """从 Settings 构建 frozen Application metadata。

    instance_id 由 Composition Root 每进程生成一次并注入；本函数不读取 env。
    """
    return ApplicationMetadata(
        environment_profile=settings.environment_profile.value,
        environment_id=settings.environment_id,
        service_version=settings.service_version,
        instance_id=instance_id or new_instance_id(),
    )


__all__ = [
    "ApplicationMetadata",
    "create_application_metadata",
    "new_instance_id",
]
