"""WP6 首阶段：进程级 Observability Owner、HTTP 指标与 OTel 基础。"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client.parser import text_string_to_metric_families
import pytest

from core.observability import HttpObservabilityMiddleware, ObservabilityService, fail_open_span
from core.settings import Settings, SettingsValidationError


def _settings(**changes) -> Settings:
    return replace(Settings.load(), **changes)


def _test_app(service: ObservabilityService) -> FastAPI:
    app = FastAPI()
    app.state.observability_service = service
    app.add_middleware(HttpObservabilityMiddleware)

    @app.get("/jobs/{job_id}")
    async def job(job_id: str):
        return {"job_id": job_id}

    return app


def test_private_registry_allows_multiple_process_owners() -> None:
    first = ObservabilityService(_settings(), process_role="api")
    second = ObservabilityService(_settings(), process_role="publisher")
    assert first.registry is not second.registry
    assert b"localagent_http_server_requests_total" in first.render_metrics().body
    assert b"localagent_http_server_requests_total" in second.render_metrics().body


def test_http_metrics_use_route_template_and_bounded_labels() -> None:
    service = ObservabilityService(_settings(), process_role="api")
    client = TestClient(_test_app(service))
    for index in range(100):
        response = client.get(
            f"/jobs/job-{index}", headers={"X-Request-ID": f"request-{index}"}
        )
        assert response.status_code == 200

    families = list(
        text_string_to_metric_families(service.render_metrics().body.decode())
    )
    family = next(
        item for item in families if item.name == "localagent_http_server_requests"
    )
    samples = [sample for sample in family.samples if sample.name.endswith("_total")]
    assert len(samples) == 1
    assert samples[0].labels == {
        "method": "GET",
        "route_template": "/jobs/{job_id}",
        "status_class": "2xx",
    }
    rendered = service.render_metrics().body.decode()
    assert "job-99" not in rendered
    assert "request-99" not in rendered


def test_w3c_parent_context_and_real_sdk_export() -> None:
    exporter = InMemorySpanExporter()
    service = ObservabilityService(
        _settings(tracing_enabled=True, otel_trace_sample_ratio=1.0),
        process_role="api",
        span_exporter=exporter,
    )
    provider = service._tracer_provider
    assert provider is not None

    trace_id = "0af7651916cd43dd8448eb211c80319c"
    headers = {"traceparent": f"00-{trace_id}-b7ad6b7169203331-01"}
    with service.start_server_span(headers, "GET") as span:
        service.finish_server_span(span, "/jobs/{job_id}", 200)

    finished = exporter.get_finished_spans()
    assert len(finished) == 1
    assert f"{finished[0].context.trace_id:032x}" == trace_id
    assert finished[0].attributes == {
        "http.request.method": "GET",
        "http.route": "/jobs/{job_id}",
        "http.response.status_code": 200,
    }
    asyncio.run(service.close())


def test_invalid_observability_settings_fail_closed(monkeypatch) -> None:
    for invalid_path in ("metrics/{user_id}", "/api/metrics", "/health"):
        monkeypatch.setenv("LOCAL_AGENT_METRICS_PATH", invalid_path)
        try:
            Settings.load()
        except SettingsValidationError as exc:
            assert exc.field == "LOCAL_AGENT_METRICS_PATH"
        else:  # pragma: no cover - 防止错误配置被静默接受
            raise AssertionError("invalid metrics path was accepted")


def test_span_helper_failure_is_fail_open_and_preserves_business_error() -> None:
    executed: list[str] = []

    def broken_factory():
        raise RuntimeError("tracing start failed")

    class BrokenExit:
        def __enter__(self):
            return None

        def __exit__(self, *_args):
            raise RuntimeError("tracing close failed")

    with fail_open_span(broken_factory):
        executed.append("start")
    with fail_open_span(BrokenExit):
        executed.append("close")
    with pytest.raises(ValueError, match="business failure"):
        with fail_open_span(BrokenExit):
            raise ValueError("business failure")
    assert executed == ["start", "close"]


def test_server_metrics_endpoint_is_real_and_bypasses_end_user_auth() -> None:
    import server

    service = ObservabilityService(_settings(), process_role="api")
    server.app.state.observability_service = service
    response = TestClient(server.app).get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=")
    assert response.headers["content-type"].endswith("; charset=utf-8")
    assert "localagent_http_server_request_duration_seconds" in response.text

    server.app.state.observability_service = ObservabilityService(
        _settings(metrics_enabled=False), process_role="api"
    )
    assert TestClient(server.app).get("/metrics").status_code == 404
