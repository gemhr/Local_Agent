"""WP5 real PostgreSQL + real Kafka evidence.

Every test owns random topics and a random consumer group, and removes only those
topics.  The Kafka broker is supplied by the WP5 single-node test container.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

import pytest
from confluent_kafka import Consumer, Producer
from confluent_kafka.admin import AdminClient, NewTopic
from sqlalchemy import func, select

from core.evaluation_jobs import EvaluationJobRequest, EvaluationJobService, JobError
from core.evaluation_worker import KafkaEvaluationWorker, KafkaWorkerConfig
from core.kafka_event_sink import KafkaEventSink, KafkaProducerConfig
from core.outbox_publisher import OutboxPublisherConfig, OutboxPublisherService
from core.persistence.models import (
    ConsumerProcessedEventRow,
    EvaluationResultRow,
    OutboxEventRow,
    RoleRow,
    UserRoleRow,
    UserRow,
)
from core.persistence.repositories import evaluation_jobs as repository


BOOTSTRAP = os.getenv("LOCAL_AGENT_KAFKA_BOOTSTRAP_SERVERS", "127.0.0.1:9092")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ROLE_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _admin() -> AdminClient:
    return AdminClient({"bootstrap.servers": BOOTSTRAP})


@pytest.fixture()
def kafka_topics():
    suffix = uuid.uuid4().hex
    topics = (
        f"test.{suffix}.evaluation.jobs.v1",
        f"test.{suffix}.evaluation.jobs.v1.dlq",
        f"test.{suffix}.evaluation.jobs.v1.bad",
    )
    admin = _admin()
    futures = admin.create_topics(
        [NewTopic(topics[0], num_partitions=2, replication_factor=1), NewTopic(topics[1], 1, 1), NewTopic(topics[2], 1, 1)]
    )
    for future in futures.values():
        future.result(15)
    try:
        yield topics
    finally:
        futures = admin.delete_topics(list(topics), operation_timeout=15)
        for future in futures.values():
            try:
                future.result(15)
            except Exception:
                pass


async def _user(database) -> uuid.UUID:
    async with database.transaction() as session:
        if await session.get(RoleRow, _ROLE_ID) is None:
            session.add(RoleRow(id=_ROLE_ID, code="USER"))
            await session.flush()
        user_id = uuid.uuid4()
        session.add(UserRow(id=user_id, subject=str(user_id), display_name="wp5"))
        await session.flush()
        session.add(UserRoleRow(user_id=user_id, role_id=_ROLE_ID))
    return user_id


def _produce(topic: str, value: dict, *, key: str | None = None) -> None:
    producer = Producer({"bootstrap.servers": BOOTSTRAP, "enable.idempotence": True, "acks": "all"})
    producer.produce(topic, key=(key or "k").encode(), value=json.dumps(value).encode())
    assert producer.flush(10) == 0


def _consumer(topic: str, group: str) -> Consumer:
    consumer = Consumer(
        {
            "bootstrap.servers": BOOTSTRAP,
            "group.id": group,
            "enable.auto.commit": False,
            "enable.auto.offset.store": False,
            "auto.offset.reset": "earliest",
        }
    )
    consumer.subscribe([topic])
    return consumer


def _poll(consumer: Consumer, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = consumer.poll(0.5)
        if message is not None and message.error() is None:
            return message
    raise AssertionError("Kafka message was not delivered")


def _poll_for_job(consumer: Consumer, job_id: uuid.UUID, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = consumer.poll(0.5)
        if message is None or message.error() is not None:
            continue
        payload = json.loads(message.value())
        if payload.get("job_id") == str(job_id):
            return message
    raise AssertionError(f"Kafka message for job {job_id} was not delivered")


class _DeterministicExecutor:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, job):
        self.calls += 1
        return {"schema_version": "evaluation-result.v1", "status": "SUCCEEDED", "value": "ok"}


def _worker(database, topic: str, dlq: str, group: str, consumer: Consumer, executor, *, dlq_producer=None):
    return KafkaEvaluationWorker(
        database,
        executor,
        KafkaWorkerConfig(
            bootstrap_servers=BOOTSTRAP,
            client_id=f"wp5-{uuid.uuid4().hex}",
            topic=topic,
            dlq_topic=dlq,
            consumer_group=group,
            poll_timeout_seconds=0.2,
            max_evaluation_seconds=1,
            job_lease_seconds=32,
            dlq_timeout_seconds=1,
        ),
        consumer=consumer,
        dlq_producer=dlq_producer,
        worker_id=f"worker-{uuid.uuid4().hex}",
    )


@pytest.mark.asyncio
async def test_real_outbox_ack_worker_dedup_crash_and_cancel(clean_database, kafka_topics):
    topic, dlq, _ = kafka_topics
    service = EvaluationJobService(clean_database)
    owner = await _user(clean_database)
    job = await service.submit(owner, EvaluationJobRequest("agent", "question", 30))
    async with clean_database.session() as session:
        event = await session.scalar(select(OutboxEventRow).where(OutboxEventRow.aggregate_id == job.job_id))
    sink = KafkaEventSink(KafkaProducerConfig(BOOTSTRAP, "wp5-publisher", topic, 5))
    publisher = OutboxPublisherService(
        clean_database,
        sink,
        OutboxPublisherConfig("wp5-test-publisher", poll_interval_seconds=0.1),
    )
    assert await publisher.run_once() == 1
    async with clean_database.session() as session:
        published = await session.get(OutboxEventRow, event.event_id)
        assert published.status == "PUBLISHED"
        assert published.payload_digest == event.payload_digest
    first_group = f"test.{uuid.uuid4().hex}.group"
    first = _consumer(topic, first_group)
    message = _poll(first)
    assert message.key().decode() == str(job.job_id)
    executor = _DeterministicExecutor()
    worker = _worker(clean_database, topic, dlq, first_group, first, executor)
    worker._commit = lambda _message: (_ for _ in ()).throw(RuntimeError("crash before offset"))
    with pytest.raises(RuntimeError):
        await worker.process_message(message)
    first.close()
    second = _consumer(topic, worker._config.consumer_group)
    redelivery = _poll(second)
    worker2 = _worker(clean_database, topic, dlq, worker._config.consumer_group, second, executor)
    assert await worker2.process_message(redelivery) == "NOOP"
    assert executor.calls == 1
    second.close()
    async with clean_database.session() as session:
        assert await session.scalar(select(func.count()).select_from(EvaluationResultRow).where(EvaluationResultRow.job_id == job.job_id)) == 1
        assert await session.scalar(select(func.count()).select_from(ConsumerProcessedEventRow)) == 1

    cancelled = await service.submit(owner, EvaluationJobRequest("agent", "cancel", 30))
    await service.cancel(cancelled.job_id)
    await publisher.run_once()
    cancel_group = f"test.{uuid.uuid4().hex}.cancel"
    cancel_consumer = _consumer(topic, cancel_group)
    cancel_executor = _DeterministicExecutor()
    cancel_worker = _worker(clean_database, topic, dlq, cancel_group, cancel_consumer, cancel_executor)
    assert await cancel_worker.process_message(
        _poll_for_job(cancel_consumer, cancelled.job_id)
    ) == "NOOP"
    assert cancel_executor.calls == 0
    cancel_consumer.close()
    async with clean_database.session() as session:
        assert await session.scalar(select(func.count()).select_from(EvaluationResultRow).where(EvaluationResultRow.job_id == cancelled.job_id)) == 0
        cancelled_event = await session.scalar(
            select(OutboxEventRow).where(OutboxEventRow.aggregate_id == cancelled.job_id)
        )
        evidence = await session.get(
            ConsumerProcessedEventRow, (cancel_group, cancelled_event.event_id)
        )
        assert evidence.outcome == "CANCELLED_NOOP"


@pytest.mark.asyncio
async def test_real_pending_outbox_intent_is_valid_after_broker_ack(clean_database, kafka_topics):
    topic, dlq, _ = kafka_topics
    service = EvaluationJobService(clean_database)
    owner = await _user(clean_database)
    job = await service.submit(owner, EvaluationJobRequest("agent", "pending-window", 30))
    async with clean_database.session() as session:
        event = await session.scalar(
            select(OutboxEventRow).where(OutboxEventRow.aggregate_id == job.job_id)
        )
        assert event.status == "PENDING"

    # Simulate Kafka delivery ACK immediately before the publisher's PG mark.
    _produce(topic, event.payload, key=str(job.job_id))
    group = f"test.{uuid.uuid4().hex}.pending-window"
    consumer = _consumer(topic, group)
    executor = _DeterministicExecutor()
    worker = _worker(clean_database, topic, dlq, group, consumer, executor)
    assert await worker.process_message(_poll_for_job(consumer, job.job_id)) == "SUCCEEDED"
    consumer.close()
    async with clean_database.session() as session:
        persisted = await session.get(OutboxEventRow, event.event_id)
        assert persisted.status == "PENDING"
        assert await session.scalar(
            select(func.count()).select_from(EvaluationResultRow).where(
                EvaluationResultRow.job_id == job.job_id
            )
        ) == 1


@pytest.mark.asyncio
async def test_real_broker_failure_keeps_outbox_pending(clean_database, kafka_topics):
    topic, _, _ = kafka_topics
    service = EvaluationJobService(clean_database)
    owner = await _user(clean_database)
    job = await service.submit(owner, EvaluationJobRequest("agent", "unavailable", 30))
    sink = KafkaEventSink(KafkaProducerConfig("127.0.0.1:65531", "wp5-down", topic, 0.2))
    publisher = OutboxPublisherService(clean_database, sink, OutboxPublisherConfig("wp5-down", retry_max_seconds=1))
    assert await publisher.run_once() == 0
    async with clean_database.session() as session:
        event = await session.scalar(select(OutboxEventRow).where(OutboxEventRow.aggregate_id == job.job_id))
        assert event.status == "PENDING"


@pytest.mark.asyncio
async def test_real_dlq_ack_and_failed_dlq_do_not_commit(clean_database, kafka_topics):
    topic, dlq, bad_topic = kafka_topics
    service = EvaluationJobService(clean_database)
    owner = await _user(clean_database)
    job = await service.submit(owner, EvaluationJobRequest("agent", "poison", 30))
    # The forged trigger uses a valid envelope but no matching PG Outbox event.
    forged = {"schema_version": "evaluation-job-queued.v1", "event_id": str(uuid.uuid4()), "job_id": str(job.job_id)}
    _produce(topic, forged, key=str(job.job_id))
    poison_group = f"test.{uuid.uuid4().hex}.poison"
    source = _consumer(topic, poison_group)
    executor = _DeterministicExecutor()
    worker = _worker(clean_database, topic, dlq, poison_group, source, executor)
    assert await worker.process_message(_poll(source)) == "DLQ"
    assert executor.calls == 0
    dlq_consumer = _consumer(dlq, f"test.{uuid.uuid4().hex}.dlq")
    assert _poll(dlq_consumer).value()
    source.close()
    dlq_consumer.close()

    # A broker-unavailable DLQ leaves the malformed source offset uncommitted.
    bad_group = f"test.{uuid.uuid4().hex}.dlq-fail"
    source2 = _consumer(bad_topic, bad_group)
    _produce(bad_topic, {"schema_version": "unsupported.v0"}, key="bad")
    bad_worker = _worker(
        clean_database,
        topic,
        dlq,
        bad_group,
        source2,
        _DeterministicExecutor(),
        dlq_producer=Producer({"bootstrap.servers": "127.0.0.1:65531"}),
    )
    with pytest.raises(Exception):
        await bad_worker.process_message(_poll(source2))
    source2.close()
    source3 = _consumer(bad_topic, bad_group)
    assert json.loads(_poll(source3).value()) == {"schema_version": "unsupported.v0"}
    source3.close()


@pytest.mark.asyncio
async def test_real_pg_claim_expiry_reclaim_and_stale_fencing(clean_database):
    service = EvaluationJobService(clean_database)
    owner = await _user(clean_database)
    job = await service.submit(owner, EvaluationJobRequest("agent", "lease", 30))
    first = await service.claim_for_worker(job.job_id, claim_owner="worker-a", lease_seconds=0.1)
    await asyncio.sleep(0.2)
    second = await service.claim_for_worker(job.job_id, claim_owner="worker-b", lease_seconds=30)
    assert first is not None and second is not None and first.claim_token != second.claim_token
    assert await service.claim_for_worker(job.job_id, claim_owner="worker-c", lease_seconds=30) is None
    with pytest.raises(JobError):
        await service.record_processed_event_noop(
            job_id=job.job_id, consumer_name="test-worker", event_id=uuid.uuid4(),
            topic="test.topic", partition=0, offset=0,
        )
    async with clean_database.session() as session:
        assert await session.scalar(select(func.count()).select_from(ConsumerProcessedEventRow)) == 0
    with pytest.raises(JobError):
        await service.finalize_worker_success(
            job_id=job.job_id,
            claim_owner=first.claim_owner,
            claim_token=first.claim_token,
            consumer_name="test-worker",
            event_id=uuid.uuid4(),
            topic="test.topic",
            partition=0,
            offset=0,
            result_payload={"schema_version": "evaluation-result.v1", "status": "SUCCEEDED"},
        )
    async with clean_database.session() as session:
        event = await session.scalar(
            select(OutboxEventRow).where(OutboxEventRow.aggregate_id == job.job_id)
        )
    finalized = await service.finalize_worker_success(
        job_id=job.job_id,
        claim_owner=second.claim_owner,
        claim_token=second.claim_token,
        consumer_name="test-worker",
        event_id=event.event_id,
        topic="test.topic",
        partition=0,
        offset=0,
        result_payload={"schema_version": "evaluation-result.v1", "status": "SUCCEEDED"},
    )
    assert finalized.applied is True
    async with clean_database.session() as session:
        assert await session.scalar(
            select(func.count()).select_from(EvaluationResultRow).where(
                EvaluationResultRow.job_id == job.job_id
            )
        ) == 1


@pytest.mark.asyncio
async def test_real_two_consumers_same_group_assignment_one_result(clean_database, kafka_topics):
    topic, dlq, _ = kafka_topics
    service = EvaluationJobService(clean_database)
    owner = await _user(clean_database)
    job = await service.submit(owner, EvaluationJobRequest("agent", "two-workers", 30))
    sink = KafkaEventSink(KafkaProducerConfig(BOOTSTRAP, "wp5-two-publisher", topic, 5))
    publisher = OutboxPublisherService(clean_database, sink, OutboxPublisherConfig("wp5-two"))
    group = f"test.{uuid.uuid4().hex}.two-workers"
    first = _consumer(topic, group)
    second = _consumer(topic, group)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not (first.assignment() and second.assignment()):
        first.poll(0.2)
        second.poll(0.2)

    assert first.assignment() and second.assignment()
    assert await publisher.run_once() == 1
    messages = []
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not messages:
        for consumer in (first, second):
            candidate = consumer.poll(0.2)
            if candidate is not None and candidate.error() is None:
                messages.append((consumer, candidate))
                break
        if messages:
            break
    assert len(messages) == 1
    executor = _DeterministicExecutor()
    consumer, message = messages[0]
    worker = _worker(clean_database, topic, dlq, group, consumer, executor)
    assert await worker.process_message(message) == "SUCCEEDED"
    first.close()
    second.close()
    async with clean_database.session() as session:
        assert await session.scalar(
            select(func.count()).select_from(EvaluationResultRow).where(EvaluationResultRow.job_id == job.job_id)
        ) == 1
        assert executor.calls == 1
