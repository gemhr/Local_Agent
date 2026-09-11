#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WP6 bounded、read-only、按进程能力聚合的依赖 readiness。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
import time
from typing import Any, Awaitable, Callable

from core.kafka_event_sink import validate_kafka_topic


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    component: str
    status: HealthStatus
    reason_code: str
    latency_ms: int

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "reason_code": self.reason_code,
            "latency_ms": self.latency_ms,
        }


@dataclass(frozen=True, slots=True)
class ProcessReadiness:
    ready: bool
    components: tuple[ComponentHealth, ...]

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "status": "ready" if self.ready else "not_ready",
            "components": {
                item.component: item.to_safe_dict() for item in self.components
            },
        }


class ComponentReadinessService:
    """只读 dependency checks；结果是诊断快照，不写回业务或 lifecycle。"""

    def __init__(
        self,
        database: Any,
        settings: Any,
        *,
        redis_client: Any = None,
        redis_cache_client: Any = None,
        redis_limiter_client: Any = None,
    ) -> None:
        self._database = database
        self._settings = settings
        self._redis_cache = (
            redis_cache_client if redis_cache_client is not None else redis_client
        )
        self._redis_limiter = (
            redis_limiter_client if redis_limiter_client is not None else redis_client
        )
        self._timeout = settings.health_check_timeout_seconds

    async def _check(
        self,
        component: str,
        operation: Callable[[], Awaitable[None]],
        *,
        failure_status: HealthStatus = HealthStatus.UNAVAILABLE,
    ) -> ComponentHealth:
        started_at = time.perf_counter()
        try:
            async with asyncio.timeout(self._timeout):
                await operation()
        except TimeoutError:
            return ComponentHealth(
                component, failure_status, "timeout", int((time.perf_counter() - started_at) * 1000)
            )
        except Exception:
            return ComponentHealth(
                component, failure_status, "dependency_unavailable",
                int((time.perf_counter() - started_at) * 1000),
            )
        return ComponentHealth(
            component, HealthStatus.HEALTHY, "ok",
            int((time.perf_counter() - started_at) * 1000),
        )

    async def _postgresql(self) -> ComponentHealth:
        return await self._check("postgresql", self._database.verify_reachable)

    @staticmethod
    async def _redis_ping(client: Any) -> None:
        if client is None:
            raise RuntimeError("redis client unavailable")
        await client.ping()

    async def _kafka(self) -> ComponentHealth:
        async def check() -> None:
            if not self._settings.kafka_enabled:
                raise RuntimeError("kafka disabled")
            await validate_kafka_topic(
                self._settings.kafka_bootstrap_servers,
                self._settings.kafka_job_topic,
                timeout_seconds=min(self._timeout, self._settings.kafka_poll_timeout_seconds),
                security_protocol=self._settings.kafka_security_protocol,
                sasl_username=self._settings.kafka_sasl_username,
                sasl_password=self._settings.kafka_sasl_password,
            )

        return await self._check("kafka", check)

    async def check_api(self, *, runtime_ready: bool = True) -> ProcessReadiness:
        async def disabled(component: str) -> ComponentHealth:
            return ComponentHealth(component, HealthStatus.HEALTHY, "disabled", 0)

        postgresql, limiter, cache = await asyncio.gather(
            self._postgresql(),
            (
                self._check(
                    "redis_limiter", lambda: self._redis_ping(self._redis_limiter)
                )
                if self._settings.rate_limit_enabled
                else disabled("redis_limiter")
            ),
            (
                self._check(
                    "redis_cache",
                    lambda: self._redis_ping(self._redis_cache),
                    failure_status=HealthStatus.DEGRADED,
                )
                if self._settings.rag_cache_enabled
                else disabled("redis_cache")
            ),
        )
        runtime = ComponentHealth(
            "runtime",
            HealthStatus.HEALTHY if runtime_ready else HealthStatus.UNAVAILABLE,
            "ok" if runtime_ready else "not_accepting",
            0,
        )
        ready = (
            runtime_ready
            and postgresql.status is HealthStatus.HEALTHY
            and limiter.status is HealthStatus.HEALTHY
        )
        return ProcessReadiness(ready, (postgresql, limiter, cache, runtime))

    async def check_publisher(self) -> ProcessReadiness:
        postgresql, kafka = await asyncio.gather(self._postgresql(), self._kafka())
        return ProcessReadiness(
            postgresql.status is HealthStatus.HEALTHY and kafka.status is HealthStatus.HEALTHY,
            (postgresql, kafka),
        )

    async def check_worker(self) -> ProcessReadiness:
        return await self.check_publisher()


__all__ = [
    "ComponentHealth",
    "ComponentReadinessService",
    "HealthStatus",
    "ProcessReadiness",
]
