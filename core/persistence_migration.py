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


__all__ = [
    "MigrationAction",
    "PERSISTENCE_MIGRATION_FAILED",
    "PERSISTENCE_PREFLIGHT_FAILED",
    "PERSISTENCE_SCHEMA_UNSUPPORTED",
    "PersistenceError",
    "PersistencePreflightResult",
    "PreflightMode",
    "PreflightStatus",
    "StoreId",
    "open_read_only",
    "sqlite_quick_check",
]
