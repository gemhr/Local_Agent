#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PostgreSQL 持久化的类型化安全错误与失败映射。

本模块是唯一把驱动层异常（asyncpg / SQLAlchemy / DBAPI）翻译成
LocalAgent typed failure 的位置。上层 Store / Application Service 只处理
``PersistenceError``；**绝不**向 HTTP Client、日志或事件投影暴露：

* 原始 SQL 语句或参数；
* DSN、host、port、database name、user；
* 驱动原始异常文本（可能内嵌 DSN 或行数据）。
"""

from __future__ import annotations

from enum import Enum


class DatabaseErrorCode(str, Enum):
    """稳定、低基数、可安全外传的数据库错误码。"""

    DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
    DATABASE_POOL_EXHAUSTED = "DATABASE_POOL_EXHAUSTED"
    DATABASE_STATEMENT_TIMEOUT = "DATABASE_STATEMENT_TIMEOUT"
    DATABASE_LOCK_TIMEOUT = "DATABASE_LOCK_TIMEOUT"
    DATABASE_IDLE_TRANSACTION_TIMEOUT = "DATABASE_IDLE_TRANSACTION_TIMEOUT"
    DATABASE_INTEGRITY_VIOLATION = "DATABASE_INTEGRITY_VIOLATION"
    DATABASE_SERIALIZATION_FAILURE = "DATABASE_SERIALIZATION_FAILURE"
    DATABASE_SCHEMA_NOT_READY = "DATABASE_SCHEMA_NOT_READY"
    DATABASE_SCHEMA_INCOMPATIBLE = "DATABASE_SCHEMA_INCOMPATIBLE"
    DATABASE_OPERATION_FAILED = "DATABASE_OPERATION_FAILED"
    DATABASE_CLOSED = "DATABASE_CLOSED"


# 允许由 Transaction Owner 做有限重试的错误码。只覆盖短、已证明幂等的事务：
# 序列化失败与死锁是 PostgreSQL 明确要求调用方重试的条件。
RETRYABLE_DATABASE_ERROR_CODES = frozenset(
    {
        DatabaseErrorCode.DATABASE_SERIALIZATION_FAILURE,
    }
)

# PostgreSQL SQLSTATE（由 asyncpg 抛出，挂在 SQLAlchemy 异常对象的 ``orig``）。
_SQLSTATE_QUERY_CANCELED = "57014"
_SQLSTATE_LOCK_NOT_AVAILABLE = "55P03"
_SQLSTATE_DEADLOCK_DETECTED = "40P01"
_SQLSTATE_SERIALIZATION_FAILURE = "40001"
_SQLSTATE_IDLE_IN_TRANSACTION_TIMEOUT = "25P03"
_SQLSTATE_INSUFFICIENT_PRIVILEGE = "42501"
_SQLSTATE_UNDEFINED_TABLE = "42P01"
_SQLSTATE_UNDEFINED_COLUMN = "42703"
_SQLSTATE_UNDEFINED_OBJECT = "42704"

_INTEGRITY_CLASS_PREFIX = "23"
_CONNECTION_CLASS_PREFIX = "08"
_UNAVAILABLE_SQLSTATES = frozenset(
    {
        "57P01",  # admin_shutdown
        "57P02",  # crash_shutdown
        "57P03",  # cannot_connect_now
        "53300",  # too_many_connections
    }
)

_SCHEMA_NOT_READY_SQLSTATES = frozenset(
    {_SQLSTATE_UNDEFINED_TABLE, _SQLSTATE_UNDEFINED_COLUMN, _SQLSTATE_UNDEFINED_OBJECT}
)


class PersistenceError(Exception):
    """不暴露 DSN、SQL 或原始驱动异常的类型化持久化错误。"""

    def __init__(
        self,
        error_code: DatabaseErrorCode,
        *,
        operation: str,
        retryable: bool = False,
    ) -> None:
        if not isinstance(error_code, DatabaseErrorCode):
            raise TypeError("error_code 必须是 DatabaseErrorCode")
        if not isinstance(operation, str) or not operation.strip():
            raise ValueError("operation 必须是非空字符串")
        self.error_code = error_code
        self.operation = operation
        self.retryable = bool(retryable)
        super().__init__(f"{error_code.value} (operation={operation})")

    def __repr__(self) -> str:
        return (
            "PersistenceError("
            f"error_code={self.error_code.value!r}, "
            f"operation={self.operation!r}, retryable={self.retryable!r})"
        )


def _sqlstate(exc: BaseException) -> str | None:
    """从 SQLAlchemy/asyncpg 异常链上提取 SQLSTATE，不做任何文本解析。"""
    candidate: BaseException | None = exc
    seen: set[int] = set()
    while candidate is not None and id(candidate) not in seen:
        seen.add(id(candidate))
        state = getattr(candidate, "sqlstate", None)
        if isinstance(state, str) and state:
            return state
        sqlstate_like = getattr(candidate, "pgcode", None)
        if isinstance(sqlstate_like, str) and sqlstate_like:
            return sqlstate_like
        candidate = getattr(candidate, "__cause__", None) or getattr(
            candidate, "__context__", None
        )
    return None


def _class_name(exc: BaseException) -> str:
    chain: list[str] = []
    candidate: BaseException | None = exc
    seen: set[int] = set()
    while candidate is not None and id(candidate) not in seen:
        seen.add(id(candidate))
        chain.append(type(candidate).__name__)
        candidate = getattr(candidate, "__cause__", None) or getattr(
            candidate, "__context__", None
        )
    return "|".join(chain)


def classify_exception(exc: BaseException) -> DatabaseErrorCode:
    """把驱动异常映射成稳定的 typed error code。

    只依据异常类型名与 SQLSTATE 判定，不读取异常正文，因此不会把 DSN、
    SQL 或行数据带入错误码。
    """
    names = _class_name(exc)
    state = _sqlstate(exc)

    # SQLAlchemy 连接池 acquire timeout：必须在其它判定之前，因为它包装的
    # 异常通常是 TimeoutError，与语句超时同形。
    if "PoolTimeout" in names or names.startswith("TimeoutError"):
        return DatabaseErrorCode.DATABASE_POOL_EXHAUSTED

    if state is not None:
        if state == _SQLSTATE_QUERY_CANCELED:
            return DatabaseErrorCode.DATABASE_STATEMENT_TIMEOUT
        if state == _SQLSTATE_LOCK_NOT_AVAILABLE:
            return DatabaseErrorCode.DATABASE_LOCK_TIMEOUT
        if state == _SQLSTATE_IDLE_IN_TRANSACTION_TIMEOUT:
            return DatabaseErrorCode.DATABASE_IDLE_TRANSACTION_TIMEOUT
        if state == _SQLSTATE_DEADLOCK_DETECTED:
            return DatabaseErrorCode.DATABASE_SERIALIZATION_FAILURE
        if state == _SQLSTATE_SERIALIZATION_FAILURE:
            return DatabaseErrorCode.DATABASE_SERIALIZATION_FAILURE
        if state in _SCHEMA_NOT_READY_SQLSTATES:
            return DatabaseErrorCode.DATABASE_SCHEMA_NOT_READY
        if state in _UNAVAILABLE_SQLSTATES:
            return DatabaseErrorCode.DATABASE_UNAVAILABLE
        if state.startswith(_INTEGRITY_CLASS_PREFIX):
            return DatabaseErrorCode.DATABASE_INTEGRITY_VIOLATION
        if state.startswith(_CONNECTION_CLASS_PREFIX):
            return DatabaseErrorCode.DATABASE_UNAVAILABLE

    if "TimeoutError" in names:
        return DatabaseErrorCode.DATABASE_STATEMENT_TIMEOUT
    if "IntegrityError" in names:
        return DatabaseErrorCode.DATABASE_INTEGRITY_VIOLATION
    if (
        "CannotConnectNowError" in names
        or "InvalidPasswordError" in names
        or "InvalidCatalogNameError" in names
        or "InvalidAuthorizationSpecificationError" in names
        or "ConnectionDoesNotExistError" in names
        or "ConnectionFailureError" in names
        or "InterfaceError" in names
        or "ConnectionRefusedError" in names
        or "OSError" in names
    ):
        return DatabaseErrorCode.DATABASE_UNAVAILABLE
    return DatabaseErrorCode.DATABASE_OPERATION_FAILED


def to_persistence_error(exc: BaseException, *, operation: str) -> PersistenceError:
    """把驱动异常收敛成 ``PersistenceError``；已是本类型时原样返回。"""
    if isinstance(exc, PersistenceError):
        return exc
    code = classify_exception(exc)
    return PersistenceError(
        code,
        operation=operation,
        retryable=code in RETRYABLE_DATABASE_ERROR_CODES,
    )


__all__ = [
    "DatabaseErrorCode",
    "RETRYABLE_DATABASE_ERROR_CODES",
    "PersistenceError",
    "classify_exception",
    "to_persistence_error",
]
