"""WP6 HTTP → durable Outbox → Kafka headers → Worker distributed trace。"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import asyncio
import json
import uuid

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import select

from core.evaluation_jobs import EvaluationJobRequest, EvaluationJobService
from core.evaluation_worker import KafkaEvaluationWorker, KafkaWorkerConfig
from core.kafka_event_sink import KafkaEventSink, KafkaProducerConfig
from core.observability import ObservabilityService
from core.outbox_publisher import OutboxPublisherConfig, OutboxPublisherService
from core.persistence.models import OutboxEventRow, UserRow
from core.settings import Settings


class _Producer:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail = False

    def produce(self, topic, **kwargs) -> None:
        self.calls.append({"topic": topic, **kwargs})

    def flush(self, _timeout: float) -> int:
        if self.fail:
            return 1
        if self.calls:
            self.calls[-1]["callback"](None, None)
        return 0


class _Consumer:
    def commit(self, **_kwargs):
        return [SimpleNamespace(error=lambda: None)]


class _Message:
    def __init__(self, call: dict) -> None:
        self._call = call

    def value(self):
        return self._call["value"]

    def key(self):
        return self._call["key"]

    def headers(self):
        return self._call["headers"]

    def topic(self):
        return self._call["topic"]

    def partition(self):
        return 0

    def offset(self):
        return 1


class _Executor:
    async def execute(self, job):
        return {
            "schema_version": "evaluation-result.v1",
            "job_id": str(job.job_id),
            "status": "SUCCEEDED",
        }


class _TimeoutExecutor:
    async def execute(self, _job):
        await asyncio.sleep(0.05)
        return {}


def _observability(role: str):
    exporter = InMemorySpanExporter()
    settings = replace(
        Settings.load(), tracing_enabled=True, otel_trace_sample_ratio=1.0
    )
    return ObservabilityService(settings, process_role=role, span_exporter=exporter), exporter


@pytest.mark.asyncio
async def test_trace_context_survives_outbox_commit_and_process_restart(clean_database) -> None:
    owner = uuid.uuid4()
    async with clean_database.transaction() as session:
        session.add(UserRow(id=owner, subject=str(owner), display_name="trace-test"))

    api_observability, api_exporter = _observability("api")
    api_jobs = EvaluationJobService(clean_database, observability=api_observability)
    incoming_trace_id = "0af7651916cd43dd8448eb211c80319c"
    with api_observability.start_server_span(
        {"traceparent": f"00-{incoming_trace_id}-b7ad6b7169203331-01"}, "POST"
    ) as span:
        job = await api_jobs.submit(
            owner,
            EvaluationJobRequest("agent", "private query", 30.0),
        )
        api_observability.finish_server_span(
            span, "/api/evaluation/jobs", 202
        )

    async with clean_database.session() as session:
        durable = await session.scalar(select(OutboxEventRow))
        assert durable is not None
        assert durable.traceparent is not None
        assert incoming_trace_id in durable.traceparent
        assert durable.tracestate is None
        assert "private query" not in durable.traceparent

    # API span 已结束，Publisher 是全新的 process-level Owner；上下文只能来自 PostgreSQL。
    publisher_observability, publisher_exporter = _observability("publisher")
    producer = _Producer()
    sink = KafkaEventSink(
        KafkaProducerConfig("broker", "publisher", "jobs", 1.0),
        producer=producer,
        observability=publisher_observability,
    )
    publisher = OutboxPublisherService(
        clean_database,
        sink,
        OutboxPublisherConfig("publisher-test"),
        observability=publisher_observability,
    )
    assert await publisher.run_once() == 1
    kafka_call = producer.calls[0]
    kafka_headers = dict(kafka_call["headers"])
    assert incoming_trace_id.encode("ascii") in kafka_headers["traceparent"]
    assert "private query" not in json.dumps(kafka_call, default=str)

    worker_observability, worker_exporter = _observability("worker")
    worker = KafkaEvaluationWorker(
        clean_database,
        _Executor(),
        KafkaWorkerConfig(
            "broker", "worker", "jobs", "jobs.dlq", "workers",
            max_evaluation_seconds=1.0,
            job_lease_seconds=32.0,
            max_poll_interval_ms=63_000,
        ),
        consumer=_Consumer(),
        dlq_producer=_Producer(),
        worker_id="worker-test",
        observability=worker_observability,
    )
    assert await worker.process_message(_Message(kafka_call)) == "SUCCEEDED"

    first_request_spans = (
        api_exporter.get_finished_spans()
        + publisher_exporter.get_finished_spans()
        + worker_exporter.get_finished_spans()
    )
    assert {f"{item.context.trace_id:032x}" for item in first_request_spans} == {
        incoming_trace_id
    }
    assert {item.name for item in first_request_spans} >= {
        "HTTP request",
        "outbox publish",
        "kafka produce",
        "kafka consume/process",
        "evaluation execution",
    }

    cancelled = await api_jobs.submit(
        owner, EvaluationJobRequest("agent", "cancelled private query", 30.0)
    )
    await api_jobs.cancel(cancelled.job_id)
    assert await publisher.run_once() == 1
    assert await worker.process_message(_Message(producer.calls[1])) == "NOOP"

    producer.fail = True
    await api_jobs.submit(owner, EvaluationJobRequest("agent", "publish failure", 30.0))
    assert await publisher.run_once() == 0
    producer.fail = False

    await api_jobs.submit(owner, EvaluationJobRequest("agent", "worker timeout", 30.0))
    assert await publisher.run_once() == 1
    timeout_worker = KafkaEvaluationWorker(
        clean_database,
        _TimeoutExecutor(),
        KafkaWorkerConfig(
            "broker", "worker-timeout", "jobs", "jobs.dlq", "workers-timeout",
            max_evaluation_seconds=0.01,
            job_lease_seconds=32.0,
            max_poll_interval_ms=63_000,
        ),
        consumer=_Consumer(),
        dlq_producer=_Producer(),
        worker_id="worker-timeout-test",
        observability=worker_observability,
    )
    with pytest.raises(TimeoutError):
        await timeout_worker.process_message(_Message(producer.calls[-1]))

    publisher_metrics = publisher_observability.render_metrics().body.decode()
    assert 'localagent_outbox_publish_total{outcome="success"} 3.0' in publisher_metrics
    assert 'localagent_outbox_publish_total{outcome="failure"} 1.0' in publisher_metrics
    assert 'localagent_kafka_producer_publish_total{outcome="acked"} 3.0' in publisher_metrics
    assert 'localagent_kafka_producer_publish_total{outcome="timeout"} 1.0' in publisher_metrics
    worker_metrics = worker_observability.render_metrics().body.decode()
    assert 'localagent_kafka_consumer_messages_total{outcome="processed"} 1.0' in worker_metrics
    assert 'localagent_evaluation_worker_execution_total{outcome="success"} 1.0' in worker_metrics
    assert 'localagent_evaluation_worker_execution_total{outcome="cancelled_noop"} 1.0' in worker_metrics
    assert 'localagent_evaluation_worker_execution_total{outcome="timeout"} 1.0' in worker_metrics
    assert 'localagent_kafka_consumer_messages_total{outcome="cancelled_noop"} 1.0' in worker_metrics
    assert 'localagent_kafka_offset_commit_total{outcome="success"} 2.0' in worker_metrics

    api_metrics = api_observability.render_metrics().body.decode()
    assert "localagent_evaluation_jobs_created_total 4.0" in api_metrics
    assert 'localagent_evaluation_job_finalization_total{outcome="cancelled"} 1.0' in api_metrics

    await api_observability.close()
    await publisher_observability.close()
    await worker_observability.close()
