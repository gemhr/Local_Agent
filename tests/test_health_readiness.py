"""WP1-C Health / Readiness 纯投影、Authority 不可变、ASGI 集成与安全测试。

覆盖：
- pure projector 全矩阵（STARTING / READY / READY_DEGRADED / DRAINING /
  CLOSED / UNAVAILABLE）；
- resolver/projector 不修改 lifecycle / admission / StartupDependencySnapshot；
- RuntimeLifecycleState 仍恰为 4 值；ApplicationRuntimeServices 无 is_ready /
  mutable health manager；
- 真实 ASGI GET /health、GET /readyz 的 route / status / exact body；
- alias 负向（/healthz /ready /status /metadata /version 均 404）；
- 安全 schema（exact keys，无 secret/path/exception marker）。
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.runtime.admission import RuntimeAdmissionGate, RuntimeAdmissionState
from core.runtime.application_services import (
    ApplicationRuntimeServices,
    RuntimeLifecycleState,
    StartupDependencySnapshot,
)
from core.runtime.health import (
    ApplicationDiagnosticSnapshot,
    DiagnosticStatus,
    health_http_status,
    readiness_http_status,
    resolve_application_diagnostic,
)


def _make_services(
    *,
    lifecycle: RuntimeLifecycleState = RuntimeLifecycleState.READY,
    admission: RuntimeAdmissionState = RuntimeAdmissionState.ACCEPTING,
    kb_degraded: bool = False,
) -> ApplicationRuntimeServices:
    gate = RuntimeAdmissionGate()
    if admission is RuntimeAdmissionState.DRAINING:
        gate.close_admission()
    elif admission is RuntimeAdmissionState.CLOSED:
        gate.close_admission()
        gate.mark_closed()
    return ApplicationRuntimeServices(
        event_journal=object(),
        observability_dispatcher=object(),
        structured_logger=object(),
        runtime_metrics_recorder=object(),
        span_recorder=object(),
        snapshot_store=None,
        recovery_validator=None,
        model_invocation_router=object(),
        tool_execution_service=object(),
        retrieval_execution_service=None,
        blocking_executors=(),
        worker_trackers=(),
        run_registry=object(),
        admission_gate=gate,
        startup_dependency_snapshot=StartupDependencySnapshot(
            knowledge_base_degraded=kb_degraded
        ),
    )


def _set_lifecycle(services: ApplicationRuntimeServices, state) -> None:
    # 测试辅助：直接驱动 _LifecycleControl（生产只经 begin_shutdown/close）。
    if state is RuntimeLifecycleState.SHUTTING_DOWN:
        services.begin_shutdown()
    elif state is RuntimeLifecycleState.CLOSED:
        services.begin_shutdown()
        services._lifecycle.state = RuntimeLifecycleState.CLOSED


# ---------------------------------------------------------------------------
# Pure projector matrix
# ---------------------------------------------------------------------------


def test_pre_services_starting_fallback() -> None:
    snapshot = resolve_application_diagnostic(
        None,
        fallback_lifecycle=RuntimeLifecycleState.STARTING,
    )
    assert snapshot.status is DiagnosticStatus.STARTING
    assert snapshot.lifecycle is RuntimeLifecycleState.STARTING
    # RuntimeAdmissionState 只有 ACCEPTING/DRAINING/CLOSED；
    # pre-services 阶段以安全字符串 UNAVAILABLE 表达。
    assert snapshot.admission == "UNAVAILABLE"
    assert snapshot.degraded is False
    assert health_http_status(snapshot) == 200
    assert readiness_http_status(snapshot) == 503


def test_ready_accepting_healthy_kb() -> None:
    services = _make_services()
    snapshot = resolve_application_diagnostic(services)
    assert snapshot.status is DiagnosticStatus.READY
    assert snapshot.lifecycle is RuntimeLifecycleState.READY
    assert snapshot.admission is RuntimeAdmissionState.ACCEPTING
    assert snapshot.degraded is False
    assert health_http_status(snapshot) == 200
    assert readiness_http_status(snapshot) == 200


def test_ready_accepting_degraded_kb() -> None:
    services = _make_services(kb_degraded=True)
    snapshot = resolve_application_diagnostic(services)
    assert snapshot.status is DiagnosticStatus.READY_DEGRADED
    assert snapshot.lifecycle is RuntimeLifecycleState.READY
    assert snapshot.admission is RuntimeAdmissionState.ACCEPTING
    assert snapshot.degraded is True
    assert health_http_status(snapshot) == 200
    assert readiness_http_status(snapshot) == 200


def test_ready_draining() -> None:
    services = _make_services(admission=RuntimeAdmissionState.DRAINING)
    snapshot = resolve_application_diagnostic(services)
    assert snapshot.status is DiagnosticStatus.DRAINING
    assert snapshot.lifecycle is RuntimeLifecycleState.READY
    assert snapshot.admission is RuntimeAdmissionState.DRAINING
    assert health_http_status(snapshot) == 200
    assert readiness_http_status(snapshot) == 503


def test_shutting_down_draining() -> None:
    services = _make_services(admission=RuntimeAdmissionState.DRAINING)
    _set_lifecycle(services, RuntimeLifecycleState.SHUTTING_DOWN)
    snapshot = resolve_application_diagnostic(services)
    assert snapshot.status is DiagnosticStatus.DRAINING
    assert snapshot.lifecycle is RuntimeLifecycleState.SHUTTING_DOWN
    assert snapshot.admission is RuntimeAdmissionState.DRAINING
    assert health_http_status(snapshot) == 200
    assert readiness_http_status(snapshot) == 503


def test_closed_closed() -> None:
    services = _make_services(admission=RuntimeAdmissionState.CLOSED)
    _set_lifecycle(services, RuntimeLifecycleState.CLOSED)
    snapshot = resolve_application_diagnostic(services)
    assert snapshot.status is DiagnosticStatus.CLOSED
    assert snapshot.lifecycle is RuntimeLifecycleState.CLOSED
    assert snapshot.admission is RuntimeAdmissionState.CLOSED
    assert health_http_status(snapshot) == 503
    assert readiness_http_status(snapshot) == 503


def test_unavailable_no_services_no_fallback() -> None:
    snapshot = resolve_application_diagnostic(None)
    assert snapshot.status is DiagnosticStatus.UNAVAILABLE
    assert health_http_status(snapshot) == 503
    assert readiness_http_status(snapshot) == 503


def test_unavailable_unknown_fallback() -> None:
    snapshot = resolve_application_diagnostic(None, fallback_lifecycle="BOGUS")
    assert snapshot.status is DiagnosticStatus.UNAVAILABLE
    assert health_http_status(snapshot) == 503
    assert readiness_http_status(snapshot) == 503


def test_unavailable_ready_closed_inconsistent() -> None:
    services = _make_services(admission=RuntimeAdmissionState.CLOSED)
    snapshot = resolve_application_diagnostic(services)
    assert snapshot.status is DiagnosticStatus.UNAVAILABLE
    assert health_http_status(snapshot) == 503
    assert readiness_http_status(snapshot) == 503


# ---------------------------------------------------------------------------
# Authority immutability
# ---------------------------------------------------------------------------


def test_resolver_does_not_mutate_authority() -> None:
    services = _make_services(kb_degraded=True)
    lifecycle_before = services.lifecycle_state
    admission_before = services.admission_gate.state
    snapshot_before = services.startup_dependency_snapshot

    resolve_application_diagnostic(services)

    assert services.lifecycle_state is lifecycle_before
    assert services.admission_gate.state is admission_before
    assert services.startup_dependency_snapshot is snapshot_before
    assert services.startup_dependency_snapshot.knowledge_base_degraded is True


def test_runtime_lifecycle_state_has_exactly_four_values() -> None:
    values = {member.value for member in RuntimeLifecycleState}
    assert values == {"STARTING", "READY", "SHUTTING_DOWN", "CLOSED"}


def test_application_services_has_no_is_ready_or_health_manager() -> None:
    fields = {f.name for f in dataclasses.fields(ApplicationRuntimeServices)}
    assert "is_ready" not in fields
    assert not any("health" in name for name in fields)
    assert not any("ready" in name for name in fields)


def test_startup_dependency_snapshot_is_frozen() -> None:
    snapshot = StartupDependencySnapshot()
    with pytest.raises(Exception):
        snapshot.knowledge_base_degraded = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Safe serialization
# ---------------------------------------------------------------------------


def test_to_safe_dict_exact_four_fields() -> None:
    services = _make_services(kb_degraded=True)
    snapshot = resolve_application_diagnostic(services)
    body = snapshot.to_safe_dict()
    assert set(body.keys()) == {"status", "lifecycle", "admission", "degraded"}
    assert body == {
        "status": "READY_DEGRADED",
        "lifecycle": "READY",
        "admission": "ACCEPTING",
        "degraded": True,
    }


# ---------------------------------------------------------------------------
# ASGI integration
# ---------------------------------------------------------------------------


def _asgi_app(services, fallback=None) -> FastAPI:
    import server as server_module

    server_module.application_runtime_services = services
    if fallback is not None:
        server_module.app.state.runtime_lifecycle_state = fallback
    return server_module.app


def test_asgi_health_ready_ready() -> None:
    services = _make_services()
    app = _asgi_app(services)
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "READY",
        "lifecycle": "READY",
        "admission": "ACCEPTING",
        "degraded": False,
    }
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "READY",
        "lifecycle": "READY",
        "admission": "ACCEPTING",
        "degraded": False,
    }


def test_asgi_ready_degraded() -> None:
    services = _make_services(kb_degraded=True)
    app = _asgi_app(services)
    client = TestClient(app)
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "READY_DEGRADED"
    assert resp.json()["degraded"] is True


def test_asgi_draining() -> None:
    services = _make_services(admission=RuntimeAdmissionState.DRAINING)
    app = _asgi_app(services)
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "DRAINING"
    resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["status"] == "DRAINING"


def test_asgi_closed_projection() -> None:
    services = _make_services(admission=RuntimeAdmissionState.CLOSED)
    _set_lifecycle(services, RuntimeLifecycleState.CLOSED)
    app = _asgi_app(services)
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["status"] == "CLOSED"
    resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["status"] == "CLOSED"


def test_asgi_unavailable_projection() -> None:
    app = _asgi_app(None)
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["status"] == "UNAVAILABLE"
    resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["status"] == "UNAVAILABLE"


# ---------------------------------------------------------------------------
# Alias negative tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/healthz", "/ready", "/status", "/metadata", "/version"],
)
def test_alias_endpoints_do_not_exist(path: str) -> None:
    services = _make_services()
    app = _asgi_app(services)
    client = TestClient(app)
    resp = client.get(path)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Security schema
# ---------------------------------------------------------------------------


def test_health_body_has_no_forbidden_markers() -> None:
    services = _make_services(kb_degraded=True)
    app = _asgi_app(services)
    client = TestClient(app)
    resp = client.get("/health")
    body = resp.json()
    assert set(body.keys()) == {"status", "lifecycle", "admission", "degraded"}
    text = str(body).lower()
    for marker in (
        "key",
        "cookie",
        "path",
        "url",
        "exception",
        "error",
        "trace",
        "run",
        "version",
        "environment",
        "instance",
    ):
        assert marker not in text
