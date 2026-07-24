#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Vector Store 分数语义与唯一归一化边界。"""

from __future__ import annotations

import math
from enum import Enum


class VectorScoreSemantics(str, Enum):
    RAW_DISTANCE = "RAW_DISTANCE"
    NORMALIZED_RELEVANCE = "NORMALIZED_RELEVANCE"


def normalize_vector_score(
    value: float,
    semantics: VectorScoreSemantics,
) -> float:
    """把明确语义的输入恰好转换一次为 [0, 1] 且越高越相关的分数。"""
    if not isinstance(semantics, VectorScoreSemantics):
        raise ValueError("Vector score semantics 必须显式声明为枚举值")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Vector score 必须是数值")
    score = float(value)
    if not math.isfinite(score):
        raise ValueError("Vector score 必须是有限数")
    if semantics == VectorScoreSemantics.RAW_DISTANCE:
        return 1.0 / (1.0 + max(0.0, score))
    if semantics == VectorScoreSemantics.NORMALIZED_RELEVANCE:
        return min(1.0, max(0.0, score))
    raise ValueError("不支持的 Vector score semantics")


__all__ = ["VectorScoreSemantics", "normalize_vector_score"]
