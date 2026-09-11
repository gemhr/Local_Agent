"""WP5 Kafka transport contract tests; broker integration lives in the WP5 integration suite."""

from __future__ import annotations

import json
import asyncio
import threading
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from core.kafka_event_sink import KafkaEventSink, KafkaProducerConfig
from core.evaluation_jobs import EVALUATION_JOB_REQUEST_SCHEMA, canonical_json_digest
from core.evaluation_worker import (
    KafkaEvaluationWorker,
    KafkaWorkerConfig,
    PermanentEvaluationError,
    RuntimeEvaluationExecutor,
)
from core.persistence.repositories.evaluation_jobs import OutboxClaim


class _Producer:
    def __init__(self, *, pending: int = 0, error=None, report: bool = True) -> None:
        self.calls: list[dict] = []
        self.pending = pending
        self.error = error
        self.report = report

    def produce(self, topic, **kwargs):
        self.calls.append({"topic": topic, **kwargs})

    def flush(self, timeout):
        self.timeout = timeout
        if self.report and self.calls:
            self.calls[-1]["callback"](self.error, None)
        return self.pending


class _Message:
    def __init__(self, value: dict[str, object]) -> None:
        self._value = value

    def value(self):
        import json

        return json.dumps(self._value).encode("utf-8")

    def topic(self):
        return "jobs"

    def partition(self):
        return 1

    def offset(self):
        return 2


def _claim() -> OutboxClaim:
    return OutboxClaim(
        event_id=uuid.uuid4(),
        event_type="EVALUATION_JOB_QUEUED",
        aggregate_type="EVALUATION_JOB",
        aggregate_id=uuid.uuid4(),
        schema_version=1,
        payload={
            "schema_version": "evaluation-job-queued.v1",
            "event_id": str(uuid.uuid4()),
            "job_id": str(uuid.uuid4()),
        },
        payload_digest="0" * 64,
        attempt_count=0,
        claim_owner="test",
        claim_token=uuid.uuid4(),
        claim_deadline=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_kafka_sink_waits_for_delivery_report_and_uses_job_key() -> None:
    producer = _Producer()
    sink = KafkaEventSink(
        KafkaProducerConfig("127.0.0.1:9092", "test", "test.topic", 1),
        producer=producer,
    )
    event = _claim()
    await sink.publish(event)
    assert producer.calls[0]["topic"] == "test.topic"
    assert producer.calls[0]["key"] == str(event.aggregate_id).encode("ascii")
    assert producer.timeout <= 1


@pytest.mark.asyncio
async def test_kafka_sink_does_not_ack_when_flush_is_pending() -> None:
    sink = KafkaEventSink(
        KafkaProducerConfig("127.0.0.1:9092", "test", "test.topic", 1),
        producer=_Producer(pending=1),
    )
    with pytest.raises(TimeoutError):
        await sink.publish(_claim())
    for producer in (_Producer(error=RuntimeError("broker rejected")), _Producer(report=False)):
        sink = KafkaEventSink(KafkaProducerConfig("broker", "test", "topic", 1), producer=producer)
        with pytest.raises(RuntimeError):
            await sink.publish(_claim())


@pytest.mark.asyncio
async def test_cancelled_publish_drains_delivery_before_close() -> None:
    started = threading.Event()
    release = threading.Event()

    class SlowProducer(_Producer):
        flush_calls = 0

        def flush(self, timeout):
            self.flush_calls += 1
            started.set()
            assert release.wait(2)
            return super().flush(timeout)

    producer = SlowProducer()
    sink = KafkaEventSink(KafkaProducerConfig("broker", "test", "topic", 2), producer=producer)
    publishing = asyncio.create_task(sink.publish(_claim()))
    assert await asyncio.to_thread(started.wait, 1)
    publishing.cancel()
    closing = asyncio.create_task(sink.close())
    try:
        await asyncio.sleep(0.02)
        assert not publishing.done() and not closing.done()
        assert producer.flush_calls == 1
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await publishing
        await closing
    assert producer.flush_calls == 2


def test_dlq_preserves_safe_parseable_identity_for_unsupported_schema() -> None:
    event_id = uuid.uuid4()
    job_id = uuid.uuid4()
    producer = _Producer()
    worker = KafkaEvaluationWorker(
        SimpleNamespace(),
        SimpleNamespace(),
        KafkaWorkerConfig(
            "broker:9092",
            "client",
            "jobs",
            "jobs.dlq",
            "workers",
        ),
        consumer=SimpleNamespace(),
        dlq_producer=producer,
    )
    worker._publish_dlq_sync(
        _Message(
            {
                "schema_version": "evaluation-job-queued.v999",
                "event_id": str(event_id),
                "job_id": str(job_id),
                "sensitive_query": "must not be copied",
            }
        ),
        PermanentEvaluationError("unsupported event schema"),
    )

    payload = json.loads(producer.calls[0]["value"])
    assert payload["event_id"] == str(event_id)
    assert payload["job_id"] == str(job_id)
    assert payload["original_schema_version"] == "evaluation-job-queued.v999"
    assert "sensitive_query" not in payload
    worker._publish_dlq_sync(
        _Message({"schema_version": "Bearer secret-query-credential", "event_id": str(event_id)}),
        PermanentEvaluationError("secret failure detail"),
    )
    payload = json.loads(producer.calls[-1]["value"])
    assert payload["event_id"] == str(event_id)
    assert "original_schema_version" not in payload
    assert b"secret" not in producer.calls[-1]["value"]


@pytest.mark.asyncio
async def test_commit_partition_error_stops_before_higher_offset_without_blocking_loop() -> None:
    loop_thread = threading.get_ident()

    class Consumer:
        polls = 0
        closed = False
        commits = 0

        def subscribe(self, _topics, **callbacks):
            callbacks["on_revoke"](self, [1])
            assert worker._partition_lost
            callbacks["on_assign"](self, [1])
            assert not worker._partition_lost

        def poll(self, _timeout):
            self.polls += 1
            message = _Message({"schema_version": "unsupported.v0"})
            message.error = lambda: None
            return message

        def commit(self, *, message, asynchronous):
            assert threading.get_ident() != loop_thread
            assert asynchronous is False
            self.commits += 1
            return [SimpleNamespace(error=RuntimeError("partition error"))]

        def close(self):
            self.closed = True

    consumer = Consumer()
    worker = KafkaEvaluationWorker(
        SimpleNamespace(), SimpleNamespace(),
        KafkaWorkerConfig("broker", "client", "jobs", "dlq", "workers"),
        consumer=consumer, dlq_producer=_Producer(),
    )
    with pytest.raises(RuntimeError, match="uncommitted"):
        await worker.run(asyncio.Event())
    assert consumer.polls == consumer.commits == 1
    assert consumer.closed


@pytest.mark.asyncio
async def test_worker_execution_timeout_leaves_business_and_offset_uncommitted() -> None:
    from unittest.mock import AsyncMock, Mock

    worker = KafkaEvaluationWorker(
        SimpleNamespace(), SimpleNamespace(execute=AsyncMock(side_effect=lambda _job: None)),
        KafkaWorkerConfig("broker", "client", "jobs", "dlq", "workers", max_evaluation_seconds=0.01),
        consumer=SimpleNamespace(), dlq_producer=_Producer(),
    )

    async def execute(_job):
        await asyncio.Event().wait()

    worker._executor.execute = execute
    worker._jobs = SimpleNamespace(
        validate_queued_event=AsyncMock(return_value=True),
        claim_for_worker=AsyncMock(return_value=SimpleNamespace(job=object())),
        finalize_worker_success=AsyncMock(), record_worker_failure=AsyncMock(),
    )
    worker._commit = Mock()
    job_id = str(uuid.uuid4())
    message = _Message({"schema_version": "evaluation-job-queued.v1", "event_id": str(uuid.uuid4()), "job_id": job_id})
    message.key = lambda: job_id.encode()
    with pytest.raises(TimeoutError):
        await worker.process_message(message)
    worker._jobs.finalize_worker_success.assert_not_awaited()
    worker._jobs.record_worker_failure.assert_not_awaited()
    worker._commit.assert_not_called()


def test_worker_config_rejects_poll_interval_shorter_than_long_evaluation() -> None:
    with pytest.raises(ValueError, match="max_poll_interval_ms"):
        KafkaWorkerConfig(
            "broker:9092",
            "client",
            "jobs",
            "jobs.dlq",
            "workers",
            max_poll_interval_ms=1,
        )


def test_worker_config_rejects_incomplete_sasl_credentials() -> None:
    with pytest.raises(ValueError, match="SASL"):
        KafkaWorkerConfig(
            "broker:9092",
            "client",
            "jobs",
            "jobs.dlq",
            "workers",
            security_protocol="SASL_SSL",
            sasl_username="worker",
        )


@pytest.mark.asyncio
async def test_runtime_executor_uses_fresh_run_id_for_each_claim_attempt() -> None:
    class _ChatService:
        def __init__(self) -> None:
            self.run_ids = []

        async def run_coordinated_agent(self, _agent_id, _query, **kwargs):
            self.run_ids.append(kwargs["run_id"])
            return "ok", SimpleNamespace(status=SimpleNamespace(value="SUCCEEDED"))

    payload = {
        "schema_version": EVALUATION_JOB_REQUEST_SCHEMA,
        "agent_id": "agent",
        "query": "question",
        "run_id": str(uuid.uuid4()),
        "timeout_seconds": 30,
    }
    job = SimpleNamespace(
        job_id=uuid.uuid4(),
        evaluator_kind="RUNTIME_EVALUATION_V1",
        request_payload=payload,
        request_digest=canonical_json_digest(payload),
    )
    chat_service = _ChatService()
    executor = RuntimeEvaluationExecutor(chat_service)
    await executor.execute(job)
    await executor.execute(job)
    assert len(set(chat_service.run_ids)) == 2
    assert payload["run_id"] not in chat_service.run_ids
