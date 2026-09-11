#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""独立 Evaluation Worker 入口。

生产 worker 在独立进程中复用 canonical ``server.lifespan(server.app)`` 的应用级
装配，从 ``app.state`` 取得同一 ``ChatService`` 与 PostgreSQL ``Database``；不启动
HTTP server，也不创建第二套 evaluator 或第二个数据库池。
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import uuid

from core.evaluation_worker import (
    KafkaEvaluationWorker,
    KafkaWorkerConfig,
    RuntimeEvaluationExecutor,
)
from core.kafka_event_sink import validate_kafka_topic


async def _run(args: argparse.Namespace) -> None:
    # server.lifespan is the canonical application composition context.  This
    # worker reuses its ChatService and Database but never starts an HTTP server.
    import server

    settings = server.settings
    server.app.state.process_role = "worker"
    if not settings.kafka_enabled:
        raise SystemExit("Kafka worker requires LOCAL_AGENT_KAFKA_ENABLED=true")
    await validate_kafka_topic(
        settings.kafka_bootstrap_servers,
        settings.kafka_job_topic,
        timeout_seconds=settings.kafka_poll_timeout_seconds,
        security_protocol=settings.kafka_security_protocol,
        sasl_username=settings.kafka_sasl_username,
        sasl_password=settings.kafka_sasl_password,
    )
    await validate_kafka_topic(
        settings.kafka_bootstrap_servers,
        settings.kafka_job_dlq_topic,
        timeout_seconds=settings.kafka_poll_timeout_seconds,
        security_protocol=settings.kafka_security_protocol,
        sasl_username=settings.kafka_sasl_username,
        sasl_password=settings.kafka_sasl_password,
    )
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except NotImplementedError:  # pragma: no cover - Windows event loop
            signal.signal(signum, lambda *_: stop_event.set())
    async with server.lifespan(server.app):
        server.app.state.observability_service.start_metrics_http_server(
            args.metrics_port, address=args.metrics_address
        )
        job_service = server.app.state.evaluation_job_service
        database = job_service.database
        worker = KafkaEvaluationWorker(
            database,
            RuntimeEvaluationExecutor(server.app.state.chat_service),
            KafkaWorkerConfig(
                bootstrap_servers=settings.kafka_bootstrap_servers,
                client_id=settings.kafka_client_id,
                topic=settings.kafka_job_topic,
                dlq_topic=settings.kafka_job_dlq_topic,
                consumer_group=settings.kafka_consumer_group,
                poll_timeout_seconds=settings.kafka_poll_timeout_seconds,
                max_poll_interval_ms=settings.kafka_max_poll_interval_ms,
                session_timeout_ms=settings.kafka_session_timeout_ms,
                job_lease_seconds=max(1.0, settings.kafka_max_poll_interval_ms / 1000 - 30),
                dlq_timeout_seconds=settings.kafka_produce_timeout_seconds,
                security_protocol=settings.kafka_security_protocol,
                sasl_username=settings.kafka_sasl_username,
                sasl_password=settings.kafka_sasl_password,
            ),
            worker_id=args.worker_id or f"worker-{uuid.uuid4()}",
            observability=server.app.state.observability_service,
        )
        await worker.run(stop_event)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-id")
    parser.add_argument("--metrics-port", type=int, default=0)
    parser.add_argument("--metrics-address", default="127.0.0.1")
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()
