#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""独立 Outbox Publisher 进程入口；生产默认使用 Kafka ACK sink。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import uuid

from core.outbox_publisher import (
    OutboxPublisherConfig,
    OutboxPublisherService,
    RecordingEventSink,
)
from core.kafka_event_sink import KafkaEventSink, KafkaProducerConfig, validate_kafka_topic
from core.persistence import Database, DatabaseConfig, assert_schema_ready
from core.settings import EnvironmentProfile, Settings


async def _run(args: argparse.Namespace) -> None:
    settings = Settings.load()
    if args.recording_sink:
        if settings.environment_profile is not EnvironmentProfile.TEST:
            raise SystemExit("Recording sink is limited to TEST")
        sink = RecordingEventSink()
    else:
        if not settings.kafka_enabled:
            raise SystemExit("Kafka publisher requires LOCAL_AGENT_KAFKA_ENABLED=true")
        await validate_kafka_topic(
            settings.kafka_bootstrap_servers,
            settings.kafka_job_topic,
            timeout_seconds=settings.kafka_produce_timeout_seconds,
            security_protocol=settings.kafka_security_protocol,
            sasl_username=settings.kafka_sasl_username,
            sasl_password=settings.kafka_sasl_password,
        )
        sink = KafkaEventSink(
            KafkaProducerConfig(
                bootstrap_servers=settings.kafka_bootstrap_servers,
                client_id=settings.kafka_client_id,
                topic=settings.kafka_job_topic,
                delivery_timeout_seconds=settings.kafka_produce_timeout_seconds,
                security_protocol=settings.kafka_security_protocol,
                sasl_username=settings.kafka_sasl_username,
                sasl_password=settings.kafka_sasl_password,
            )
        )

    database = Database(DatabaseConfig.from_settings(settings))
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except NotImplementedError:  # pragma: no cover - Windows event loop
            signal.signal(signum, lambda *_: stop_event.set())

    service = OutboxPublisherService(
        database,
        sink,
        OutboxPublisherConfig(
            claim_owner=args.publisher_id or f"publisher-{uuid.uuid4()}",
            batch_size=args.batch_size,
            lease_seconds=args.lease_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
        ),
    )
    try:
        await assert_schema_ready(database)
        await service.run(stop_event)
    finally:
        if hasattr(sink, "close"):
            await sink.close()
        await database.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recording-sink", action="store_true")
    parser.add_argument("--publisher-id")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--lease-seconds", type=float, default=30.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=1.0)
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()
