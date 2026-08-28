"""WP3-R1 CanonicalPredicateRegistry deterministic tests。

证明 v1 冻结 slot 的 exact lookup、category/value 约束、无 alias/无 dynamic
registration、OPEN/REGISTERED resolution vocabulary 与 supersede/forget
capability 标志。registry 是 code-owned 只读数据，不是 ontology/platform。
"""

from __future__ import annotations

import pytest

from core.runtime import (
    CanonicalPredicateRegistry,
    CanonicalPredicateSlot,
    PredicateResolution,
    PredicateValueConstraint,
)

REGISTRY_IDS = frozenset(
    {"project.database", "project.package_manager", "engineering.public_network_allowed"}
)


def test_registry_has_exactly_v1_slots() -> None:
    assert CanonicalPredicateRegistry.all_ids() == REGISTRY_IDS


def test_exact_lookup_and_canonical_key() -> None:
    for predicate_id in REGISTRY_IDS:
        slot = CanonicalPredicateRegistry.get(predicate_id)
        assert slot is not None
        assert slot.predicate_id == predicate_id
        # canonical logical_key 与 predicate ID 相同（LocalAgent 编译）。
        assert slot.canonical_logical_key == predicate_id


def test_no_alias_lookup() -> None:
    for invented in ("project_database", "project.db", "database_backend",
                     "engineering.network_access", "project.runtime",
                     "user.response_style", "NEW_PREDICATE(x)"):
        assert CanonicalPredicateRegistry.get(invented) is None


def test_project_database_constraints() -> None:
    slot = CanonicalPredicateRegistry.get("project.database")
    assert slot is not None
    assert slot.allowed_categories == frozenset(
        {"PROJECT_STABLE_FACT", "LONG_TERM_DECISION"}
    )
    assert slot.value_constraint is PredicateValueConstraint.NON_EMPTY_STRING
    assert slot.validate_value("SQLite") is True
    assert slot.validate_value("") is False
    assert slot.validate_value(False) is False
    assert slot.supersede_allowed is True
    assert slot.forget_allowed is True


def test_project_package_manager_constraints() -> None:
    slot = CanonicalPredicateRegistry.get("project.package_manager")
    assert slot is not None
    assert slot.allowed_categories == frozenset(
        {"PROJECT_STABLE_FACT", "ENGINEERING_CONSTRAINT", "LONG_TERM_DECISION"}
    )
    assert slot.value_constraint is PredicateValueConstraint.NON_EMPTY_STRING
    assert slot.validate_value("uv") is True


def test_engineering_public_network_allowed_strict_bool() -> None:
    slot = CanonicalPredicateRegistry.get("engineering.public_network_allowed")
    assert slot is not None
    assert slot.allowed_categories == frozenset({"ENGINEERING_CONSTRAINT"})
    assert slot.value_constraint is PredicateValueConstraint.BOOLEAN
    assert slot.validate_value(True) is True
    assert slot.validate_value(False) is True
    # 禁止字符串 "false"/"no"/"disabled" 静默 coercion 成 bool。
    for bad in ("false", "no", "disabled", "0", 0, 1, "true"):
        assert slot.validate_value(bad) is False


def test_category_membership_is_strict() -> None:
    database = CanonicalPredicateRegistry.get("project.database")
    assert database is not None
    assert "PROJECT_STABLE_FACT" in database.allowed_categories
    assert "STABLE_USER_PREFERENCE" not in database.allowed_categories
    assert "ENGINEERING_CONSTRAINT" not in database.allowed_categories


def test_predicate_resolution_vocabulary() -> None:
    assert PredicateResolution.REGISTERED.value == "REGISTERED"
    assert PredicateResolution.OPEN.value == "OPEN"


def test_slots_are_frozen_dataclasses() -> None:
    slot = CanonicalPredicateRegistry.get("project.database")
    assert isinstance(slot, CanonicalPredicateSlot)
    with pytest.raises(Exception):
        slot.predicate_id = "hijacked"  # frozen