#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WP3-R1 Canonical Predicate Registry（v1）。

``logical_key`` 在 WP3 中的角色是 canonical predicate identity：``NO_CHANGE`` /
``SUPERSEDE`` / exact-key ``FORGET`` 都按它划分 partition，因此其值不能由 Model
自由发明后直接持久化。本模块是小型、code-owned 的 v1 lifecycle-managed semantic
slot 注册表，不是 ontology、知识图谱、通用 schema registry 或 new-predicate 平台。

- registry 不存数据库、不提供动态注册、配置入口或外部写接口；
- 没有 alias lookup、fuzzy matching 或 Model-defined namespace；
- `logical_key` 只能由 LocalAgent 从 accepted registry entry 编译；
- `NEW_PREDICATE` 不接受；新增 slot 必须是独立 code-and-contract 决策。

v1 只冻结三个精确、单值 slot（`15_wp3_predicate_resolution_architecture_amendment.md`
冻结；`user.response_style` / `project.runtime` / `engineering.network_access` 已被
移除或改名，不在 v1）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, FrozenSet, Optional


class PredicateResolution(str, Enum):
    """Formation proposal 的显式 predicate 判定（REMEMBER 必填）。"""

    REGISTERED = "REGISTERED"
    OPEN = "OPEN"


class PredicateValueConstraint(str, Enum):
    """v1 slot 的 value 约束（只保护已冻结 slot 的 typed lifecycle，
    不是通用 payload schema）。"""

    NON_EMPTY_STRING = "NON_EMPTY_STRING"
    BOOLEAN = "BOOLEAN"


@dataclass(frozen=True)
class CanonicalPredicateSlot:
    """一个 registry slot。predicate ID 与 canonical logical_key 相同。"""

    predicate_id: str
    allowed_categories: FrozenSet[str]
    value_constraint: PredicateValueConstraint
    supersede_allowed: bool = True
    forget_allowed: bool = True

    @property
    def canonical_logical_key(self) -> str:
        """LocalAgent 编译出的 authoritative ``logical_key``。"""
        return self.predicate_id

    def validate_value(self, value: object) -> bool:
        if self.value_constraint is PredicateValueConstraint.BOOLEAN:
            # strict bool：字符串 "false"/"no"/"disabled" 一律不 coercion。
            return isinstance(value, bool)
        if self.value_constraint is PredicateValueConstraint.NON_EMPTY_STRING:
            return isinstance(value, str) and bool(value.strip())
        return False


class CanonicalPredicateRegistry:
    """v1 冻结注册表；仅 LocalAgent code-owned 只读。

    不做 dynamic registration / config plugin / database table / ontology /
    alias table / external API。
    """

    _SLOTS: Dict[str, CanonicalPredicateSlot] = {
        "project.database": CanonicalPredicateSlot(
            predicate_id="project.database",
            allowed_categories=frozenset(
                {"PROJECT_STABLE_FACT", "LONG_TERM_DECISION"}
            ),
            value_constraint=PredicateValueConstraint.NON_EMPTY_STRING,
        ),
        "project.package_manager": CanonicalPredicateSlot(
            predicate_id="project.package_manager",
            allowed_categories=frozenset(
                {
                    "PROJECT_STABLE_FACT",
                    "ENGINEERING_CONSTRAINT",
                    "LONG_TERM_DECISION",
                }
            ),
            value_constraint=PredicateValueConstraint.NON_EMPTY_STRING,
        ),
        "engineering.public_network_allowed": CanonicalPredicateSlot(
            predicate_id="engineering.public_network_allowed",
            allowed_categories=frozenset({"ENGINEERING_CONSTRAINT"}),
            value_constraint=PredicateValueConstraint.BOOLEAN,
        ),
    }

    @classmethod
    def get(cls, predicate_id: object) -> Optional[CanonicalPredicateSlot]:
        """exact predicate ID lookup；invented/alias/unknown → None。"""
        if not isinstance(predicate_id, str):
            return None
        return cls._SLOTS.get(predicate_id)

    @classmethod
    def all_ids(cls) -> FrozenSet[str]:
        return frozenset(cls._SLOTS)

    @classmethod
    def all_slots(cls) -> Dict[str, CanonicalPredicateSlot]:
        return dict(cls._SLOTS)


__all__ = [
    "CanonicalPredicateRegistry",
    "CanonicalPredicateSlot",
    "PredicateResolution",
    "PredicateValueConstraint",
]