from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text

from core.persistence.database import Database, DatabaseConfig
from core.persistence.errors import PersistenceError
from tests._pg_fixtures import _alembic

pytest_plugins = ("tests._pg_fixtures",)


USER_A = uuid.UUID("10000000-0000-0000-0000-000000000001")
USER_B = uuid.UUID("10000000-0000-0000-0000-000000000002")
DEFAULT_TENANT = "00000000-0000-0000-0000-000000000001"


def _run(pg_url: str, operation):
    async def execute():
        database = Database(DatabaseConfig(url=pg_url, use_null_pool=True))
        try:
            return await operation(database)
        finally:
            await database.dispose()

    return asyncio.run(execute())


def test_0016_to_0017_backfill_constraints_and_ambiguous_fail_closed(
    pg_schema: str,
):
    downgraded = _alembic("downgrade", "0016_stage8_wp10_ticket")
    assert downgraded.returncode == 0, downgraded.stderr

    async def seed_single_owner(database):
        async with database.transaction() as session:
            await session.execute(
                text(
                    "INSERT INTO users "
                    "(id, subject, display_name, principal_kind, service_scopes) "
                    "VALUES (:id, :subject, 'owner', 'HUMAN', '[]'::jsonb)"
                ),
                {"id": USER_A, "subject": str(USER_A)},
            )
            await session.execute(
                text(
                    "INSERT INTO stage8_feature_test_missions "
                    "(mission_id, feature_id, status, version) "
                    "VALUES ('existing-mission', 'feature', 'CREATED', 1)"
                )
            )

    _run(pg_schema, seed_single_owner)
    upgraded = _alembic("upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr

    async def verify_constraints(database):
        async with database.session() as session:
            owner = await session.execute(
                text(
                    "SELECT owner_user_id, tenant_id FROM object_ownership "
                    "WHERE object_type='MISSION' AND object_id='existing-mission'"
                )
            )
            assert owner.one() == (USER_A, DEFAULT_TENANT)

        async with database.transaction() as session:
            await session.execute(
                text(
                    "INSERT INTO stage8_feature_test_missions "
                    "(mission_id, feature_id, status, version) "
                    "VALUES ('new-mission', 'feature', 'CREATED', 1)"
                )
            )
            await session.execute(
                text(
                    "INSERT INTO object_ownership "
                    "(object_type, object_id, owner_user_id, tenant_id) "
                    "VALUES ('MISSION', 'new-mission', :owner, :tenant)"
                ),
                {"owner": USER_A, "tenant": DEFAULT_TENANT},
            )

        async with database.transaction() as session:
            await session.execute(
                text(
                    "INSERT INTO tenants (tenant_id) VALUES ('tenant-b')"
                )
            )
        with pytest.raises(PersistenceError):
            async with database.transaction() as session:
                await session.execute(
                    text(
                        "INSERT INTO object_ownership "
                        "(object_type, object_id, owner_user_id, tenant_id) "
                        "VALUES ('RUN', 'drift-run', :owner, 'tenant-b')"
                    ),
                    {"owner": USER_A},
                )

    _run(pg_schema, verify_constraints)

    downgraded_again = _alembic("downgrade", "0016_stage8_wp10_ticket")
    assert downgraded_again.returncode == 0, downgraded_again.stderr

    async def add_ambiguous_owner(database):
        async with database.transaction() as session:
            await session.execute(
                text(
                    "INSERT INTO users "
                    "(id, subject, display_name, principal_kind, service_scopes) "
                    "VALUES (:id, :subject, 'second', 'HUMAN', '[]'::jsonb)"
                ),
                {"id": USER_B, "subject": str(USER_B)},
            )

    _run(pg_schema, add_ambiguous_owner)
    ambiguous = _alembic("upgrade", "head")
    assert ambiguous.returncode != 0
    assert "ambiguous existing mission ownership backfill" in (
        ambiguous.stderr + ambiguous.stdout
    )

    async def remove_ambiguous_owner(database):
        async with database.transaction() as session:
            await session.execute(
                text("DELETE FROM users WHERE id = :id"), {"id": USER_B}
            )

    _run(pg_schema, remove_ambiguous_owner)
    restored = _alembic("upgrade", "head")
    assert restored.returncode == 0, restored.stderr
