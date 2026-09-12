"""Stage7-WP1 PostgreSQL durable Run control integration evidence."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import time
from types import SimpleNamespace
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from core.persistence.models import RunControlRow, RuntimeEventJournalRow
from core.runtime import CoordinatedRuntimeFactory
from core.runtime import RunRegistry
from core.runtime.run_control import (
    DurableRunControlService,
    OwnershipLost,
    RunControlConflict,
)
from core.runtime.event_journal_store import PostgresRunEventJournal
from core.runtime.events import RunCompletedPayload, RuntimeEvent, RuntimeEventType
from tests._runtime_assembly_fixtures import FakeRouter, make_services
import server


def _terminal_event(run_id: str, *, sequence: int = 1) -> RuntimeEvent:
    return RuntimeEvent(
        schema_version=2,
        event_id=uuid.uuid4().hex,
        run_id=run_id,
        trace_id=uuid.uuid4().hex,
        sequence=sequence,
        event_type=RuntimeEventType.RUN_COMPLETED,
        emitted_at=datetime.now(UTC),
        component="test",
        payload=RunCompletedPayload(status="SUCCEEDED", stop_reason="COMPLETED"),
    )


@pytest.mark.asyncio
async def test_competing_claim_and_stale_release_do_not_replace_owner(clean_database):
    run_id = uuid.uuid4().hex
    first = DurableRunControlService(clean_database, lease_seconds=2)
    second = DurableRunControlService(clean_database, lease_seconds=2)
    results = await asyncio.gather(
        first.claim(run_id, "instance-a"),
        second.claim(run_id, "instance-b"),
        return_exceptions=True,
    )
    leases = [item for item in results if not isinstance(item, Exception)]
    assert len(leases) == 1
    winner = leases[0]
    loser = "instance-b" if winner.owner_id == "instance-a" else "instance-a"
    with pytest.raises(RunControlConflict):
        await (second if loser == "instance-b" else first).claim(run_id, loser)
    assert await first.release(winner) is True


@pytest.mark.asyncio
async def test_takeover_increments_fence_and_stale_renew_fails(clean_database):
    run_id = uuid.uuid4().hex
    first = DurableRunControlService(clean_database, lease_seconds=1)
    second = DurableRunControlService(clean_database, lease_seconds=1)
    old = await first.claim(run_id, "instance-a")
    await asyncio.sleep(1.1)
    current = await second.claim(run_id, "instance-b")
    assert current.fencing_token > old.fencing_token
    with pytest.raises(OwnershipLost):
        await first.renew(old)
    assert await first.release(old) is False
    with pytest.raises(OwnershipLost):
        await first.finalize_terminal(
            old, _terminal_event(run_id), PostgresRunEventJournal(clean_database)
        )
    assert await second.release(current) is True


@pytest.mark.asyncio
async def test_cancel_is_durable_and_idempotent_across_service_instances(clean_database):
    run_id = uuid.uuid4().hex
    first = DurableRunControlService(clean_database)
    second = DurableRunControlService(clean_database)
    command_id = uuid.uuid4().hex
    first_intent = await first.request_cancel(run_id, "USER_CANCELLED", command_id=command_id)
    second_intent = await second.request_cancel(run_id, "USER_CANCELLED", command_id=command_id)
    assert second_intent == first_intent
    assert await second.request_cancel(run_id, "USER_CANCELLED") == first_intent
    assert await second.cancel_intent(run_id) == first_intent
    with pytest.raises(RunControlConflict):
        await second.request_cancel(run_id, "CLIENT_DISCONNECTED", command_id=command_id)


@pytest.mark.asyncio
async def test_terminal_append_and_control_close_share_one_transaction(clean_database):
    run_id = uuid.uuid4().hex
    control = DurableRunControlService(clean_database)
    lease = await control.claim(run_id, "instance-a")
    event = _terminal_event(run_id)
    await control.finalize_terminal(
        lease, event, PostgresRunEventJournal(clean_database)
    )
    async with clean_database.session() as session:
        row = (await session.execute(select(RunControlRow).where(RunControlRow.run_id == run_id))).scalar_one()
        terminal = (await session.execute(select(RuntimeEventJournalRow).where(RuntimeEventJournalRow.run_id == run_id))).scalar_one()
    assert row.state == "CLOSED"
    assert row.terminal_sequence == terminal.sequence == 1
    with pytest.raises(OwnershipLost):
        await control.assert_current(lease)


@pytest.mark.asyncio
async def test_released_or_cancel_first_run_can_be_claimed(clean_database):
    control = DurableRunControlService(clean_database)
    released = await control.claim(uuid.uuid4().hex, "instance-a")
    assert await control.release(released) is True
    async with clean_database.session() as session:
        released_row = (await session.execute(
            select(RunControlRow).where(RunControlRow.run_id == released.run_id)
        )).scalar_one()
    assert released_row.state == "ACTIVE"
    assert released_row.terminal_sequence is None
    reclaimed = await control.claim(released.run_id, "instance-b")
    assert reclaimed.fencing_token > released.fencing_token

    cancel_first_run = uuid.uuid4().hex
    await control.request_cancel(cancel_first_run, "REQUEST_CANCELLED")
    claimed = await control.claim(cancel_first_run, "instance-a")
    assert claimed.fencing_token == 1


@pytest.mark.asyncio
async def test_terminal_append_failure_rolls_back_control_close(clean_database):
    run_id = uuid.uuid4().hex
    control = DurableRunControlService(clean_database)
    lease = await control.claim(run_id, "instance-a")
    journal = PostgresRunEventJournal(clean_database)

    class AppendThenFail:
        async def append_in_transaction(self, session, event):
            await journal.append_in_transaction(session, event)
            raise RuntimeError("forced append failure")

    with pytest.raises(RuntimeError, match="forced append failure"):
        await control.finalize_terminal(lease, _terminal_event(run_id), AppendThenFail())

    async with clean_database.session() as session:
        row = (await session.execute(select(RunControlRow).where(RunControlRow.run_id == run_id))).scalar_one()
        event_count = (await session.execute(select(func.count()).select_from(RuntimeEventJournalRow).where(RuntimeEventJournalRow.run_id == run_id))).scalar_one()
    assert row.state == "ACTIVE"
    assert row.terminal_sequence is None
    assert event_count == 0


@pytest.mark.asyncio
async def test_control_close_failure_rolls_back_terminal_append(clean_database, monkeypatch):
    run_id = uuid.uuid4().hex
    control = DurableRunControlService(clean_database)
    lease = await control.claim(run_id, "instance-a")

    async def fail_close(self, session, row, sequence):
        raise RuntimeError("forced control close failure")

    monkeypatch.setattr(DurableRunControlService, "_close_terminal_locked", fail_close)
    with pytest.raises(RuntimeError, match="forced control close failure"):
        await control.finalize_terminal(
            lease, _terminal_event(run_id), PostgresRunEventJournal(clean_database)
        )

    async with clean_database.session() as session:
        row = (await session.execute(select(RunControlRow).where(RunControlRow.run_id == run_id))).scalar_one()
        event_count = (await session.execute(select(func.count()).select_from(RuntimeEventJournalRow).where(RuntimeEventJournalRow.run_id == run_id))).scalar_one()
    assert row.state == "ACTIVE"
    assert row.terminal_sequence is None
    assert event_count == 0


@pytest.mark.asyncio
async def test_production_factory_path_renews_and_atomically_finalizes(clean_database):
    class SlowRouter(FakeRouter):
        def complete_single_agent(self, agent_id: str, query: str, **kwargs) -> str:
            time.sleep(1.4)
            return "assembled-output"

    router = SlowRouter()
    control = DurableRunControlService(clean_database, lease_seconds=1)
    services = replace(
        make_services(snapshot_enabled=False),
        event_journal=PostgresRunEventJournal(clean_database),
        durable_run_control=control,
        run_control_owner_id="instance-a",
    )
    scope = await CoordinatedRuntimeFactory(
        router, services, event_channel_capacity=64
    ).create_static_run_scope("core_router", "question")

    executing = asyncio.create_task(scope.execute())
    await asyncio.sleep(1.1)
    with pytest.raises(RunControlConflict):
        await DurableRunControlService(clean_database, lease_seconds=1).claim(
            scope.run_id, "instance-b"
        )
    result = await executing
    await scope.close()

    assert result.status.value == "SUCCEEDED"
    async with clean_database.session() as session:
        row = (await session.execute(select(RunControlRow).where(RunControlRow.run_id == scope.run_id))).scalar_one()
        terminal = (await session.execute(select(RuntimeEventJournalRow).where(
            RuntimeEventJournalRow.run_id == scope.run_id,
            RuntimeEventJournalRow.event_type == RuntimeEventType.RUN_COMPLETED.value,
        ))).scalar_one()
    assert row.state == "CLOSED"
    assert row.terminal_sequence == terminal.sequence


@pytest.mark.asyncio
async def test_production_scope_observes_cross_instance_cancel(clean_database):
    class SlowRouter(FakeRouter):
        def complete_single_agent(self, agent_id: str, query: str, **kwargs) -> str:
            time.sleep(0.8)
            return "assembled-output"

    control = DurableRunControlService(clean_database, lease_seconds=2)
    services = replace(
        make_services(snapshot_enabled=False),
        event_journal=PostgresRunEventJournal(clean_database),
        durable_run_control=control,
        run_control_owner_id="instance-a",
    )
    scope = await CoordinatedRuntimeFactory(
        SlowRouter(), services, event_channel_capacity=64
    ).create_static_run_scope("core_router", "question")

    executing = asyncio.create_task(scope.execute())
    await asyncio.sleep(0.1)
    await DurableRunControlService(clean_database).request_cancel(
        scope.run_id, "REQUEST_CANCELLED"
    )
    result = await executing
    await scope.close()

    assert result.status.value == "CANCELLED"
    assert scope.cancellation_source.token.reason == "REQUEST_CANCELLED"


@pytest.mark.asyncio
async def test_cancel_endpoint_writes_durable_intent_before_registry_miss(
    clean_database, monkeypatch
):
    control = DurableRunControlService(clean_database)
    registry = RunRegistry()
    service = SimpleNamespace(
        durable_run_control=control,
        run_registry=registry,
    )
    monkeypatch.setattr(server, "chat_service", service)

    async def allow(_request, _run_id):
        return None

    monkeypatch.setattr(server, "_require_run_owner", allow)
    run_id = uuid.uuid4().hex
    first = await server.cancel_run_endpoint(run_id, object())
    second = await server.cancel_run_endpoint(run_id, object())

    assert first == {"status": "cancelled", "run_id": run_id}
    assert second == {"status": "cancelled", "run_id": run_id}
    assert (await control.cancel_intent(run_id)).reason == "REQUEST_CANCELLED"


@pytest.mark.asyncio
async def test_cancel_endpoint_does_not_fallback_to_registry_on_db_failure(monkeypatch):
    class BrokenControl:
        async def request_cancel(self, run_id, reason):
            raise RuntimeError("database unavailable")

    class RecordingRegistry:
        called = False

        def cancel(self, run_id, reason):
            self.called = True
            return True

    registry = RecordingRegistry()
    monkeypatch.setattr(
        server,
        "chat_service",
        SimpleNamespace(durable_run_control=BrokenControl(), run_registry=registry),
    )

    async def allow(_request, _run_id):
        return None

    monkeypatch.setattr(server, "_require_run_owner", allow)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await server.cancel_run_endpoint(uuid.uuid4().hex, object())
    assert registry.called is False
