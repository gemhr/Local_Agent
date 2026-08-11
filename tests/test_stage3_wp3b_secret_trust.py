from __future__ import annotations

import io
from dataclasses import replace
from datetime import UTC, datetime

import pytest
import requests

from core.llm_engine import RemoteLLMEngine
from core.runtime import (
    ChatStreamCompatibilityAdapter,
    ErrorPayload,
    InMemoryMetricsRecorder,
    JournalRecord,
    JsonStructuredRuntimeLogger,
    RuntimeEvent,
    RuntimeEventType,
    RuntimeMetricsProjector,
    StructuredLogProjector,
)
from core.runtime.health import resolve_application_diagnostic
from core.runtime.model_invocation import GeneratorModelAdapter, ModelAdapterInvocationError
from core.runtime.tracing import InMemorySpanRecorder
from core.settings import EnvironmentProfile, Settings, SettingsValidationError


MARKER = "WP3B_TEST_SECRET_A91F"


class _Response:
    def __init__(self, status_code: int, payload=None, *, json_error: Exception | None = None):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error
        self.text = MARKER

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class _Session:
    def __init__(self, outcome):
        self.outcome = outcome
        self.trust_env = None
        self.authorization_constructed = False

    def mount(self, *args, **kwargs):
        return None

    def post(self, *args, **kwargs):
        self.authorization_constructed = kwargs["headers"].get("Authorization") == f"Bearer {MARKER}"
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    def close(self):
        return None


def _safe_projections(error: ModelAdapterInvocationError) -> tuple[str, ...]:
    event = RuntimeEvent(
        schema_version=1,
        event_id="event-1",
        run_id="run-1",
        trace_id="trace-1",
        sequence=1,
        event_type=RuntimeEventType.ERROR,
        emitted_at=datetime.now(UTC),
        component="model_invocation",
        payload=ErrorPayload(
            error.safe_error_code,
            str(error),
            "model_invocation",
            False,
        ),
    )
    record = JournalRecord.from_event(event)

    log_stream = io.StringIO()
    StructuredLogProjector(JsonStructuredRuntimeLogger(log_stream)).project(record)

    metrics = InMemoryMetricsRecorder()
    RuntimeMetricsProjector(metrics).project(record)

    spans = InMemorySpanRecorder(metrics_recorder=metrics)
    handle = spans.start_span(
        trace_id="trace-1",
        run_id="run-1",
        component="model_attempt",
        operation="attempt",
    )
    handle.end_error(error.safe_error_code)

    chunk = ChatStreamCompatibilityAdapter().adapt(event)
    health = resolve_application_diagnostic(None, fallback_lifecycle=None).to_safe_dict()
    return (
        repr(error),
        str(error),
        repr(event),
        repr(record),
        repr(record.safe_payload),
        log_stream.getvalue(),
        repr(metrics.snapshot()),
        repr(spans.snapshot()),
        repr(chunk),
        repr(health),
    )


@pytest.mark.parametrize(
    "outcome",
    [
        _Response(401, {"error": MARKER}),
        _Response(403, {"error": MARKER}),
        requests.Timeout("timeout " + MARKER),
        _Response(500, {"error": MARKER}),
        _Response(200, json_error=ValueError("malformed " + MARKER)),
    ],
    ids=("401", "403", "timeout", "500", "malformed"),
)
def test_provider_failures_keep_secret_out_of_all_safe_projections(outcome) -> None:
    session = _Session(outcome)
    engine = RemoteLLMEngine(
        "https://provider.invalid",
        "example-model",
        api_key=MARKER,
        session=session,
        trust_env=False,
    )
    with pytest.raises(ModelAdapterInvocationError) as captured:
        GeneratorModelAdapter(engine).invoke(
            [{"role": "user", "content": "safe"}],
            max_tokens=8,
        )
    assert session.authorization_constructed
    assert all(MARKER not in projection for projection in _safe_projections(captured.value))


def test_settings_credentials_remain_repr_safe() -> None:
    settings = Settings.load()
    marked = replace(settings, remote_api_key=MARKER, wiki_cookie=MARKER)
    assert MARKER not in repr(marked)
    assert MARKER not in str(marked)


def _load_profile(monkeypatch, tmp_path, profile: str, **overrides) -> Settings:
    keys = (
        "LOCAL_AGENT_ENVIRONMENT_PROFILE",
        "LOCAL_AGENT_ENVIRONMENT_ID",
        "LOCAL_AGENT_TOOL_ALLOWED_READ_ROOTS",
        "LOCAL_AGENT_API_HOST",
        "LOCAL_AGENT_API_BASE_URL",
        "LOCAL_AGENT_REMOTE_TRUST_ENV",
    )
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LOCAL_AGENT_ENVIRONMENT_PROFILE", profile)
    if profile == "PRODUCTION":
        monkeypatch.setenv("LOCAL_AGENT_ENVIRONMENT_ID", "wp3b-prod")
        monkeypatch.setenv("LOCAL_AGENT_TOOL_ALLOWED_READ_ROOTS", str(tmp_path))
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    return Settings.load()


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_production_numeric_loopback_remains_allowed(monkeypatch, tmp_path, host: str) -> None:
    settings = _load_profile(
        monkeypatch,
        tmp_path,
        "PRODUCTION",
        LOCAL_AGENT_API_HOST=host,
    )
    assert settings.environment_profile is EnvironmentProfile.PRODUCTION


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.2", "example.test", "localhost"])
def test_production_non_loopback_and_hostname_remain_denied(monkeypatch, tmp_path, host: str) -> None:
    with pytest.raises(SettingsValidationError):
        _load_profile(
            monkeypatch,
            tmp_path,
            "PRODUCTION",
            LOCAL_AGENT_API_HOST=host,
        )


def test_remote_trust_env_defaults_and_explicit_operator_choice(monkeypatch, tmp_path) -> None:
    assert _load_profile(monkeypatch, tmp_path, "TEST").remote_trust_env is False
    assert _load_profile(monkeypatch, tmp_path, "PRODUCTION").remote_trust_env is False
    assert (
        _load_profile(
            monkeypatch,
            tmp_path,
            "PRODUCTION",
            LOCAL_AGENT_REMOTE_TRUST_ENV="true",
        ).remote_trust_env
        is True
    )


def test_agent_id_remains_routing_field_not_authentication_contract() -> None:
    fields = set(__import__("server").ChatRequest.model_fields)
    assert "agent_id" in fields
    assert not ({"user_id", "principal", "tenant_id", "authorization"} & fields)
