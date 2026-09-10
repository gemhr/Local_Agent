#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Alembic async 环境。

DSN 只来自 ``Settings``（环境变量），不落盘、不打印。运行 migration 的进程是
显式 operator 命令（或后续 WP 的 Compose init / Kubernetes Job），**不是**
FastAPI startup。
"""

from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.persistence.models import PersistenceBase  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = PersistenceBase.metadata


def _database_url() -> str:
    """从 Settings 读取 DSN；绝不回退到 alembic.ini 中的明文 URL。"""
    from core.settings import Settings

    settings = Settings.load()
    url = settings.database_url
    if not url or not url.strip():
        raise RuntimeError(
            "LOCAL_AGENT_DATABASE_URL 未配置；Alembic 需要显式 PostgreSQL DSN"
        )
    return url


def run_migrations_offline() -> None:
    """离线模式（只生成 SQL），不建立连接。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    engine = create_async_engine(_database_url(), poolclass=None)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_do_run_migrations)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
