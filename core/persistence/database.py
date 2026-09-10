#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PostgreSQL AsyncEngine / Connection Pool / Session Factory 的 Application Owner。

Owner 边界（Stage6-WP0 冻结）：

* ``Database`` 拥有 ``AsyncEngine``、连接池与 ``async_sessionmaker``；
* 生命周期由 application lifecycle（``server.py::lifespan``）创建与
  ``await dispose()`` 关闭，每个进程各自持有自己的 pool；
* ``Database`` **不** commit / rollback，也**不**替业务决定事务边界；
  事务 Owner 是 Application Service（``async with session.begin():``）；
* 不在 import 时连接数据库，也不做任何隐式 DDL / migration。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from core.persistence.errors import (
    DatabaseErrorCode,
    PersistenceError,
    to_persistence_error,
)


# 明确的驱动名；禁止静默接受其它 dialect，否则 async 边界会被悄悄破坏。
ASYNC_DRIVER_NAME = "postgresql+asyncpg"

# ``set_config`` 的第三个参数为 true 表示 transaction-local，等价于
# ``SET LOCAL``，但允许绑定参数，因此无需拼接 SQL。
_SET_LOCAL_CONFIG_SQL = text("SELECT set_config(:name, :value, true)")

_TIMEOUT_CONFIG_NAMES = (
    ("statement_timeout", "db_statement_timeout_ms"),
    ("lock_timeout", "db_lock_timeout_ms"),
    (
        "idle_in_transaction_session_timeout",
        "db_idle_in_transaction_timeout_ms",
    ),
)


@dataclass(frozen=True)
class DatabaseConfig:
    """不可变 PostgreSQL 连接配置；``url`` 是 secret，绝不进入 repr。"""

    url: str
    pool_size: int = 5
    max_overflow: int = 5
    pool_timeout_seconds: float = 5.0
    pool_recycle_seconds: int = 1800
    connect_timeout_seconds: float = 5.0
    statement_timeout_ms: int = 15000
    lock_timeout_ms: int = 5000
    idle_in_transaction_timeout_ms: int = 10000
    # 默认使用真实连接池（生产语义）。置为 True 时改用 NullPool：每个
    # session 独占一条连接并在结束时关闭，适用于「一个进程内有多个彼此
    # 独立的 event loop」的测试夹具，避免池化连接跨 loop 复用。
    use_null_pool: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url.strip():
            raise ValueError("url 必须是非空字符串")
        if not self.url.startswith(ASYNC_DRIVER_NAME):
            raise ValueError(
                f"Database URL 必须使用 {ASYNC_DRIVER_NAME} driver"
            )
        for name, value in (
            ("pool_size", self.pool_size),
            ("max_overflow", self.max_overflow),
            ("pool_recycle_seconds", self.pool_recycle_seconds),
            ("statement_timeout_ms", self.statement_timeout_ms),
            ("lock_timeout_ms", self.lock_timeout_ms),
            ("idle_in_transaction_timeout_ms", self.idle_in_transaction_timeout_ms),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须是非负整数")
        if self.pool_size < 1:
            raise ValueError("pool_size 必须至少为 1")
        for name, value in (
            ("pool_timeout_seconds", self.pool_timeout_seconds),
            ("connect_timeout_seconds", self.connect_timeout_seconds),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} 必须是数字")
            if not (value > 0):
                raise ValueError(f"{name} 必须为正数")

    def __repr__(self) -> str:
        # 绝不输出 DSN（含凭据）或 host。
        return (
            "DatabaseConfig("
            f"driver={ASYNC_DRIVER_NAME!r}, "
            f"pool_size={self.pool_size}, max_overflow={self.max_overflow}, "
            f"pool_timeout_seconds={self.pool_timeout_seconds}, "
            f"pool_recycle_seconds={self.pool_recycle_seconds}, "
            f"connect_timeout_seconds={self.connect_timeout_seconds}, "
            f"statement_timeout_ms={self.statement_timeout_ms}, "
            f"lock_timeout_ms={self.lock_timeout_ms}, "
            "idle_in_transaction_timeout_ms="
            f"{self.idle_in_transaction_timeout_ms})"
        )

    @classmethod
    def from_settings(cls, settings: Any) -> "DatabaseConfig":
        """从 Settings 构造；只读取显式列出的字段，不做二次合并。"""
        return cls(
            url=settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout_seconds=settings.db_pool_timeout_seconds,
            pool_recycle_seconds=settings.db_pool_recycle_seconds,
            connect_timeout_seconds=settings.db_connect_timeout_seconds,
            statement_timeout_ms=settings.db_statement_timeout_ms,
            lock_timeout_ms=settings.db_lock_timeout_ms,
            idle_in_transaction_timeout_ms=(
                settings.db_idle_in_transaction_timeout_ms
            ),
        )


class Database:
    """进程级 AsyncEngine / Session Factory Owner。"""

    def __init__(self, config: DatabaseConfig) -> None:
        if not isinstance(config, DatabaseConfig):
            raise TypeError("config 必须是 DatabaseConfig")
        self._config = config
        self._closed = False
        self._engine: AsyncEngine = self._create_engine(config)
        self._session_factory: async_sessionmaker[AsyncSession] = (
            async_sessionmaker(
                bind=self._engine,
                expire_on_commit=False,
                autoflush=False,
                class_=AsyncSession,
            )
        )

    @staticmethod
    def _create_engine(config: DatabaseConfig) -> AsyncEngine:
        server_settings = {
            "application_name": "localagent",
            "statement_timeout": str(config.statement_timeout_ms),
            "lock_timeout": str(config.lock_timeout_ms),
            "idle_in_transaction_session_timeout": str(
                config.idle_in_transaction_timeout_ms
            ),
        }
        pool_kwargs: dict[str, Any] = (
            {"poolclass": NullPool}
            if config.use_null_pool
            else {
                "pool_size": config.pool_size,
                "max_overflow": config.max_overflow,
                "pool_timeout": config.pool_timeout_seconds,
                "pool_recycle": config.pool_recycle_seconds,
                "pool_pre_ping": True,
            }
        )
        return create_async_engine(
            config.url,
            future=True,
            connect_args={
                "timeout": config.connect_timeout_seconds,
                "server_settings": server_settings,
            },
            **pool_kwargs,
        )

    @property
    def config(self) -> DatabaseConfig:
        return self._config

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory

    @property
    def closed(self) -> bool:
        return self._closed

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """产出一个独立 Session；每个并发操作必须各自获取。

        本方法只负责 session 生命周期（关闭）；事务仍由调用方
        （Application Service）用 ``async with session.begin():`` 拥有。
        """
        if self._closed:
            raise PersistenceError(
                DatabaseErrorCode.DATABASE_CLOSED, operation="session"
            )
        session = self._session_factory()
        try:
            yield session
        except PersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise to_persistence_error(exc, operation="session") from None
        finally:
            try:
                await session.close()
            except Exception:
                # close 是幂等清理边界，绝不覆盖调用方的业务异常。
                pass

    @asynccontextmanager
    async def transaction(
        self,
        *,
        statement_timeout_ms: int | None = None,
        lock_timeout_ms: int | None = None,
    ) -> AsyncIterator[AsyncSession]:
        """Application Service 的 Unit of Work 入口。

        可选把 remaining deadline 收紧为更短的 statement/lock timeout，保证
        ``DB timeout < remaining request/runtime deadline``。未显式给出时使用
        连接级 Settings 值，不伪造完整 deadline coverage。
        """
        async with self.session() as session:
            try:
                async with session.begin():
                    if statement_timeout_ms is not None:
                        await self._set_local_timeout(
                            session, "statement_timeout", statement_timeout_ms
                        )
                    if lock_timeout_ms is not None:
                        await self._set_local_timeout(
                            session, "lock_timeout", lock_timeout_ms
                        )
                    yield session
            except PersistenceError:
                raise
            except SQLAlchemyError as exc:
                # 只翻译驱动/池层失败；业务异常必须原样穿透，否则
                # JournalError / SnapshotStoreError 会被吞掉。
                raise to_persistence_error(exc, operation="transaction") from None

    @staticmethod
    async def _set_local_timeout(
        session: AsyncSession, name: str, value_ms: int
    ) -> None:
        if isinstance(value_ms, bool) or not isinstance(value_ms, int):
            raise ValueError("timeout 必须是整数毫秒")
        if value_ms < 1:
            raise ValueError("timeout 必须至少为 1ms")
        await session.execute(
            _SET_LOCAL_CONFIG_SQL,
            {"name": name, "value": str(value_ms)},
        )

    def deadline_statement_timeout_ms(
        self, remaining_seconds: float | None, *, margin_ms: int = 50
    ) -> int | None:
        """把剩余预算收敛为不超过配置值的 statement timeout。

        返回 ``None`` 表示没有可用预算信息，调用方应沿用连接级 Settings 值。
        """
        if remaining_seconds is None:
            return None
        if not isinstance(remaining_seconds, (int, float)) or isinstance(
            remaining_seconds, bool
        ):
            raise ValueError("remaining_seconds 必须是数字或 None")
        if remaining_seconds <= 0:
            return 1
        budget_ms = int(remaining_seconds * 1000) - margin_ms
        if budget_ms < 1:
            return 1
        return min(budget_ms, self._config.statement_timeout_ms)

    async def verify_reachable(self) -> None:
        """显式可达性探测；失败映射为 typed DATABASE_UNAVAILABLE。"""
        try:
            async with self._engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception as exc:
            raise to_persistence_error(
                exc, operation="verify_reachable"
            ) from None

    async def server_version(self) -> str:
        """返回 PostgreSQL server_version 文本，仅用于诊断/证据。"""
        try:
            async with self._engine.connect() as connection:
                result = await connection.execute(text("SHOW server_version"))
                value = result.scalar_one()
        except Exception as exc:
            raise to_persistence_error(exc, operation="server_version") from None
        return str(value)

    async def close(self) -> None:
        """``RuntimeInitializationStack`` 的统一关闭入口（等价于 dispose）。"""
        await self.dispose()

    async def dispose(self) -> None:
        """Shutdown Owner：关闭连接池；幂等。"""
        if self._closed:
            return
        self._closed = True
        try:
            await self._engine.dispose()
        except Exception:
            # dispose 是关闭边界，不向外抛驱动细节。
            return

    async def pool_snapshot(self) -> dict[str, int]:
        """只读 pool 统计，用于 readiness/诊断与测试。"""
        pool = self._engine.pool
        status: dict[str, int] = {}
        for label, attribute in (
            ("checked_out", "checkedout"),
            ("checked_in", "checkedin"),
            ("overflow", "overflow"),
            ("size", "size"),
        ):
            getter = getattr(pool, attribute, None)
            if callable(getter):
                try:
                    status[label] = int(getter())
                except Exception:
                    continue
        return status

    async def wait_closed(self, timeout_seconds: float = 1.0) -> bool:
        """有界等待 in-flight 连接归还；返回是否在预算内收敛。"""
        deadline = asyncio.get_running_loop().time() + max(0.0, timeout_seconds)
        while True:
            snapshot = await self.pool_snapshot()
            if snapshot.get("checked_out", 0) == 0:
                return True
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.01)


__all__ = ["ASYNC_DRIVER_NAME", "Database", "DatabaseConfig"]
