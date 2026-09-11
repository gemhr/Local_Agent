#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WP4 独立 Outbox Publisher 进程入口；当前只允许 TEST Recording sink。"""

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
from core.persistence import Database, DatabaseConfig, assert_schema_ready
from core.settings import EnvironmentProfile, Settings


async def _run(args: argparse.Namespace) -> None:
    settings = Settings.load()
    if settings.environment_profile is not EnvironmentProfile.TEST:
        raise SystemExit(
            "WP4 Recording sink is limited to TEST; Kafka delivery belongs to WP5"
        )
    if not args.recording_sink:
        raise SystemExit("WP4 requires explicit --recording-sink")

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
        RecordingEventSink(),
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
