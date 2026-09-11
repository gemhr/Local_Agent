#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Kafka transport adapters for the WP5 transactional outbox and DLQ."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any

from core.outbox_publisher import EventSink
from core.persistence.repositories.evaluation_jobs import OutboxClaim

logger = logging.getLogger(__name__)


async def await_kafka_io(function: Any, *args: Any) -> Any:
    """等待有界 Kafka 同步调用；取消时先收拢线程，避免与 close/下一次调用重叠。"""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise


@dataclass(frozen=True, slots=True)
class KafkaProducerConfig:
    bootstrap_servers: str
    client_id: str
    topic: str
    delivery_timeout_seconds: float = 10.0
    security_protocol: str = "PLAINTEXT"
    sasl_username: str = field(default="", repr=False)
    sasl_password: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not self.bootstrap_servers or not self.client_id or not self.topic:
            raise ValueError("Kafka producer 配置不能为空")
        if not math.isfinite(self.delivery_timeout_seconds) or self.delivery_timeout_seconds <= 0:
            raise ValueError("delivery_timeout_seconds 必须为正数")
        protocol = self.security_protocol.upper()
        if protocol not in {"PLAINTEXT", "SSL", "SASL_PLAINTEXT", "SASL_SSL"}:
            raise ValueError("unsupported Kafka security_protocol")
        object.__setattr__(self, "security_protocol", protocol)
        has_username = bool(self.sasl_username)
        has_password = bool(self.sasl_password)
        if protocol.startswith("SASL_") and not (has_username and has_password):
            raise ValueError("SASL Kafka 配置需要 username/password")
        if not protocol.startswith("SASL_") and (has_username or has_password):
            raise ValueError("非 SASL Kafka 不得提供 SASL credential")


class KafkaEventSink(EventSink):
    """EventSink implementation which returns only after broker delivery ACK."""

    def __init__(self, config: KafkaProducerConfig, *, producer: Any | None = None) -> None:
        self.config = config
        self._producer = producer
        self._producer_lock = asyncio.Lock()

    def _get_producer(self) -> Any:
        if self._producer is None:
            try:
                from confluent_kafka import Producer
            except ImportError as exc:  # pragma: no cover - dependency packaging failure
                raise RuntimeError("confluent-kafka is required for KafkaEventSink") from exc
            config = {
                    "bootstrap.servers": self.config.bootstrap_servers,
                    "client.id": self.config.client_id,
                    "enable.idempotence": True,
                    "acks": "all",
                    "retries": 2_147_483_647,
                    "max.in.flight.requests.per.connection": 5,
                    "message.timeout.ms": max(1, int(self.config.delivery_timeout_seconds * 1000)),
                    "security.protocol": self.config.security_protocol,
                }
            if self.config.sasl_username or self.config.sasl_password:
                config.update({
                    "sasl.mechanisms": "PLAIN",
                    "sasl.username": self.config.sasl_username,
                    "sasl.password": self.config.sasl_password,
                })
            self._producer = Producer(config)
        return self._producer

    def _publish_sync(self, event: OutboxClaim, deadline: float) -> None:
        producer = self._get_producer()
        delivery_error: list[BaseException] = []
        acknowledged = False

        def delivery_report(error: Any, _message: Any) -> None:
            nonlocal acknowledged
            if error is not None:
                delivery_error.append(RuntimeError(str(error)))
            else:
                acknowledged = True

        payload = json.dumps(
            event.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if time.monotonic() >= deadline:
            raise TimeoutError("Kafka delivery deadline expired before produce")
        producer.produce(
            self.config.topic,
            key=str(event.aggregate_id).encode("ascii"),
            value=payload,
            callback=delivery_report,
        )
        remaining = producer.flush(max(0.001, deadline - time.monotonic()))
        if remaining:
            raise TimeoutError("Kafka broker delivery ACK timeout")
        if delivery_error:
            raise delivery_error[0]
        if not acknowledged:
            raise RuntimeError("Kafka delivery callback did not confirm ACK")

    async def publish(self, event: OutboxClaim) -> None:
        # confluent-kafka is callback-driven/synchronous; never block the ASGI loop.
        async with self._producer_lock:
            deadline = time.monotonic() + self.config.delivery_timeout_seconds
            await await_kafka_io(self._publish_sync, event, deadline)

    async def close(self) -> None:
        async with self._producer_lock:
            producer = self._producer
            if producer is not None:
                await await_kafka_io(producer.flush, self.config.delivery_timeout_seconds)


async def validate_kafka_topic(
    bootstrap_servers: str,
    topic: str,
    *,
    timeout_seconds: float = 5.0,
    security_protocol: str = "PLAINTEXT",
    sasl_username: str = "",
    sasl_password: str = "",
) -> None:
    """Read-only readiness check; production never relies on auto-create."""
    def _check() -> None:
        try:
            from confluent_kafka.admin import AdminClient
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("confluent-kafka is required for Kafka readiness") from exc
        config = {
            "bootstrap.servers": bootstrap_servers,
            "security.protocol": security_protocol,
        }
        if sasl_username or sasl_password:
            config.update({
                "sasl.mechanisms": "PLAIN",
                "sasl.username": sasl_username,
                "sasl.password": sasl_password,
            })
        metadata = AdminClient(config).list_topics(
            timeout=timeout_seconds
        )
        topic_metadata = metadata.topics.get(topic)
        if topic_metadata is None:
            raise RuntimeError(f"Kafka topic does not exist: {topic}")
        if getattr(topic_metadata, "error", None) is not None:
            raise RuntimeError(f"Kafka topic metadata error: {topic}")

    await await_kafka_io(_check)


__all__ = ["KafkaEventSink", "KafkaProducerConfig", "validate_kafka_topic"]
