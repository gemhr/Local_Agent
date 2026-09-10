#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Await-aware 适配助手。

PostgreSQL Canonical Store 全部是 Awaitable；进程内 / 测试替身（例如
``InMemoryRunEventJournal``）保持同步实现，因为它们不执行任何 I/O。

消费者用本助手统一处理两者，避免为了测试替身把整个 Runtime 改成 async，
也避免生产路径出现同步数据库调用。
"""

from __future__ import annotations

import inspect
from typing import Any, TypeVar

T = TypeVar("T")


async def resolve(value: T | Any) -> T:
    """值可等待时 await，否则原样返回。"""
    if inspect.isawaitable(value):
        return await value
    return value  # type: ignore[return-value]


__all__ = ["resolve"]
