#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PostgreSQL database preflight：可读、schema 存在、Alembic revision 兼容。

严格只读：本模块只执行 ``SELECT``，**不**创建、修改或迁移任何对象，也**不**
执行 ``alembic upgrade``（Stage6 冻结 ``API startup != Migration Owner``）。
migration 只由显式 operator 命令产生。

失败映射为 typed 错误码（沿用项目既有风格）：

* ``DATABASE_UNAVAILABLE``        —— 连接/认证/网络不可达；
* ``DATABASE_SCHEMA_NOT_READY``   —— 表或 ``alembic_version`` 尚不存在；
* ``DATABASE_SCHEMA_INCOMPATIBLE``—— revision 与当前代码 head 不一致，或
  revision graph 自身不是单一 head。

本模块绝不输出密码、完整 DSN、host、SQL 语句或原始驱动异常文本。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text

from core.persistence.database import Database
from core.persistence.errors import (
    DatabaseErrorCode,
    PersistenceError,
    to_persistence_error,
)
from core.persistence.models import CANONICAL_TABLES

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ALEMBIC_INI = _PROJECT_ROOT / "alembic.ini"
_ALEMBIC_DIR = _PROJECT_ROOT / "alembic"

_EXISTING_TABLES_SQL = text(
    """
    SELECT table_name FROM information_schema.tables
    WHERE table_schema = current_schema()
    """
)

_ALEMBIC_REVISION_SQL = text("SELECT version_num FROM alembic_version")


def alembic_head_revision() -> str | None:
    """从 Alembic revision graph 读取单一 head；无 head 或多 head 返回 None。"""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        config = Config(str(_ALEMBIC_INI))
        config.set_main_option("script_location", str(_ALEMBIC_DIR))
        script = ScriptDirectory.from_config(config)
        heads = script.get_heads()
    except Exception:
        return None
    if len(heads) != 1:
        return None
    return str(heads[0])


@dataclass(frozen=True)
class SchemaReadiness:
    """只读 preflight 结果；不包含任何连接或凭据信息。"""

    ready: bool
    error_code: DatabaseErrorCode | None
    alembic_revision: str | None
    alembic_head: str | None
    missing_tables: tuple[str, ...]

    def __repr__(self) -> str:
        return (
            "SchemaReadiness("
            f"ready={self.ready!r}, "
            f"error_code={self.error_code.value if self.error_code else None!r}, "
            f"alembic_revision={self.alembic_revision!r}, "
            f"alembic_head={self.alembic_head!r}, "
            f"missing_tables={self.missing_tables!r})"
        )


def _result(
    *,
    ready: bool,
    error_code: DatabaseErrorCode | None,
    revision: str | None = None,
    head: str | None = None,
    missing: tuple[str, ...] = (),
) -> SchemaReadiness:
    return SchemaReadiness(
        ready=ready,
        error_code=error_code,
        alembic_revision=revision,
        alembic_head=head,
        missing_tables=missing,
    )


async def check_schema_readiness(
    database: Database,
    *,
    required_tables: tuple[str, ...] = CANONICAL_TABLES,
) -> SchemaReadiness:
    """只读判定 PostgreSQL schema readiness；不修改数据库。"""
    if not isinstance(database, Database):
        raise TypeError("database 必须是 Database")

    try:
        async with database.engine.connect() as connection:
            existing = {
                str(row[0])
                for row in (
                    await connection.execute(_EXISTING_TABLES_SQL)
                ).fetchall()
            }
            missing = tuple(
                name for name in required_tables if name not in existing
            )
            if missing:
                return _result(
                    ready=False,
                    error_code=DatabaseErrorCode.DATABASE_SCHEMA_NOT_READY,
                    missing=missing,
                )
            if "alembic_version" not in existing:
                return _result(
                    ready=False,
                    error_code=DatabaseErrorCode.DATABASE_SCHEMA_NOT_READY,
                )
            rows = (
                await connection.execute(_ALEMBIC_REVISION_SQL)
            ).fetchall()
    except PersistenceError:
        raise
    except Exception as exc:
        error = to_persistence_error(exc, operation="schema_readiness")
        if error.error_code is DatabaseErrorCode.DATABASE_SCHEMA_NOT_READY:
            return _result(
                ready=False,
                error_code=DatabaseErrorCode.DATABASE_SCHEMA_NOT_READY,
            )
        raise error from None

    if len(rows) != 1:
        # alembic_version 必须恰好一行；多行代表 revision 状态被破坏。
        return _result(
            ready=False,
            error_code=DatabaseErrorCode.DATABASE_SCHEMA_INCOMPATIBLE,
        )
    revision = str(rows[0][0])
    head = alembic_head_revision()
    if head is None:
        return _result(
            ready=False,
            error_code=DatabaseErrorCode.DATABASE_SCHEMA_INCOMPATIBLE,
            revision=revision,
        )
    if revision != head:
        return _result(
            ready=False,
            error_code=DatabaseErrorCode.DATABASE_SCHEMA_INCOMPATIBLE,
            revision=revision,
            head=head,
        )
    return _result(
        ready=True, error_code=None, revision=revision, head=head
    )


async def assert_schema_ready(database: Database) -> SchemaReadiness:
    """Startup 便捷入口：schema 未就绪时抛出 typed ``PersistenceError``。"""
    readiness = await check_schema_readiness(database)
    if not readiness.ready:
        raise PersistenceError(
            readiness.error_code
            or DatabaseErrorCode.DATABASE_SCHEMA_NOT_READY,
            operation="schema_readiness",
        )
    return readiness


__all__ = [
    "SchemaReadiness",
    "alembic_head_revision",
    "assert_schema_ready",
    "check_schema_readiness",
]
