#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Agent history read policy.

``HistoryPolicy`` 只控制“读取”历史，与 ``persist``（控制写入）是两条
独立的轴。内部 multi-agent specialist/synthesis 调用必须显式使用
``NONE``，避免旧 Run Memory 成为隐藏输入来源；Legacy/direct 路径保持
默认 ``AGENT_SCOPE`` 行为不变。
"""

from __future__ import annotations

from enum import Enum


class HistoryPolicy(str, Enum):
    AGENT_SCOPE = "AGENT_SCOPE"
    NONE = "NONE"


__all__ = ["HistoryPolicy"]
