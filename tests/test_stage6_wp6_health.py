"""WP6 component-aware readiness 合同。"""

from __future__ import annotations

from dataclasses import replace
import asyncio
import json
import time

import pytest

from core.health_checks import ComponentReadinessService, HealthStatus
from core.settings import Settings
from scripts import healthcheck


class _Database:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error

    async def verify_reachable(self) -> None:
        if self.error is not None:
            raise self.error


class _Redis:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error

    async def ping(self) -> None:
        if self.error is not None:
            raise self.error


def _settings(**changes):
    return replace(Settings.load(), kafka_enabled=True, **changes)


@pytest.mark.asyncio
async def test_api_readiness_uses_required_capabilities_not_kafka(monkeypatch) -> None:
    async def kafka_must_not_run(*_args, **_kwargs):
        raise AssertionError("API readiness must not query Kafka")

    monkeypatch.setattr("core.health_checks.validate_kafka_topic", kafka_must_not_run)
    service = ComponentReadinessService(
        _Database(),
        _settings(),
        redis_cache_client=_Redis(RuntimeError("cache down")),
        redis_limiter_client=_Redis(),
    )
    result = await service.check_api()
    assert result.ready
    components = {item.component: item for item in result.components}
    assert components["postgresql"].status is HealthStatus.HEALTHY
    assert components["redis_cache"].status is HealthStatus.DEGRADED
    assert components["redis_limiter"].status is HealthStatus.HEALTHY

    unavailable = ComponentReadinessService(
        _Database(), _settings(), redis_client=_Redis(RuntimeError("redis down"))
    )
    result = await unavailable.check_api()
    components = {item.component: item for item in result.components}
    assert not result.ready
    assert components["redis_cache"].status is HealthStatus.DEGRADED
    assert components["redis_limiter"].status is HealthStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_publisher_and_worker_require_pg_and_kafka(monkeypatch) -> None:
    async def kafka_ok(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr("core.health_checks.validate_kafka_topic", kafka_ok)
    service = ComponentReadinessService(_Database(), _settings())
    assert (await service.check_publisher()).ready
    assert (await service.check_worker()).ready

    pg_down = ComponentReadinessService(_Database(RuntimeError("pg down")), _settings())
    assert not (await pg_down.check_worker()).ready

    async def kafka_down(*_args, **_kwargs) -> None:
        raise RuntimeError("broker down")

    monkeypatch.setattr("core.health_checks.validate_kafka_topic", kafka_down)
    assert not (await service.check_publisher()).ready


@pytest.mark.asyncio
async def test_health_timeout_is_bounded_and_response_is_safe(monkeypatch) -> None:
    async def hanging_kafka(*_args, **_kwargs) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr("core.health_checks.validate_kafka_topic", hanging_kafka)
    service = ComponentReadinessService(
        _Database(), _settings(health_check_timeout_seconds=0.02)
    )
    started_at = time.perf_counter()
    result = await service.check_publisher()
    assert time.perf_counter() - started_at < 0.2
    assert not result.ready
    body = result.to_safe_dict()
    assert body["components"]["kafka"]["reason_code"] == "timeout"
    text = str(body).lower()
    for forbidden in ("password", "credential", "exception", "bootstrap", "dsn"):
        assert forbidden not in text


@pytest.mark.asyncio
async def test_healthcheck_initialization_failure_has_safe_output(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        healthcheck.Settings,
        "load",
        staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("secret DSN password"))),
    )
    assert await healthcheck._run("api") == 1
    output = capsys.readouterr()
    assert output.err == ""
    body = json.loads(output.out)
    assert body["status"] == "not_ready"
    assert body["components"]["healthcheck"]["reason_code"] == "initialization_failed"
    assert "secret" not in output.out.lower()
    assert "password" not in output.out.lower()


@pytest.mark.asyncio
async def test_api_healthcheck_reads_runtime_aware_readyz(monkeypatch, capsys) -> None:
    async def ready(_settings):
        return True, {
            "status": "ready",
            "components": {
                "api": {"status": "healthy", "reason_code": "ok", "latency_ms": 1}
            },
        }

    monkeypatch.setattr(healthcheck, "_probe_api_readiness", ready)
    monkeypatch.setattr(
        healthcheck,
        "Database",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("API healthcheck must use /readyz")
        ),
    )
    assert await healthcheck._run("api") == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"
