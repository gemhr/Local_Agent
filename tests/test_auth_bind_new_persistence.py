from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid

import pytest

from core.auth import AuthorizationService, Principal
from core.persistence.errors import DatabaseErrorCode, PersistenceError


class _Session:
    async def get(self, _model, _identity):
        return None

    def add(self, _row) -> None:
        return None

    async def flush(self) -> None:
        return None


class _Transaction:
    def __init__(self, error: PersistenceError) -> None:
        self._error = error
        self.session = _Session()

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        if exc_type is None:
            raise self._error
        return False


class _Database:
    def __init__(self, error: PersistenceError) -> None:
        self.error = error

    def transaction(self):
        return _Transaction(self.error)


class _ProbeAuthorizationService(AuthorizationService):
    def __init__(self, database) -> None:
        super().__init__(database)
        self.existing_binding_reads = 0

    async def _require_existing_binding(self, principal, object_type, object_id):
        self.existing_binding_reads += 1


def _principal() -> Principal:
    user_id = uuid.uuid4()
    now = datetime.now(UTC)
    return Principal(
        user_id,
        str(user_id),
        frozenset({"USER"}),
        "bind-new-test",
        now,
        now + timedelta(minutes=5),
        tenant_id="tenant-a",
    )


@pytest.mark.asyncio
async def test_bind_new_maps_concurrent_integrity_error_but_not_other_persistence_errors():
    principal = _principal()
    integrity = PersistenceError(
        DatabaseErrorCode.DATABASE_INTEGRITY_VIOLATION,
        operation="transaction",
    )
    service = _ProbeAuthorizationService(_Database(integrity))

    await service.bind_new(principal, "CONVERSATION", "same-object")
    assert service.existing_binding_reads == 1

    operation_failed = PersistenceError(
        DatabaseErrorCode.DATABASE_OPERATION_FAILED,
        operation="transaction",
    )
    service = _ProbeAuthorizationService(_Database(operation_failed))
    with pytest.raises(PersistenceError) as captured:
        await service.bind_new(principal, "CONVERSATION", "same-object")
    assert captured.value.error_code is DatabaseErrorCode.DATABASE_OPERATION_FAILED
    assert service.existing_binding_reads == 0
