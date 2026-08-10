#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Minimal Persistence Migration Coordinator — WP1-D。

该模块只负责：

- preflight orchestration（read-only 检测与校验）
- migration ordering
- safe result aggregation
- safe error / result model

不得成为任何 Store 的 schema owner：Memory / Journal / Snapshot /
Checkpoint 的 signature、version truth、SQL 与 transaction 保留在对应
Store module；Coordinator 只编排调用。Chroma 因需要打开 VectorDB，
不在此模块内直接打开（marker validation 属于 VectorDBManager）。
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Safe Store IDs（低基数，固定 allowlist）
# ---------------------------------------------------------------------------


class StoreId(str, Enum):
    MEMORY = "MEMORY"
    EVENT_JOURNAL = "EVENT_JOURNAL"
    SNAPSHOT = "SNAPSHOT"
    OBSERVABILITY_CHECKPOINT = "OBSERVABILITY_CHECKPOINT"
    CHROMA = "CHROMA"


# ---------------------------------------------------------------------------
# Preflight Status（冻结外部合同）
# ---------------------------------------------------------------------------


class PreflightStatus(str, Enum):
    NEW = "NEW"
    CURRENT = "CURRENT"
    MIGRATION_REQUIRED = "MIGRATION_REQUIRED"
    REBUILD_REQUIRED = "REBUILD_REQUIRED"
    UNSUPPORTED = "UNSUPPORTED"
    FAILED = "FAILED"


class MigrationAction(str, Enum):
    NONE = "NONE"
    INITIALIZE = "INITIALIZE"
    MIGRATE = "MIGRATE"
    RECREATE = "RECREATE"
    REBUILD = "REBUILD"


class PreflightMode(str, Enum):
    STARTUP = "STARTUP"
    FULL = "FULL"


# ---------------------------------------------------------------------------
# Safe Error Codes（新增且仅新增三个）
# ---------------------------------------------------------------------------

PERSISTENCE_SCHEMA_UNSUPPORTED = "PERSISTENCE_SCHEMA_UNSUPPORTED"
PERSISTENCE_PREFLIGHT_FAILED = "PERSISTENCE_PREFLIGHT_FAILED"
PERSISTENCE_MIGRATION_FAILED = "PERSISTENCE_MIGRATION_FAILED"

_SAFE_ERROR_MESSAGES = {
    PERSISTENCE_SCHEMA_UNSUPPORTED: "persistence schema is newer than or outside the supported set",
    PERSISTENCE_PREFLIGHT_FAILED: "persistence preflight failed",
    PERSISTENCE_MIGRATION_FAILED: "persistence migration failed",
}


class PersistenceError(Exception):
    """Safe typed persistence error；不暴露 SQL / path / 正文 / exception text。"""

    def __init__(self, error_code: str, safe_message: Optional[str] = None) -> None:
        self.error_code = error_code
        self.safe_message = safe_message or _SAFE_ERROR_MESSAGES[error_code]
        super().__init__(f"{self.safe_message} (error_code={error_code})")

    def __repr__(self) -> str:
        return f"PersistenceError(error_code={self.error_code!r})"


# ---------------------------------------------------------------------------
# Safe Result Model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PersistencePreflightResult:
    store_id: StoreId
    status: PreflightStatus
    action: MigrationAction
    detected_version: Optional[str] = None
    target_version: Optional[str] = None
    safe_error_code: Optional[str] = None


@dataclass(frozen=True)
class PersistenceMigrationResult:
    store_id: StoreId
    action: MigrationAction
    committed: bool
    safe_error_code: Optional[str] = None


@dataclass(frozen=True)
class PersistencePaths:
    """From Settings；Snapshot 未启用时 snapshot_store_db_path 为 None。"""

    memory_db_path: str
    event_journal_db_path: str
    observability_checkpoint_db_path: str
    snapshot_store_db_path: Optional[str] = None


@dataclass(frozen=True)
class MigrationOutcome:
    results: tuple[PersistenceMigrationResult, ...]

    @property
    def failed(self) -> bool:
        return any(result.safe_error_code is not None for result in self.results)


# ---------------------------------------------------------------------------
# Shared read-only SQLite helper（generic，不属任何 Store schema）
# ---------------------------------------------------------------------------


def _read_only_uri(db_path: str) -> str:
    return f"file:{Path(db_path).resolve().as_posix()}?mode=ro"


def sqlite_quick_check(db_path: str) -> None:
    """Read-only open + PRAGMA quick_check；任何非单一 ok 均抛 safe error。"""
    conn = sqlite3.connect(_read_only_uri(db_path), uri=True)
    try:
        row = conn.execute("PRAGMA quick_check").fetchone()
        if row is None or row[0] != "ok":
            raise PersistenceError(PERSISTENCE_PREFLIGHT_FAILED)
    except PersistenceError:
        raise
    except sqlite3.Error:
        raise PersistenceError(PERSISTENCE_PREFLIGHT_FAILED) from None
    finally:
        conn.close()


def open_read_only(db_path: str) -> sqlite3.Connection:
    """Read-only SQLite open；调用方负责 close。失败抛 safe error。"""
    try:
        conn = sqlite3.connect(_read_only_uri(db_path), uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        raise PersistenceError(PERSISTENCE_PREFLIGHT_FAILED) from None


# ---------------------------------------------------------------------------
# Coordinator preflight
# ---------------------------------------------------------------------------


def run_persistence_preflight(
    paths: PersistencePaths, *, mode: PreflightMode = PreflightMode.STARTUP
) -> tuple[PersistencePreflightResult, ...]:
    """对所有 LocalAgent-owned SQLite Store 执行只读 preflight。

    mode=FULL 增加逐 record 校验（Journal digest/terminal、Snapshot
    envelope/digest、Memory bounded read probe）；startup 模式只做
    quick_check / physical shape / 支持的版本事实。
    """
    from core.memory_manager import memory_preflight
    from core.runtime.event_consumer import checkpoint_preflight
    from core.runtime.event_journal_store import journal_preflight
    from core.runtime.snapshot_store import snapshot_preflight

    results: list[PersistencePreflightResult] = [
        memory_preflight(paths.memory_db_path, mode=mode),
        journal_preflight(paths.event_journal_db_path, mode=mode),
    ]
    if paths.snapshot_store_db_path is not None:
        results.append(snapshot_preflight(paths.snapshot_store_db_path, mode=mode))
    results.append(checkpoint_preflight(paths.observability_checkpoint_db_path))
    return tuple(results)


def _blocking_statuses() -> frozenset[PreflightStatus]:
    return frozenset(
        {
            PreflightStatus.MIGRATION_REQUIRED,
            PreflightStatus.UNSUPPORTED,
            PreflightStatus.FAILED,
        }
    )


def preflight_blocks_startup(
    results: tuple[PersistencePreflightResult, ...],
) -> list[PersistencePreflightResult]:
    return [result for result in results if result.status in _blocking_statuses()]


# ---------------------------------------------------------------------------
# Coordinator migration（explicit SCRIPT_ROLE only）
# ---------------------------------------------------------------------------


def run_persistence_migration(
    paths: PersistencePaths, *, backup_confirmed: bool
) -> MigrationOutcome:
    """显式 migrate：先全 Store preflight，再按顺序对 MIGRATION_REQUIRED
    Store 执行独立单 Store transaction。

    - 任何 UNSUPPORTED / FAILED：抛 PERSISTENCE_MIGRATION_FAILED，不开始 mutation；
    - 有 MIGRATION_REQUIRED 但缺 --backup-confirmed：抛错，不开始 mutation；
    - 单个 Store 失败：记录 committed=False + safe code，停止后续执行，
      已 commit 的 Store 保留（partial committed facts），CLI 以非零退出。
    """
    from core.memory_manager import memory_migrate
    from core.runtime.event_consumer import checkpoint_recreate
    from core.runtime.event_journal_store import journal_migrate

    preflight = run_persistence_preflight(paths, mode=PreflightMode.FULL)
    for result in preflight:
        if result.status in {PreflightStatus.UNSUPPORTED, PreflightStatus.FAILED}:
            raise PersistenceError(
                PERSISTENCE_MIGRATION_FAILED,
                "preflight 发现 UNSUPPORTED 或 FAILED Store，未开始任何 mutation",
            )

    planned = [
        result for result in preflight if result.status is PreflightStatus.MIGRATION_REQUIRED
    ]
    if planned and not backup_confirmed:
        raise PersistenceError(
            PERSISTENCE_MIGRATION_FAILED,
            "已有数据需要迁移，必须显式提供 --backup-confirmed",
        )

    results: list[PersistenceMigrationResult] = [
        PersistenceMigrationResult(
            store_id=result.store_id,
            action=MigrationAction.NONE,
            committed=False,
        )
        for result in preflight
        if result.status in {PreflightStatus.NEW, PreflightStatus.CURRENT}
    ]

    # 固定顺序：Memory → Journal → Checkpoint。
    ordered_actions: list[tuple[StoreId, str, object]] = []
    for result in planned:
        if result.store_id is StoreId.MEMORY:
            ordered_actions.append(
                (StoreId.MEMORY, paths.memory_db_path, memory_migrate)
            )
        elif result.store_id is StoreId.EVENT_JOURNAL:
            ordered_actions.append(
                (StoreId.EVENT_JOURNAL, paths.event_journal_db_path, journal_migrate)
            )
        elif result.store_id is StoreId.OBSERVABILITY_CHECKPOINT:
            ordered_actions.append(
                (
                    StoreId.OBSERVABILITY_CHECKPOINT,
                    paths.observability_checkpoint_db_path,
                    checkpoint_recreate,
                )
            )

    for store_id, db_path, mutate in ordered_actions:
        action = MigrationAction.MIGRATE
        if store_id is StoreId.OBSERVABILITY_CHECKPOINT:
            action = MigrationAction.RECREATE
        try:
            mutate(db_path)
        except PersistenceError as exc:
            results.append(
                PersistenceMigrationResult(
                    store_id=store_id,
                    action=action,
                    committed=False,
                    safe_error_code=exc.error_code,
                )
            )
            return MigrationOutcome(tuple(results))
        except sqlite3.Error:
            results.append(
                PersistenceMigrationResult(
                    store_id=store_id,
                    action=action,
                    committed=False,
                    safe_error_code=PERSISTENCE_MIGRATION_FAILED,
                )
            )
            return MigrationOutcome(tuple(results))
        results.append(
            PersistenceMigrationResult(
                store_id=store_id,
                action=action,
                committed=True,
            )
        )

    return MigrationOutcome(tuple(results))


__all__ = [
    "MigrationAction",
    "MigrationOutcome",
    "PERSISTENCE_MIGRATION_FAILED",
    "PERSISTENCE_PREFLIGHT_FAILED",
    "PERSISTENCE_SCHEMA_UNSUPPORTED",
    "PersistenceError",
    "PersistenceMigrationResult",
    "PersistencePaths",
    "PersistencePreflightResult",
    "PreflightMode",
    "PreflightStatus",
    "StoreId",
    "open_read_only",
    "preflight_blocks_startup",
    "run_persistence_migration",
    "run_persistence_preflight",
    "sqlite_quick_check",
]
