#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Shared PostgreSQL integration-test fixtures for Stage6-WP1.

These tests require a **real** PostgreSQL server. Mock/fake drivers are not
accepted as WP1 evidence, so the fixtures fail loudly when PostgreSQL is not
reachable instead of silently skipping.

Configuration (never committed with real credentials)::

    LOCAL_AGENT_TEST_DATABASE_URL  full async DSN for the test database
    LOCAL_AGENT_TEST_PG_HOST       default 127.0.0.1
    LOCAL_AGENT_TEST_PG_PORT       default 5433
    LOCAL_AGENT_TEST_PG_USER       default postgres
    LOCAL_AGENT_TEST_PG_PASSWORD   no default
    LOCAL_AGENT_TEST_PG_DATABASE   default localagent_test

The test database is always distinct from any runtime database; the fixtures
never touch a database whose name does not end in ``_test``.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.persistence.database import Database, DatabaseConfig  # noqa: E402
from core.persistence.models import CANONICAL_TABLES  # noqa: E402

_TEST_DB_SUFFIX = "_test"


def _require_test_database(name: str) -> str:
    if not name.endswith(_TEST_DB_SUFFIX):
        raise RuntimeError(
            "refusing to run integration tests against a non-test database"
        )
    return name


def test_database_url() -> str:
    """Resolve the test DSN, refusing anything that is not a test database."""
    explicit = os.getenv("LOCAL_AGENT_TEST_DATABASE_URL", "").strip()
    if explicit:
        database = explicit.rsplit("/", 1)[-1].split("?", 1)[0]
        _require_test_database(database)
        return explicit
    host = os.getenv("LOCAL_AGENT_TEST_PG_HOST", "127.0.0.1")
    port = os.getenv("LOCAL_AGENT_TEST_PG_PORT", "5433")
    user = os.getenv("LOCAL_AGENT_TEST_PG_USER", "postgres")
    password = os.getenv("LOCAL_AGENT_TEST_PG_PASSWORD", "")
    database = _require_test_database(
        os.getenv("LOCAL_AGENT_TEST_PG_DATABASE", "localagent_test")
    )
    credentials = f"{user}:{password}" if password else user
    return (
        f"postgresql+asyncpg://{credentials}@{host}:{port}/{database}"
    )


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["LOCAL_AGENT_DATABASE_URL"] = test_database_url()
    env["LOCAL_AGENT_ENVIRONMENT_PROFILE"] = "TEST"
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


async def _verify_and_reset(database: Database) -> None:
    """Verify reachability and drop canonical tables inside ONE event loop."""
    await database.verify_reachable()
    await _drop_all(database)


async def _drop_all(database: Database) -> None:
    from sqlalchemy import text

    try:
        async with database.engine.begin() as connection:
            await connection.execute(
                text(
                    "DROP TABLE IF EXISTS "
                    + ", ".join(f'"{name}"' for name in CANONICAL_TABLES)
                    + " CASCADE"
                )
            )
            await connection.execute(
                text('DROP TABLE IF EXISTS "alembic_version"')
            )
    finally:
        # 必须清空 pool：这些连接绑定在 asyncio.run 的临时 loop 上，
        # 复用它们会让后续测试拿到已死 loop 的连接。
        await database.engine.dispose()


def _truncate_all(database: Database) -> None:
    async def _truncate() -> None:
        from sqlalchemy import text

        try:
            async with database.engine.begin() as connection:
                await connection.execute(
                    text(
                        "TRUNCATE TABLE "
                        + ", ".join(f'"{name}"' for name in CANONICAL_TABLES)
                        + " RESTART IDENTITY CASCADE"
                    )
                )
        finally:
            await database.engine.dispose()

    asyncio.run(_truncate())


@pytest.fixture(scope="session")
def pg_url() -> str:
    return test_database_url()


@pytest.fixture(scope="session")
def pg_schema(pg_url: str) -> str:
    """Session bootstrap: verify reachability, start from empty, migrate to head.

    Uses its own short-lived engine that is disposed before returning, because
    an ``AsyncEngine`` (and its asyncpg connections) must not be shared across
    event loops. Each test builds its own engine.
    """
    bootstrap = Database(DatabaseConfig(url=pg_url, use_null_pool=True))
    try:
        asyncio.run(_verify_and_reset(bootstrap))
    except Exception as exc:  # pragma: no cover - environment gate
        pytest.fail(
            "PostgreSQL is not reachable for integration tests: "
            f"{type(exc).__name__}"
        )

    result = _alembic("upgrade", "head")
    if result.returncode != 0:
        pytest.fail(
            "alembic upgrade head failed: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return pg_url


@pytest.fixture()
def pg_database(pg_schema: str) -> Database:
    """Function-scoped Database: each test gets its own engine and event loop."""
    # NullPool：每个 async 测试拥有独立 event loop，池化连接不能跨 loop 复用。
    database = Database(
        DatabaseConfig(url=pg_schema, use_null_pool=True)
    )
    try:
        yield database
    finally:
        asyncio.run(database.dispose())


@pytest.fixture()
def clean_database(pg_database: Database) -> Database:
    """Function-scoped isolation: truncate all canonical tables first."""
    _truncate_all(pg_database)
    return pg_database


@pytest.fixture()
def pg_dsn_env(monkeypatch: pytest.MonkeyPatch, pg_url: str) -> str:
    """Point ``Settings.load()`` at the test database."""
    monkeypatch.setenv("LOCAL_AGENT_DATABASE_URL", pg_url)
    return pg_url
