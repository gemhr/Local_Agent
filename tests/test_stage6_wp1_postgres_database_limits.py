from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from core.persistence.database import Database, DatabaseConfig
from core.persistence.errors import DatabaseErrorCode, PersistenceError

pytestmark = pytest.mark.asyncio


async def test_real_pool_acquire_timeout_is_typed_and_pool_recovers(pg_url):
    database = Database(DatabaseConfig(url=pg_url, pool_size=1, max_overflow=0, pool_timeout_seconds=0.1))
    try:
        async with database.engine.connect() as first:
            waiter = asyncio.create_task(_acquire(database))
            with pytest.raises(PersistenceError) as exc_info:
                await waiter
            assert exc_info.value.error_code is DatabaseErrorCode.DATABASE_POOL_EXHAUSTED
            assert "postgres" not in str(exc_info.value).lower()
        async with database.transaction() as session:
            assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
    finally:
        await database.dispose()


async def _acquire(database: Database):
    async with database.transaction() as session:
        await session.execute(text("SELECT 1"))


async def test_real_statement_timeout_rolls_back_and_connection_reusable(pg_url):
    database = Database(DatabaseConfig(url=pg_url, statement_timeout_ms=50, lock_timeout_ms=50))
    try:
        with pytest.raises(PersistenceError) as exc_info:
            async with database.transaction() as session:
                await session.execute(text("SELECT pg_sleep(0.2)"))
        assert exc_info.value.error_code is DatabaseErrorCode.DATABASE_STATEMENT_TIMEOUT
        async with database.transaction() as session:
            assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
    finally:
        await database.dispose()
