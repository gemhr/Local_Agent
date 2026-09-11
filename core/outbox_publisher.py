#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Transactional Outbox 的独立 Publisher Service（EventSink 可接 Kafka）。"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
import logging
import math
import random
import time
from dataclasses import dataclass
from typing import Protocol

from opentelemetry.trace import SpanKind, Status, StatusCode

from core.evaluation_jobs import canonical_json_digest
from core.observability import fail_open_span
from core.persistence.database import Database
from core.persistence.errors import PersistenceError
from core.persistence.repositories import evaluation_jobs as repository
from core.persistence.repositories.evaluation_jobs import OutboxClaim

logger = logging.getLogger(__name__)

OUTBOX_CLAIM_CONFLICT = "OUTBOX_CLAIM_CONFLICT"
OUTBOX_STALE_CLAIM = "OUTBOX_STALE_CLAIM"
OUTBOX_PUBLISH_FAILED = "OUTBOX_PUBLISH_FAILED"


class OutboxError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class EventSink(Protocol):
    async def publish(self, event: OutboxClaim) -> None: ...


class RecordingEventSink:
    """仅用于 WP4 Contract 测试/显式本地演示的内存 sink。"""

    def __init__(self) -> None:
        self.events: list[OutboxClaim] = []

    async def publish(self, event: OutboxClaim) -> None:
        self.events.append(event)


@dataclass(frozen=True, slots=True)
class OutboxPublisherConfig:
    claim_owner: str
    batch_size: int = 50
    lease_seconds: float = 30.0
    poll_interval_seconds: float = 1.0
    retry_base_seconds: float = 1.0
    retry_max_seconds: float = 60.0
    retry_jitter_ratio: float = 0.0

    def __post_init__(self) -> None:
        if not self.claim_owner or len(self.claim_owner) > 128:
            raise ValueError("claim_owner 必须为 1..128 字符")
        if self.batch_size < 1 or self.batch_size > 1000:
            raise ValueError("batch_size 必须为 1..1000")
        for name, value in (
            ("lease_seconds", self.lease_seconds),
            ("poll_interval_seconds", self.poll_interval_seconds),
            ("retry_base_seconds", self.retry_base_seconds),
            ("retry_max_seconds", self.retry_max_seconds),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} 必须为正数")
        if not math.isfinite(
            self.retry_jitter_ratio
        ) or not 0 <= self.retry_jitter_ratio <= 1:
            raise ValueError("retry_jitter_ratio 必须位于 0..1")


class OutboxPublisherService:
    def __init__(
        self,
        database: Database,
        sink: EventSink,
        config: OutboxPublisherConfig,
        observability=None,
    ) -> None:
        self._database = database
        self._sink = sink
        self._config = config
        self._observability = observability

    def _observe(self, operation: str, *args) -> None:
        try:
            if self._observability is not None:
                getattr(self._observability, operation)(*args)
        except Exception:
            pass

    def _backoff_seconds(self, attempt_count: int) -> float:
        delay = min(
            self._config.retry_max_seconds,
            self._config.retry_base_seconds * (2 ** min(attempt_count, 30)),
        )
        if self._config.retry_jitter_ratio:
            delay *= random.uniform(
                1 - self._config.retry_jitter_ratio,
                1 + self._config.retry_jitter_ratio,
            )
        return min(self._config.retry_max_seconds, max(0.001, delay))

    async def claim(self) -> tuple[OutboxClaim, ...]:
        async with self._database.transaction() as session:
            claims = await repository.claim_due_outbox_events(
                session,
                claim_owner=self._config.claim_owner,
                lease_seconds=self._config.lease_seconds,
                batch_size=self._config.batch_size,
            )
        if not claims:
            self._observe("observe_outbox_claim", "empty")
        else:
            for claim in claims:
                self._observe(
                    "observe_outbox_claim",
                    "reclaimed" if claim.was_reclaimed else "claimed",
                )
        return claims

    async def mark_published(self, claim: OutboxClaim) -> None:
        async with self._database.transaction() as session:
            marked = await repository.mark_outbox_published(session, claim)
            if not marked:
                raise OutboxError(OUTBOX_STALE_CLAIM)

    async def _record_failure(self, claim: OutboxClaim) -> None:
        async with self._database.transaction() as session:
            updated = await repository.record_outbox_failure(
                session,
                claim,
                backoff_seconds=self._backoff_seconds(claim.attempt_count),
                error_code=OUTBOX_PUBLISH_FAILED,
            )
            if not updated:
                raise OutboxError(OUTBOX_STALE_CLAIM)

    async def run_once(self) -> int:
        claims = await self.claim()
        published = 0
        for claim in claims:
            started_at = time.perf_counter()
            carrier = {
                key: value
                for key, value in {
                    "traceparent": claim.traceparent,
                    "tracestate": claim.tracestate,
                }.items()
                if value
            }
            with fail_open_span(
                lambda: self._observability.start_messaging_span(
                    "outbox publish",
                    carrier=carrier,
                    kind=SpanKind.PRODUCER,
                    attributes={"messaging.operation": "publish", "component": "outbox_publisher"},
                )
                if self._observability is not None
                else nullcontext(None)
            ) as span:
                try:
                    if canonical_json_digest(claim.payload) != claim.payload_digest:
                        raise OutboxError(OUTBOX_PUBLISH_FAILED)
                    await self._sink.publish(claim)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._observe("observe_outbox_publish", "failure", time.perf_counter() - started_at)
                    if span is not None:
                        span.set_status(Status(StatusCode.ERROR))
                    try:
                        await self._record_failure(claim)
                    except OutboxError:
                        logger.warning(
                            "Outbox failure receipt rejected",
                            extra={
                                "component": "outbox_publisher",
                                "status": "STALE",
                                "safe_error_code": OUTBOX_STALE_CLAIM,
                                **self._log_context(),
                            },
                        )
                    logger.warning(
                        "Outbox publish failed",
                        extra={
                            "component": "outbox_publisher",
                            "status": "FAILED",
                            "safe_error_code": OUTBOX_PUBLISH_FAILED,
                            **self._log_context(),
                        },
                    )
                    continue
                try:
                    await self.mark_published(claim)
                except OutboxError:
                    self._observe("observe_outbox_publish", "stale", time.perf_counter() - started_at)
                    if span is not None:
                        span.set_status(Status(StatusCode.ERROR))
                    logger.warning(
                        "Outbox publish receipt rejected",
                        extra={
                            "component": "outbox_publisher",
                            "status": "STALE",
                            "safe_error_code": OUTBOX_STALE_CLAIM,
                            **self._log_context(),
                        },
                    )
                    continue
                published += 1
                self._observe("observe_outbox_publish", "success", time.perf_counter() - started_at)
                logger.info(
                    "Outbox publish succeeded",
                    extra={
                        "component": "outbox_publisher",
                        "status": "SUCCEEDED",
                        **self._log_context(),
                    },
                )
        return published

    def _log_context(self) -> dict[str, str | None]:
        try:
            if self._observability is not None:
                return self._observability.correlated_log_fields()
        except Exception:
            pass
        return {"trace_id": None, "span_id": None}

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except PersistenceError as exc:
                logger.warning(
                    "Outbox database operation failed",
                    extra={
                        "component": "outbox_publisher",
                        "status": "FAILED",
                        "safe_error_code": exc.error_code.value,
                    },
                )
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self._config.poll_interval_seconds
                )
            except TimeoutError:
                pass


__all__ = [
    "EventSink",
    "OUTBOX_CLAIM_CONFLICT",
    "OUTBOX_PUBLISH_FAILED",
    "OUTBOX_STALE_CLAIM",
    "OutboxError",
    "OutboxPublisherConfig",
    "OutboxPublisherService",
    "RecordingEventSink",
]
