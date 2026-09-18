"""Client-safe durable delivery projection for resumable Run streams."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from core.persistence.database import Database
from core.persistence.models import ClientDeliveryEventRow
from core.runtime.events import (
    CancellationPayload, ErrorPayload, OutputDeltaPayload, RunCompletedPayload,
    RuntimeEvent, RuntimeEventType, ToolApprovalRequestedPayload,
)


CLIENT_EVENT_SCHEMA_VERSION = 1
CLIENT_FEED_POLL_INTERVAL_SECONDS = 0.25


@dataclass(frozen=True, slots=True)
class ClientDeliveryEvent:
    run_id: str
    cursor: int
    event_type: str
    payload: dict[str, object]
    created_at: datetime
    event_id: str | None = None
    schema_version: int = CLIENT_EVENT_SCHEMA_VERSION


def _counter(metrics: object | None, name: str) -> None:
    increment = getattr(metrics, "increment_counter", None) if metrics is not None else None
    if callable(increment):
        increment(name)


def project_client_event(event: RuntimeEvent) -> ClientDeliveryEvent | None:
    """Project only fields already safe for the current client wire."""
    payload: dict[str, object]
    event_type: str
    if event.event_type is RuntimeEventType.RUN_STARTED:
        event_type, payload = "run.started", {"status": event.payload.status}
    elif event.event_type is RuntimeEventType.OUTPUT_DELTA:
        assert isinstance(event.payload, OutputDeltaPayload)
        event_type, payload = "output.delta", {"text": event.payload.text}
    elif event.event_type is RuntimeEventType.ERROR:
        assert isinstance(event.payload, ErrorPayload)
        event_type, payload = "run.error", {
            "error_code": event.payload.safe_error_code,
            "safe_message": event.payload.safe_message,
        }
    elif event.event_type is RuntimeEventType.CANCELLATION:
        assert isinstance(event.payload, CancellationPayload)
        event_type, payload = "run.cancellation", {
            "reason": event.payload.reason
        }
    elif event.event_type is RuntimeEventType.RUN_COMPLETED:
        assert isinstance(event.payload, RunCompletedPayload)
        terminal_types = {
            "SUCCEEDED": "run.completed",
            "FAILED": "run.failed",
            "CANCELLED": "run.cancelled",
        }
        event_type = terminal_types.get(event.payload.status)
        if event_type is None:
            raise ValueError("Unsupported terminal Run status")
        payload = {
            "status": event.payload.status.lower(),
            "stop_reason": event.payload.stop_reason,
        }
        if event.payload.safe_error_code is not None:
            payload["error_code"] = event.payload.safe_error_code
    elif event.event_type is RuntimeEventType.TOOL_APPROVAL_REQUESTED:
        assert isinstance(event.payload, ToolApprovalRequestedPayload)
        event_type, payload = "approval.required", {
            "approval_id": event.payload.approval_id,
            "tool_name": event.payload.tool_name,
            "risk_level": event.payload.risk_level,
        }
    else:
        return None
    return ClientDeliveryEvent(
        run_id=event.run_id, cursor=event.sequence, event_type=event_type,
        payload=payload, created_at=event.emitted_at, event_id=event.event_id,
    )


class InMemoryClientEventFeed:
    def __init__(self, metrics: object | None = None) -> None:
        self._events: dict[str, dict[int, ClientDeliveryEvent]] = {}
        self._lock = asyncio.Lock()
        self._metrics = metrics

    async def append_event(self, event: RuntimeEvent) -> None:
        projected = project_client_event(event)
        if projected is None:
            return
        async with self._lock:
            run = self._events.setdefault(projected.run_id, {})
            existing = run.get(projected.cursor)
            if existing is not None and existing != projected:
                raise ValueError("Client Feed cursor conflict")
            run[projected.cursor] = projected
        _counter(self._metrics, "client_event_feed_write_total")

    async def read_after(self, run_id: str, cursor: int, limit: int = 100):
        async with self._lock:
            return tuple(self._events.get(run_id, {}).get(key) for key in sorted(
                key for key in self._events.get(run_id, {}) if key > cursor
            )[:limit] if self._events.get(run_id, {}).get(key) is not None)


class PostgresClientEventFeed:
    def __init__(self, database: Database, metrics: object | None = None) -> None:
        self._database = database
        self._metrics = metrics

    async def append_event(self, event: RuntimeEvent) -> None:
        projected = project_client_event(event)
        if projected is None:
            return
        try:
            async with self._database.transaction() as session:
                await self._append_projected_in_transaction(session, projected)
            self.record_write_succeeded()
        except Exception:
            self.record_write_failed()
            raise

    async def append_event_in_transaction(self, session, event: RuntimeEvent) -> bool:
        """在 Journal Owner 的 transaction 内写投影；返回事件是否 client-visible。"""
        projected = project_client_event(event)
        if projected is None:
            return False
        try:
            await self._append_projected_in_transaction(session, projected)
        except Exception:
            self.record_write_failed()
            raise
        return True

    async def _append_projected_in_transaction(self, session, projected) -> None:
        result = await session.execute(
            insert(ClientDeliveryEventRow)
            .values(
                run_id=projected.run_id,
                cursor=projected.cursor,
                event_type=projected.event_type,
                payload=projected.payload,
                created_at=projected.created_at,
                event_id=projected.event_id,
                schema_version=projected.schema_version,
            )
            .on_conflict_do_nothing(index_elements=["run_id", "cursor"])
        )
        if result.rowcount != 0:
            return
        existing = await session.scalar(
            select(ClientDeliveryEventRow).where(
                ClientDeliveryEventRow.run_id == projected.run_id,
                ClientDeliveryEventRow.cursor == projected.cursor,
            )
        )
        if existing is None:
            raise RuntimeError("Client Feed duplicate row disappeared")
        if (
            existing.payload != projected.payload
            or existing.event_type != projected.event_type
        ):
            raise ValueError("Client Feed cursor conflict")

    def record_write_succeeded(self) -> None:
        _counter(self._metrics, "client_event_feed_write_total")

    def record_write_failed(self) -> None:
        _counter(self._metrics, "client_event_feed_write_failures")

    async def read_after(self, run_id: str, cursor: int, limit: int = 100):
        async with self._database.session() as session:
            rows = (await session.scalars(select(ClientDeliveryEventRow).where(
                ClientDeliveryEventRow.run_id == run_id,
                ClientDeliveryEventRow.cursor > cursor,
            ).order_by(ClientDeliveryEventRow.cursor.asc()).limit(limit))).all()
            return tuple(ClientDeliveryEvent(
                run_id=row.run_id, cursor=row.cursor, event_type=row.event_type,
                payload=dict(row.payload), created_at=row.created_at,
                event_id=row.event_id, schema_version=row.schema_version,
            ) for row in rows)


def client_event_data(event: ClientDeliveryEvent) -> str:
    return json.dumps(event.payload, ensure_ascii=False, separators=(",", ":"))


__all__ = ["CLIENT_FEED_POLL_INTERVAL_SECONDS", "ClientDeliveryEvent", "InMemoryClientEventFeed", "PostgresClientEventFeed", "client_event_data", "project_client_event"]
