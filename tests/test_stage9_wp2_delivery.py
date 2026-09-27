from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import server
from core.auth import AuthError, AuthorizationAction, AuthorizationService, Principal
from core.persistence.models import TenantRow, UserRow
from core.runtime.client_event_feed import (
    InMemoryClientEventFeed,
    PostgresClientEventFeed,
    project_client_event,
)
from core.runtime.event_journal_store import PostgresRunEventJournal
from core.runtime.event_journal import JournalError
from tests.test_runtime_execute_endpoint import _install_chat_service, _result
from core.runtime.events import (
    ErrorPayload,
    OutputDeltaPayload,
    RunCompletedPayload,
    RuntimeEvent,
    RuntimeEventType,
    ToolApprovalRequestedPayload,
)
from core.runtime.state import RunStatus, StopReason

pytest_plugins = ("tests._pg_fixtures",)


TENANT_A = "00000000-0000-0000-0000-000000000001"
TENANT_B = "tenant-wp2-b"


def _event(sequence: int, event_type: RuntimeEventType, payload, *, run_id="run-wp2"):
    return RuntimeEvent(
        schema_version=RuntimeEvent.CURRENT_SCHEMA_VERSION,
        event_id=f"event-{sequence}-{uuid.uuid4().hex}",
        run_id=run_id,
        trace_id="trace-wp2",
        sequence=sequence,
        event_type=event_type,
        emitted_at=datetime.now(UTC),
        component="test",
        payload=payload,
    )


def _principal(user_id: uuid.UUID, tenant_id: str = TENANT_A) -> Principal:
    now = datetime.now(UTC)
    return Principal(
        user_id=user_id,
        subject=str(user_id),
        roles=frozenset({"USER"}),
        token_id=uuid.uuid4().hex,
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
        tenant_id=tenant_id,
    )


async def _create_user(database, tenant_id: str = TENANT_A) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with database.transaction() as session:
        if await session.get(TenantRow, tenant_id) is None:
            session.add(TenantRow(tenant_id=tenant_id))
            await session.flush()
        session.add(
            UserRow(
                id=user_id,
                subject=str(user_id),
                display_name="wp2",
                principal_kind="HUMAN",
                tenant_id=tenant_id,
            )
        )
    return user_id


def _request(principal: Principal, database, *, feed=None, supervisor=None, headers=None):
    app_state = SimpleNamespace(
        authorization_service=AuthorizationService(database),
        client_event_feed=feed,
        run_execution_supervisor=supervisor,
    )
    return SimpleNamespace(
        state=SimpleNamespace(principal=principal),
        app=SimpleNamespace(state=app_state),
        headers=headers or {},
    )


@pytest.mark.asyncio
async def test_safe_projection_preserves_output_and_derives_terminal_from_run_completed():
    secret = "client-visible-text"
    output = project_client_event(
        _event(1, RuntimeEventType.OUTPUT_DELTA, OutputDeltaPayload(secret))
    )
    error = project_client_event(
        _event(
            2,
            RuntimeEventType.ERROR,
            ErrorPayload("SAFE_CODE", "Safe message.", "provider", True),
        )
    )
    approval = project_client_event(
        _event(
            3,
            RuntimeEventType.TOOL_APPROVAL_REQUESTED,
            ToolApprovalRequestedPayload(
                approval_id="approval-a",
                tool_name="safe-tool",
                invocation_identity_digest="a" * 64,
                arguments_digest="b" * 64,
                invocation_binding_digest="c" * 64,
                risk_level="HIGH",
                risk_facts="must-not-project",
            ),
        )
    )
    terminal = project_client_event(
        _event(
            4,
            RuntimeEventType.RUN_COMPLETED,
            RunCompletedPayload(
                "FAILED", "MODEL_FAILED", safe_error_code="SAFE_CODE"
            ),
        )
    )

    assert output.payload == {"text": secret}
    assert error.event_type == "run.error"
    assert error.payload == {"error_code": "SAFE_CODE", "safe_message": "Safe message."}
    assert approval.payload == {
        "approval_id": "approval-a",
        "tool_name": "safe-tool",
        "risk_level": "HIGH",
    }
    assert terminal.event_type == "run.failed"
    assert terminal.payload == {
        "status": "failed",
        "stop_reason": "MODEL_FAILED",
        "error_code": "SAFE_CODE",
    }
    combined = json.dumps([output.payload, error.payload, approval.payload, terminal.payload])
    assert "must-not-project" not in combined
    assert "arguments_digest" not in combined


@pytest.mark.asyncio
async def test_postgres_feed_cross_instance_replay_and_duplicate_contract(clean_database):
    writer = PostgresClientEventFeed(clean_database)
    reader = PostgresClientEventFeed(clean_database)
    first = _event(1, RuntimeEventType.OUTPUT_DELTA, OutputDeltaPayload("one"))
    second = _event(3, RuntimeEventType.OUTPUT_DELTA, OutputDeltaPayload("three"))
    await writer.append_event(first)
    await writer.append_event(second)
    await asyncio.gather(writer.append_event(second), reader.append_event(second))

    replay = await reader.read_after("run-wp2", 1)
    assert [(item.cursor, item.payload) for item in replay] == [
        (3, {"text": "three"})
    ]

    conflict = RuntimeEvent(
        schema_version=second.schema_version,
        event_id="conflict",
        run_id=second.run_id,
        trace_id=second.trace_id,
        sequence=second.sequence,
        event_type=second.event_type,
        emitted_at=second.emitted_at,
        component=second.component,
        payload=OutputDeltaPayload("different"),
    )
    with pytest.raises(ValueError, match="cursor conflict"):
        await reader.append_event(conflict)

    class FailingProjection:
        failures = 0

        async def append_event_in_transaction(self, session, event):
            self.record_write_failed()
            raise RuntimeError("projection unavailable")

        def record_write_failed(self):
            self.failures += 1

        def record_write_succeeded(self):
            raise AssertionError("failed projection cannot be counted successful")

    failing = FailingProjection()
    journal = PostgresRunEventJournal(clean_database)
    atomic_event = _event(
        1,
        RuntimeEventType.OUTPUT_DELTA,
        OutputDeltaPayload("must-rollback"),
        run_id="run-atomic-failure",
    )
    with pytest.raises(JournalError):
        await journal.append_with_client_projection(atomic_event, failing)
    assert await journal.read_after("run-atomic-failure", 0, 10) == ()
    assert failing.failures == 1


@pytest.mark.asyncio
async def test_subscription_authorizes_before_read_and_supports_independent_cursors(clean_database):
    owner_id = await _create_user(clean_database)
    foreign_id = await _create_user(clean_database)
    cross_tenant_id = await _create_user(clean_database, TENANT_B)
    authz = AuthorizationService(clean_database)
    await authz.bind_new(_principal(owner_id), "RUN", "run-wp2")

    feed = InMemoryClientEventFeed()
    for sequence, text in ((1, "one"), (3, "three"), (7, "seven")):
        await feed.append_event(
            _event(sequence, RuntimeEventType.OUTPUT_DELTA, OutputDeltaPayload(text))
        )
    await feed.append_event(
        _event(
            8,
            RuntimeEventType.RUN_COMPLETED,
            RunCompletedPayload("SUCCEEDED", "COMPLETED"),
        )
    )

    owner_request = _request(_principal(owner_id), clean_database, feed=feed)
    response_a = await server.v1_run_events_endpoint(
        "run-wp2", owner_request, after_cursor=3
    )
    response_b = await server.v1_run_events_endpoint(
        "run-wp2", owner_request, after_cursor=0
    )
    frames_a = [frame async for frame in response_a.body_iterator]
    frames_b = [frame async for frame in response_b.body_iterator]
    assert [frame.split("\n", 1)[0] for frame in frames_a] == ["id: 7", "id: 8"]
    assert [frame.split("\n", 1)[0] for frame in frames_b] == [
        "id: 1", "id: 3", "id: 7", "id: 8"
    ]
    assert frames_a[-1].split("\n")[1] == "event: run.completed"

    for denied in (
        _principal(foreign_id),
        _principal(cross_tenant_id, TENANT_B),
    ):
        with pytest.raises(AuthError) as exc_info:
            await server.v1_run_events_endpoint(
                "run-wp2", _request(denied, clean_database, feed=feed), 0
            )
        assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_structured_invalid_output_is_suppressed_from_durable_sse_and_replay(clean_database):
    owner_id = await _create_user(clean_database)
    principal = _principal(owner_id)
    run_id = uuid.uuid4().hex
    await AuthorizationService(clean_database).bind_new(principal, "RUN", run_id)
    feed = PostgresClientEventFeed(clean_database)
    feed.register_structured_output(run_id, {
        "type": "object", "required": ["answer"],
        "properties": {"answer": {"type": "string"}},
    })
    invalid_body = '{"unexpected":"private-invalid-body"}'
    await feed.append_event(_event(
        1, RuntimeEventType.OUTPUT_DELTA, OutputDeltaPayload(invalid_body), run_id=run_id,
    ))
    assert await PostgresClientEventFeed(clean_database).read_after(run_id, 0) == ()
    completed_event = _event(
        2, RuntimeEventType.RUN_COMPLETED,
        RunCompletedPayload("SUCCEEDED", "COMPLETED"), run_id=run_id,
    )
    await feed.append_event(completed_event)
    await feed.append_event(completed_event)
    replay = await PostgresClientEventFeed(clean_database).read_after(run_id, 0)
    assert len(replay) == 1
    assert replay[0].event_type == "run.completed"
    assert replay[0].payload["status"] == "succeeded"
    assert replay[0].payload["business_output_valid"] is False
    assert replay[0].payload["business_error_code"] == "BUSINESS_OUTPUT_INVALID"
    assert replay[0].payload["output_disposition"] == "REJECTED"
    assert invalid_body not in json.dumps(replay[0].payload)

    response = await server.v1_run_events_endpoint(
        run_id, _request(principal, clean_database, feed=PostgresClientEventFeed(clean_database)), after_cursor=0,
    )
    frames = [frame async for frame in response.body_iterator]
    assert len(frames) == 1
    assert "event: run.completed" in frames[0]
    assert "BUSINESS_OUTPUT_INVALID" in frames[0]
    assert invalid_body not in frames[0]


@pytest.mark.asyncio
async def test_structured_valid_output_is_delivered_with_terminal_but_text_stays_incremental():
    feed = InMemoryClientEventFeed()
    valid_run = uuid.uuid4().hex
    feed.register_structured_output(valid_run, {
        "type": "object", "required": ["answer"],
        "properties": {"answer": {"type": "string"}},
    })
    await feed.append_event(_event(
        1, RuntimeEventType.OUTPUT_DELTA, OutputDeltaPayload('{"answer":'), run_id=valid_run,
    ))
    assert await feed.read_after(valid_run, 0) == ()
    digest = "d" * 64
    await feed.append_event(_event(
        2, RuntimeEventType.TOOL_APPROVAL_REQUESTED,
        ToolApprovalRequestedPayload(
            approval_id="approval-structured", tool_name="read_tool",
            invocation_identity_digest=digest, arguments_digest=digest,
            invocation_binding_digest=digest,
        ), run_id=valid_run,
    ))
    await feed.append_event(_event(
        3, RuntimeEventType.OUTPUT_DELTA, OutputDeltaPayload('"ok"}'), run_id=valid_run,
    ))
    await feed.append_event(_event(
        4, RuntimeEventType.RUN_COMPLETED,
        RunCompletedPayload("SUCCEEDED", "COMPLETED"), run_id=valid_run,
    ))
    replay = await feed.read_after(valid_run, 0)
    assert [(item.cursor, item.event_type) for item in replay] == [
        (2, "approval.required"), (4, "run.completed"),
    ]
    assert replay[-1].payload["business_output_valid"] is True
    assert replay[-1].payload["output_disposition"] == "ACCEPTED"
    assert replay[-1].payload["output"] == '{"answer":"ok"}'

    text_run = uuid.uuid4().hex
    await feed.append_event(_event(
        1, RuntimeEventType.OUTPUT_DELTA, OutputDeltaPayload("incremental"), run_id=text_run,
    ))
    text_replay = await feed.read_after(text_run, 0)
    assert [(item.event_type, item.payload) for item in text_replay] == [
        ("output.delta", {"text": "incremental"}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("valid", [False, True], ids=["invalid", "valid"])
async def test_structured_journal_transaction_rollback_retry_and_delta_dedup(clean_database, monkeypatch, valid):
    owner = await _create_user(clean_database)
    principal = _principal(owner)
    run_id = uuid.uuid4().hex
    await AuthorizationService(clean_database).bind_new(principal, "RUN", run_id)
    feed = PostgresClientEventFeed(clean_database)
    journal = PostgresRunEventJournal(clean_database)
    feed.register_structured_output(run_id, {"type": "object", "required": ["answer"],
                                          "properties": {"answer": {"type": "string"}}})
    body = '{"answer":"valid-body"}' if valid else '{"bad":"private-invalid-body"}'
    delta = _event(1, RuntimeEventType.OUTPUT_DELTA, OutputDeltaPayload(body), run_id=run_id)
    await journal.append_with_client_projection(delta, feed)
    await journal.append_with_client_projection(delta, feed)
    assert len(feed._pending_structured_output[run_id]) == 1
    reader = PostgresClientEventFeed(clean_database)
    assert await reader.read_after(run_id, 0) == ()

    terminal = _event(2, RuntimeEventType.RUN_COMPLETED, RunCompletedPayload("SUCCEEDED", "COMPLETED"), run_id=run_id)
    original = feed.append_event_in_transaction

    async def fail_after_sql(session, event):
        await original(session, event)
        # 真正 Journal Owner 的 transaction：SQL 已执行但尚未 commit。
        assert await reader.read_after(run_id, 0) == ()
        raise RuntimeError("force rollback after projection SQL")

    monkeypatch.setattr(feed, "append_event_in_transaction", fail_after_sql)
    with pytest.raises(JournalError):
        await journal.append_with_client_projection(terminal, feed)
    assert await reader.read_after(run_id, 0) == ()
    assert [item.event_type for item in await journal.read_after(run_id, 0, 10)] == [RuntimeEventType.OUTPUT_DELTA]
    assert run_id in feed._structured_schemas
    assert len(feed._pending_structured_output[run_id]) == 1

    monkeypatch.setattr(feed, "append_event_in_transaction", original)
    await journal.append_with_client_projection(terminal, feed)
    await journal.append_with_client_projection(terminal, feed)
    await journal.append_with_client_projection(delta, feed)
    events = await reader.read_after(run_id, 0)
    assert len(events) == 1
    assert events[0].payload["status"] == "succeeded"
    assert events[0].payload["business_output_valid"] is valid
    if valid:
        assert events[0].payload["output"] == body
    else:
        assert events[0].payload["business_error_code"] == "BUSINESS_OUTPUT_INVALID"
        assert body not in json.dumps(events[0].payload)
    assert run_id not in feed._structured_schemas
    response = await server.v1_run_events_endpoint(run_id, _request(principal, clean_database, feed=reader), 0)
    frames = [frame async for frame in response.body_iterator]
    assert len(frames) == 1
    assert ("valid-body" in frames[0]) is valid
    assert "private-invalid-body" not in frames[0]


def test_resume_cursor_header_query_and_validation_contract():
    def request(header=None):
        return SimpleNamespace(headers={} if header is None else {"Last-Event-ID": header})

    assert server._parse_resume_cursor(request(), None) == 0
    assert server._parse_resume_cursor(request("2"), None) == 2
    assert server._parse_resume_cursor(request(), 2) == 2
    assert server._parse_resume_cursor(request("2"), 2) == 2
    for header, query in (("2", 3), ("bad", None), ("-1", None), (str(2**31), None)):
        with pytest.raises(HTTPException) as exc_info:
            server._parse_resume_cursor(request(header), query)
        assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_v1_subscription_disconnect_does_not_cancel_supervised_producer(
    clean_database, monkeypatch
):
    owner_id = await _create_user(clean_database)
    principal = _principal(owner_id)
    run_id = uuid.uuid4().hex
    feed = InMemoryClientEventFeed()
    await feed.append_event(
        _event(
            1,
            RuntimeEventType.OUTPUT_DELTA,
            OutputDeltaPayload("ready"),
            run_id=run_id,
        )
    )
    producer_started = asyncio.Event()
    producer_release = asyncio.Event()
    producer_finished = asyncio.Event()

    class Service:
        run_registry = SimpleNamespace()

        def stream_coordinated_agent_events(self, *args, _result_out=None, **kwargs):
            async def events():
                producer_started.set()
                try:
                    await producer_release.wait()
                    yield _event(
                        2,
                        RuntimeEventType.OUTPUT_DELTA,
                        OutputDeltaPayload("done"),
                        run_id=run_id,
                    )
                    _result_out.append(
                        _result(
                            RunStatus.SUCCEEDED,
                            StopReason.COMPLETED,
                            run_id=run_id,
                        )
                    )
                finally:
                    producer_finished.set()

            return events()

    supervisor = server.RunExecutionSupervisor()
    request = _request(
        principal, clean_database, feed=feed, supervisor=supervisor
    )
    _install_chat_service(monkeypatch, Service())
    started = await server.v1_chat_endpoint(
        server.V1ChatRequest(agent_id="core_router", query="hello", run_id=run_id),
        request,
    )
    assert started["run_id"] == run_id
    await producer_started.wait()

    subscription = await server.v1_run_events_endpoint(
        run_id, request, after_cursor=0
    )
    assert (await anext(subscription.body_iterator)).startswith("id: 1")
    await subscription.body_iterator.aclose()
    await asyncio.sleep(0)
    assert producer_finished.is_set() is False

    producer_release.set()
    await supervisor.close()
    assert producer_finished.is_set() is True


@pytest.mark.asyncio
async def test_run_execution_supervisor_bounds_active_producers():
    supervisor = server.RunExecutionSupervisor(max_active_runs=1)
    first_release = asyncio.Event()

    async def first_stream():
        await first_release.wait()
        if False:
            yield None

    first_slot = await supervisor.acquire_slot()
    supervisor.spawn("first", first_stream(), reserved_slot=first_slot)
    second_slot_task = asyncio.create_task(supervisor.acquire_slot())
    await asyncio.sleep(0)
    assert second_slot_task.done() is False

    first_release.set()
    assert await asyncio.wait_for(second_slot_task, timeout=1) is True
    supervisor.release_slot(True)
    await supervisor.close()


@pytest.mark.asyncio
async def test_explicit_v1_cancel_uses_authorization_and_durable_control(
    clean_database, monkeypatch
):
    owner_id = await _create_user(clean_database)
    principal = _principal(owner_id)
    run_id = uuid.uuid4().hex
    authz = AuthorizationService(clean_database)
    await authz.bind_new(principal, "RUN", run_id)
    durable_calls = []
    local_calls = []

    class Durable:
        async def request_cancel(self, target, reason):
            durable_calls.append((target, reason))

    class Registry:
        def cancel(self, target, reason):
            local_calls.append((target, reason))
            return True

    monkeypatch.setattr(
        server,
        "_require_runtime_services",
        lambda: SimpleNamespace(durable_run_control=Durable()),
    )
    monkeypatch.setattr(
        server.app.state,
        "chat_service",
        SimpleNamespace(run_registry=Registry()),
        raising=False,
    )
    result = await server.cancel_run_endpoint(
        run_id, _request(principal, clean_database)
    )
    assert result == {"status": "cancelled", "run_id": run_id}
    assert durable_calls == [(run_id, "REQUEST_CANCELLED")]
    assert local_calls and local_calls[0][0] == run_id


@pytest.mark.asyncio
async def test_legacy_disconnect_still_requests_client_disconnected_cancel(monkeypatch):
    cancelled = asyncio.Event()

    class Registry:
        calls = []

        def cancel(self, run_id, reason):
            self.calls.append((run_id, reason))
            cancelled.set()
            return True

    registry = Registry()

    class Service:
        run_registry = registry

        async def stream_coordinated_agent_events(self, *args, **kwargs):
            await cancelled.wait()
            if False:
                yield None

    class Request:
        state = SimpleNamespace(
            principal=SimpleNamespace(authz_domain_id="tenant:legacy")
        )

        async def is_disconnected(self):
            return True

    async def no_bind(*args, **kwargs):
        return None

    _install_chat_service(monkeypatch, Service())
    monkeypatch.setattr(server, "_bind_new_run_and_conversation", no_bind)
    response = await server.chat_endpoint(
        server.ChatRequest(
            agent_id="core_router", query="hello", run_id=uuid.uuid4().hex
        ),
        Request(),
    )
    assert [chunk async for chunk in response.body_iterator] == []
    assert len(registry.calls) == 1
    assert registry.calls[0][1].value == "CLIENT_DISCONNECTED"
