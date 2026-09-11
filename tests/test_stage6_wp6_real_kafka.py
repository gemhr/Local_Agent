"""WP6 real Kafka ACK、header 与 failure metric evidence。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import asyncio
import uuid

import pytest
from confluent_kafka import Consumer
from confluent_kafka.admin import AdminClient, NewTopic

from core.kafka_event_sink import KafkaEventSink, KafkaProducerConfig
from core.observability import ObservabilityService
from core.persistence.repositories.evaluation_jobs import OutboxClaim
from core.settings import Settings


@pytest.mark.asyncio
async def test_real_kafka_ack_trace_header_and_unavailable_metric() -> None:
    topic = f"test.{uuid.uuid4().hex}.wp6.observability"
    group = f"test.{uuid.uuid4().hex}.wp6"
    admin = AdminClient({"bootstrap.servers": "127.0.0.1:9092"})
    future = admin.create_topics([NewTopic(topic, num_partitions=1, replication_factor=1)])[topic]
    await asyncio.to_thread(future.result, 10)

    settings = replace(Settings.load(), tracing_enabled=True, otel_trace_sample_ratio=1.0)
    observability = ObservabilityService(settings, process_role="publisher")
    event_id = uuid.uuid4()
    job_id = uuid.uuid4()
    claim = OutboxClaim(
        event_id=event_id,
        event_type="EVALUATION_JOB_QUEUED",
        aggregate_type="EVALUATION_JOB",
        aggregate_id=job_id,
        schema_version=1,
        payload={
            "schema_version": "evaluation-job-queued.v1",
            "event_id": str(event_id),
            "job_id": str(job_id),
        },
        payload_digest="0" * 64,
        attempt_count=0,
        claim_owner="test",
        claim_token=uuid.uuid4(),
        claim_deadline=datetime.now(UTC),
    )
    sink = KafkaEventSink(
        KafkaProducerConfig("127.0.0.1:9092", "wp6-test", topic, 5.0),
        observability=observability,
    )
    consumer = Consumer(
        {
            "bootstrap.servers": "127.0.0.1:9092",
            "group.id": group,
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
            "allow.auto.create.topics": False,
        }
    )
    try:
        with observability.start_server_span(
            {"traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"},
            "POST",
        ):
            await sink.publish(claim)
        consumer.subscribe([topic])
        message = None
        for _ in range(30):
            candidate = await asyncio.to_thread(consumer.poll, 0.25)
            if candidate is not None and candidate.error() is None:
                message = candidate
                break
        assert message is not None
        headers = dict(message.headers() or ())
        assert b"0af7651916cd43dd8448eb211c80319c" in headers["traceparent"]

        unavailable = KafkaEventSink(
            KafkaProducerConfig("127.0.0.1:1", "wp6-failure", topic, 0.2),
            observability=observability,
        )
        with pytest.raises(Exception):
            await unavailable.publish(claim)
        rendered = observability.render_metrics().body.decode()
        assert 'localagent_kafka_producer_publish_total{outcome="acked"} 1.0' in rendered
        assert (
            'localagent_kafka_producer_publish_total{outcome="failed"} 1.0' in rendered
            or 'localagent_kafka_producer_publish_total{outcome="timeout"} 1.0' in rendered
        )
    finally:
        consumer.close()
        await sink.close()
        await observability.close()
        delete = admin.delete_topics([topic], operation_timeout=10)[topic]
        await asyncio.to_thread(delete.result, 10)
