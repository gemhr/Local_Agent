#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""独立 Kafka Evaluation Worker；DB transaction 完成后才提交 offset。"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
import json
import logging
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from opentelemetry.trace import SpanKind, Status, StatusCode

from core.evaluation_jobs import (
    EVALUATION_JOB_REQUEST_SCHEMA,
    EVALUATION_JOB_TIMEOUT_MAX_SECONDS,
    EvaluationJobService,
    JobError,
    canonical_json_digest,
)
from core.observability import fail_open_span
from core.persistence.database import Database
from core.kafka_event_sink import await_kafka_io

logger = logging.getLogger(__name__)


class EvaluationExecutor(Protocol):
    async def execute(self, job: Any) -> dict[str, object]: ...


class RuntimeEvaluationExecutor:
    """Production adapter to the existing ChatService coordinated boundary."""

    def __init__(self, chat_service: Any) -> None:
        self._chat_service = chat_service
        self._max_output_chars = 20_000

    async def execute(self, job: Any) -> dict[str, object]:
        payload = job.request_payload
        if job.evaluator_kind != "RUNTIME_EVALUATION_V1":
            raise PermanentEvaluationError("unsupported evaluator kind")
        if payload.get("schema_version") != EVALUATION_JOB_REQUEST_SCHEMA:
            raise PermanentEvaluationError("unsupported request schema")
        if canonical_json_digest(payload) != job.request_digest:
            raise PermanentEvaluationError("request digest mismatch")
        agent_id = payload.get("agent_id")
        query = payload.get("query")
        timeout_seconds = payload.get("timeout_seconds")
        if not isinstance(agent_id, str) or not agent_id or not isinstance(query, str) or not query:
            raise PermanentEvaluationError("invalid evaluation request")
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(float(timeout_seconds))
            or not 0 < float(timeout_seconds) <= EVALUATION_JOB_TIMEOUT_MAX_SECONDS
        ):
            raise PermanentEvaluationError("invalid evaluation timeout")
        output, result = await self._chat_service.run_coordinated_agent(
            agent_id,
            query,
            # Each claim gets a fresh execution identity.  Reusing the job's
            # submission run_id would collide with a terminal Journal after a
            # lease reclaim.
            run_id=str(uuid.uuid4()),
            timeout_seconds=float(timeout_seconds),
            persist=True,
        )
        status = getattr(result.status, "value", str(result.status))
        if status != "SUCCEEDED":
            raise PermanentEvaluationError(f"evaluation runtime returned {status}")
        return {
            "schema_version": "evaluation-result.v1",
            "job_id": str(job.job_id),
            "status": status,
            "output": (output or "")[: self._max_output_chars],
        }


class PermanentEvaluationError(ValueError):
    """不可恢复的 evaluator 输入/合同错误；worker 将其作为业务失败收口。"""


@dataclass(frozen=True, slots=True)
class KafkaWorkerConfig:
    bootstrap_servers: str
    client_id: str
    topic: str
    dlq_topic: str
    consumer_group: str
    poll_timeout_seconds: float = 1.0
    max_poll_interval_ms: int = 3_900_000
    session_timeout_ms: int = 45_000
    max_evaluation_seconds: float = EVALUATION_JOB_TIMEOUT_MAX_SECONDS
    job_lease_seconds: float = 3_870.0
    dlq_timeout_seconds: float = 10.0
    security_protocol: str = "PLAINTEXT"
    sasl_username: str = field(default="", repr=False)
    sasl_password: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not all((self.bootstrap_servers, self.client_id, self.topic, self.dlq_topic, self.consumer_group)):
            raise ValueError("Kafka worker 配置不能为空")
        if not math.isfinite(self.poll_timeout_seconds) or self.poll_timeout_seconds <= 0:
            raise ValueError("poll_timeout_seconds 必须为正数")
        if (
            not math.isfinite(self.max_evaluation_seconds)
            or self.max_evaluation_seconds <= 0
            or self.max_evaluation_seconds > EVALUATION_JOB_TIMEOUT_MAX_SECONDS
        ):
            raise ValueError("max_evaluation_seconds 超出 Evaluation contract")
        if (
            not math.isfinite(self.job_lease_seconds)
            or self.job_lease_seconds <= self.max_evaluation_seconds + 30
        ):
            raise ValueError("job_lease_seconds 必须覆盖 evaluation 与 finalization 余量")
        if self.max_poll_interval_ms / 1000 < self.job_lease_seconds + 30:
            raise ValueError("max_poll_interval_ms 必须覆盖 claim lease 与 rebalance 余量")
        if not 0 < self.session_timeout_ms < self.max_poll_interval_ms:
            raise ValueError("session_timeout_ms 必须小于 max_poll_interval_ms")
        if self.poll_timeout_seconds * 1000 >= self.session_timeout_ms:
            raise ValueError("poll_timeout_seconds 必须小于 session_timeout_ms")
        if not math.isfinite(self.dlq_timeout_seconds) or self.dlq_timeout_seconds <= 0:
            raise ValueError("dlq_timeout_seconds 必须为正数")
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


class KafkaEvaluationWorker:
    def __init__(
        self,
        database: Database,
        executor: EvaluationExecutor,
        config: KafkaWorkerConfig,
        *,
        consumer: Any | None = None,
        dlq_producer: Any | None = None,
        worker_id: str | None = None,
        observability=None,
    ) -> None:
        self._database = database
        self._executor = executor
        self._config = config
        self._consumer = consumer
        self._dlq_producer = dlq_producer
        self._worker_id = worker_id or f"worker-{uuid.uuid4()}"
        self._observability = observability
        self._jobs = EvaluationJobService(database, observability=observability)
        self._partition_lost = False

    def _on_revoke(self, _consumer: Any, partitions: Any) -> None:
        self._partition_lost = True
        logger.info(
            "Kafka partitions revoked",
            extra={"component": "evaluation_worker", "status": "REVOKED", "count": len(partitions)},
        )

    def _on_assign(self, _consumer: Any, partitions: Any) -> None:
        # Assignment callback starts a fresh generation; old revoke/lost facts
        # must not bleed into this generation's valid commit decisions.
        self._partition_lost = False
        logger.info(
            "Kafka partitions assigned",
            extra={"component": "evaluation_worker", "status": "ASSIGNED", "count": len(partitions)},
        )

    def _on_lost(self, _consumer: Any, partitions: Any) -> None:
        self._partition_lost = True
        logger.warning(
            "Kafka partitions lost; no further offset claims",
            extra={"component": "evaluation_worker", "status": "LOST", "count": len(partitions)},
        )

    def _ensure_clients(self) -> None:
        try:
            from confluent_kafka import Consumer, Producer
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("confluent-kafka is required for KafkaEvaluationWorker") from exc
        if self._consumer is None:
            consumer_config = {
                "bootstrap.servers": self._config.bootstrap_servers,
                "client.id": self._config.client_id,
                "group.id": self._config.consumer_group,
                "enable.auto.commit": False,
                "enable.auto.offset.store": False,
                "allow.auto.create.topics": False,
                "socket.timeout.ms": 10_000,
                "auto.offset.reset": "earliest",
                "max.poll.interval.ms": self._config.max_poll_interval_ms,
                "session.timeout.ms": self._config.session_timeout_ms,
                "security.protocol": self._config.security_protocol,
            }
            if self._config.sasl_username or self._config.sasl_password:
                consumer_config.update({
                    "sasl.mechanisms": "PLAIN",
                    "sasl.username": self._config.sasl_username,
                    "sasl.password": self._config.sasl_password,
                })
            self._consumer = Consumer(consumer_config)
        if self._dlq_producer is None:
            producer_config = {
                    "bootstrap.servers": self._config.bootstrap_servers,
                    "client.id": f"{self._config.client_id}-dlq",
                    "enable.idempotence": True,
                    "acks": "all",
                    "message.timeout.ms": max(1, int(self._config.dlq_timeout_seconds * 1000)),
                    "security.protocol": self._config.security_protocol,
                }
            if self._config.sasl_username or self._config.sasl_password:
                producer_config.update({
                    "sasl.mechanisms": "PLAIN",
                    "sasl.username": self._config.sasl_username,
                    "sasl.password": self._config.sasl_password,
                })
            self._dlq_producer = Producer(producer_config)

    @staticmethod
    def _decode(message: Any) -> dict[str, Any]:
        raw = message.value()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("schema_version") != "evaluation-job-queued.v1":
            raise PermanentEvaluationError("unsupported event schema")
        event_id = uuid.UUID(str(payload["event_id"]))
        job_id = uuid.UUID(str(payload["job_id"]))
        key = message.key()
        if isinstance(key, bytes):
            key = key.decode("ascii")
        if key != str(job_id):
            raise PermanentEvaluationError("message key is not job_id")
        return {"payload": payload, "event_id": event_id, "job_id": job_id}

    def _commit(self, message: Any) -> None:
        try:
            self._commit_checked(message)
        except Exception:
            self._observe("observe_offset_commit", "failure")
            raise
        self._observe("observe_offset_commit", "success")

    def _commit_checked(self, message: Any) -> None:
        # Explicit synchronous broker acknowledgement; no auto commit/store.
        if self._partition_lost:
            raise RuntimeError("Kafka partition lost before offset commit")
        result = self._consumer.commit(message=message, asynchronous=False)
        if not result:
            raise RuntimeError("Kafka offset commit returned no partition acknowledgement")
        for partition in result if isinstance(result, (list, tuple)) else (result,):
            error = getattr(partition, "error", None)
            if callable(error):
                error = error()
            if error is not None:
                raise RuntimeError("Kafka offset commit failed")

    def _publish_dlq_sync(self, message: Any, failure: BaseException) -> None:
        payload: dict[str, Any] = {"schema_version": "evaluation-job-dlq.v1"}
        try:
            raw = message.value()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            original = json.loads(raw)
            if isinstance(original, dict):
                schema_version = original.get("schema_version")
                # schema_version 也是不可信正文；只保留有界的协议版本标识。
                if (
                    isinstance(schema_version, str)
                    and re.fullmatch(r"evaluation-job-queued\.v[0-9]{1,6}", schema_version)
                ) or (type(schema_version) is int and 0 <= schema_version <= 2_147_483_647):
                    payload["original_schema_version"] = schema_version
                for field_name in ("event_id", "job_id"):
                    try:
                        payload[field_name] = str(uuid.UUID(str(original[field_name])))
                    except (KeyError, TypeError, ValueError, AttributeError):
                        pass
        except Exception:
            pass
        payload.update(
            {
                "original_topic": message.topic(),
                "original_partition": int(message.partition()),
                "original_offset": int(message.offset()),
                "failure_code": type(failure).__name__,
            }
        )
        ack_error: list[BaseException] = []
        acknowledged = False

        def callback(error: Any, _msg: Any) -> None:
            nonlocal acknowledged
            if error is not None:
                ack_error.append(RuntimeError(str(error)))
            else:
                acknowledged = True

        self._dlq_producer.produce(
            self._config.dlq_topic,
            key=str(payload.get("event_id", "unknown")).encode("ascii"),
            value=json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            callback=callback,
        )
        if self._dlq_producer.flush(self._config.dlq_timeout_seconds):
            raise TimeoutError("DLQ broker delivery ACK timeout")
        if ack_error:
            raise ack_error[0]
        if not acknowledged:
            raise RuntimeError("DLQ delivery callback did not confirm ACK")

    async def _publish_dlq(self, message: Any, failure: BaseException) -> None:
        reason = self._dlq_reason(failure)
        try:
            await await_kafka_io(self._publish_dlq_sync, message, failure)
        except Exception:
            self._observe_dlq("failed", reason)
            raise
        self._observe_dlq("acked", reason)

    @staticmethod
    def _dlq_reason(failure: BaseException) -> str:
        safe = str(failure).lower()
        if "authority mismatch" in safe:
            return "intent_mismatch"
        if "key" in safe or "uuid" in safe or "identity" in safe:
            return "invalid_identity"
        if "unsupported" in safe:
            return "unsupported_event"
        return "invalid_schema"

    def _observe(self, operation: str, *args) -> None:
        try:
            if self._observability is not None:
                getattr(self._observability, operation)(*args)
        except Exception:
            pass

    def _observe_dlq(self, outcome: str, reason: str) -> None:
        try:
            if self._observability is not None:
                self._observability.observe_dlq(outcome=outcome, reason=reason)
        except Exception:
            pass

    def _log_context(self) -> dict[str, str | None]:
        try:
            if self._observability is not None:
                return self._observability.correlated_log_fields()
        except Exception:
            pass
        return {"trace_id": None, "span_id": None}

    @staticmethod
    def _trace_headers(message: Any) -> dict[str, str]:
        try:
            raw_headers = message.headers() or ()
        except Exception:
            return {}
        headers: dict[str, str] = {}
        for key, value in raw_headers:
            if key not in {"traceparent", "tracestate"} or value is None:
                continue
            if isinstance(value, bytes):
                try:
                    value = value.decode("ascii")
                except UnicodeDecodeError:
                    continue
            if isinstance(value, str) and len(value) <= 512:
                headers[key] = value
        return headers

    async def process_message(self, message: Any) -> str:
        started_at = time.perf_counter()
        with fail_open_span(
            lambda: self._observability.start_messaging_span(
                "kafka consume/process",
                carrier=self._trace_headers(message),
                kind=SpanKind.CONSUMER,
                attributes={"messaging.system": "kafka", "messaging.operation": "process"},
            )
            if self._observability is not None
            else nullcontext(None)
        ) as span:
            try:
                outcome = await self._process_message(message)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                if span is not None:
                    span.set_status(Status(StatusCode.ERROR))
                self._observe("observe_worker", "timeout", time.perf_counter() - started_at)
                self._observe("observe_kafka_consumer", "failed")
                logger.warning(
                    "Evaluation worker timed out",
                    extra={"component": "evaluation_worker", "status": "TIMEOUT", **self._log_context()},
                )
                raise
            except Exception:
                if span is not None:
                    span.set_status(Status(StatusCode.ERROR))
                self._observe("observe_worker", "failed", time.perf_counter() - started_at)
                self._observe("observe_kafka_consumer", "failed")
                logger.warning(
                    "Evaluation worker processing failed",
                    extra={"component": "evaluation_worker", "status": "FAILED", **self._log_context()},
                )
                raise
            consumer_outcome = {
                "SUCCEEDED": "processed",
                "NOOP": "duplicate",
                "CANCELLED_NOOP": "cancelled_noop",
                "FAILED": "failed",
                "DLQ": "dlq",
            }[outcome]
            worker_outcome = {
                "SUCCEEDED": "success",
                "NOOP": "duplicate",
                "CANCELLED_NOOP": "cancelled_noop",
                "FAILED": "failed",
                "DLQ": "failed",
            }[outcome]
            self._observe("observe_kafka_consumer", consumer_outcome)
            self._observe("observe_worker", worker_outcome, time.perf_counter() - started_at)
            logger.info(
                "Evaluation worker message processed",
                extra={
                    "component": "evaluation_worker",
                    "status": outcome,
                    "trace_outcome": consumer_outcome,
                    **self._log_context(),
                },
            )
            # 对外保留 WP5 的 NOOP Contract；内部细分只用于有界指标与日志。
            return "NOOP" if outcome == "CANCELLED_NOOP" else outcome

    async def _process_message(self, message: Any) -> str:
        """Process one message; return outcome and leave transient failures uncommitted."""
        if self._dlq_producer is None:
            self._ensure_clients()
        try:
            decoded = self._decode(message)
        except Exception as exc:
            await self._publish_dlq(message, exc)
            await await_kafka_io(self._commit, message)
            return "DLQ"

        event_id = decoded["event_id"]
        job_id = decoded["job_id"]
        topic = message.topic()
        partition = int(message.partition())
        offset = int(message.offset())
        if not await self._jobs.validate_queued_event(
            event_id=event_id, job_id=job_id, payload=decoded["payload"]
        ):
            await self._publish_dlq(message, PermanentEvaluationError("event authority mismatch"))
            await await_kafka_io(self._commit, message)
            return "DLQ"
        try:
            claim = await self._jobs.claim_for_worker(
                job_id,
                claim_owner=self._worker_id,
                lease_seconds=self._config.job_lease_seconds,
            )
        except JobError as exc:
            await self._publish_dlq(message, exc)
            await await_kafka_io(self._commit, message)
            return "DLQ"
        if claim is None:
            current = await self._jobs.get(job_id)
            try:
                await self._jobs.record_processed_event_noop(
                    job_id=job_id,
                    consumer_name=self._config.consumer_group,
                    event_id=event_id,
                    topic=topic,
                    partition=partition,
                    offset=offset,
                )
            except JobError as exc:
                if exc.code.value == "JOB_NOT_FOUND":
                    await self._publish_dlq(message, exc)
                    await await_kafka_io(self._commit, message)
                    return "DLQ"
                self._observe("observe_worker_claim", "busy")
                raise
            self._observe("observe_worker_claim", "terminal")
            await await_kafka_io(self._commit, message)
            return (
                "CANCELLED_NOOP"
                if getattr(current.status, "value", current.status) == "CANCELLED"
                else "NOOP"
            )

        attempt = getattr(claim.job, "attempt", 1)
        self._observe(
            "observe_worker_claim", "reclaimed" if attempt > 1 else "claimed"
        )
        if attempt == 1:
            self._observe("observe_job_transition", "QUEUED", "RUNNING")

        try:
            # Evaluation owns its own retry policy.  The Kafka worker executes it
            # once; infrastructure failure leaves the offset uncommitted so the
            # broker redelivers the message after process restart/recovery.
            with fail_open_span(
                lambda: self._observability.start_messaging_span(
                    "evaluation execution",
                    carrier=None,
                    kind=SpanKind.INTERNAL,
                    attributes={"component": "evaluation_worker", "operation": "evaluate"},
                )
                if self._observability is not None
                else nullcontext(None)
            ):
                async with asyncio.timeout(self._config.max_evaluation_seconds):
                    result = await self._executor.execute(claim.job)
            finalization = await self._jobs.finalize_worker_success(
                job_id=job_id,
                claim_owner=claim.claim_owner,
                claim_token=claim.claim_token,
                consumer_name=self._config.consumer_group,
                event_id=event_id,
                topic=topic,
                partition=partition,
                offset=offset,
                result_payload=result,
            )
            if finalization.idempotent or not finalization.applied:
                self._observe("observe_job_finalization", "duplicate")
            else:
                self._observe("observe_job_transition", "RUNNING", "SUCCEEDED")
                self._observe("observe_job_finalization", "success")
            await await_kafka_io(self._commit, message)
            return "SUCCEEDED"
        except PermanentEvaluationError as exc:
            await self._jobs.record_worker_failure(
                job_id=job_id,
                claim_owner=claim.claim_owner,
                claim_token=claim.claim_token,
                consumer_name=self._config.consumer_group,
                event_id=event_id,
                topic=topic,
                partition=partition,
                offset=offset,
                failure_code="EVALUATION_INPUT_INVALID",
                failure_message=str(exc)[:512],
            )
            self._observe("observe_job_transition", "RUNNING", "FAILED")
            self._observe("observe_job_finalization", "failed")
            await await_kafka_io(self._commit, message)
            return "FAILED"
        except asyncio.CancelledError:
            raise

    async def run(self, stop_event: asyncio.Event) -> None:
        self._ensure_clients()
        try:
            self._consumer.subscribe(
                [self._config.topic],
                on_assign=self._on_assign,
                on_revoke=self._on_revoke,
                on_lost=self._on_lost,
            )
        except TypeError:  # narrow compatibility seam for injected test consumers
            self._consumer.subscribe([self._config.topic])
        try:
            while not stop_event.is_set():
                message = await await_kafka_io(self._consumer.poll, self._config.poll_timeout_seconds)
                if message is None:
                    continue
                error = message.error()
                if error is not None:
                    # Partition EOF is normal; other errors retain group progress unchanged.
                    if getattr(error, "code", lambda: None)() == -191:  # KafkaError._PARTITION_EOF
                        continue
                    logger.warning("Kafka poll failed", extra={"component": "evaluation_worker"})
                    continue
                try:
                    await self.process_message(message)
                except Exception:
                    # Do not poll/commit a later offset over an uncommitted message.
                    logger.warning(
                        "Kafka worker stopped after uncommitted message",
                        extra={"component": "evaluation_worker", "status": "STOPPED"},
                    )
                    raise RuntimeError("Kafka worker stopped with an uncommitted message") from None
        finally:
            if self._consumer is not None:
                await await_kafka_io(self._consumer.close)


__all__ = [
    "EvaluationExecutor",
    "KafkaEvaluationWorker",
    "KafkaWorkerConfig",
    "PermanentEvaluationError",
    "RuntimeEvaluationExecutor",
]
