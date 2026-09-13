#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LocalAgent PostgreSQL 持久化基础（Stage6-WP1）。

Owner 摘要：

* ``Database``            —— AsyncEngine / Connection Pool / Session Factory；
* ``PersistenceError``    —— 唯一把驱动异常翻译为 typed failure 的边界；
* ``models``              —— SQLAlchemy 2.x 声明式 Model Owner；
* ``SyncPersistenceBridge``—— worker 线程到 async 持久化的有界桥；
* ``readiness``           —— 只读 schema readiness preflight。

事务 Owner 是 Application Service（``async with database.transaction():``）；
Repository 只接收调用方提供的 ``AsyncSession``，不 commit / rollback / 建 session。
"""

from core.persistence.database import (
    ASYNC_DRIVER_NAME,
    Database,
    DatabaseConfig,
)
from core.persistence.errors import (
    RETRYABLE_DATABASE_ERROR_CODES,
    DatabaseErrorCode,
    PersistenceError,
    classify_exception,
    to_persistence_error,
)
from core.persistence.models import (
    CANONICAL_TABLES,
    TABLES_DEFERRED_TO_LATER_WORK_PACKAGES,
    PersistenceBase,
    UserRow,
    RoleRow,
    UserRoleRow,
    ObjectOwnershipRow,
    DurableApprovalRow,
    DurableToolExecutionClaimRow,
)
from core.persistence.readiness import (
    SchemaReadiness,
    alembic_head_revision,
    assert_schema_ready,
    check_schema_readiness,
)
from core.persistence.sync_bridge import SyncPersistenceBridge
from core.persistence.memory import (
    PostgresAdvancedMemoryStore,
    PostgresAdvancedMemoryStoreBridge,
    PostgresMemoryManager,
    PostgresMemoryManagerBridge,
    PostgresProjectSemanticMemoryStore,
    PostgresProjectSemanticMemoryStoreBridge,
)

__all__ = [
    "ASYNC_DRIVER_NAME",
    "CANONICAL_TABLES",
    "RETRYABLE_DATABASE_ERROR_CODES",
    "TABLES_DEFERRED_TO_LATER_WORK_PACKAGES",
    "Database",
    "DatabaseConfig",
    "DatabaseErrorCode",
    "PersistenceBase",
    "UserRow",
    "RoleRow",
    "UserRoleRow",
    "ObjectOwnershipRow",
    "DurableApprovalRow",
    "DurableToolExecutionClaimRow",
    "PersistenceError",
    "SchemaReadiness",
    "SyncPersistenceBridge",
    "PostgresAdvancedMemoryStore",
    "PostgresAdvancedMemoryStoreBridge",
    "PostgresMemoryManager",
    "PostgresMemoryManagerBridge",
    "PostgresProjectSemanticMemoryStore",
    "PostgresProjectSemanticMemoryStoreBridge",
    "alembic_head_revision",
    "assert_schema_ready",
    "check_schema_readiness",
    "classify_exception",
    "to_persistence_error",
]
