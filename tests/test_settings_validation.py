"""WP1-A 配置严格解析与错误 taxonomy 的 negative/integration 测试。

覆盖：strict bool/int/float、NaN/Inf、out-of-range、未知 profile/backend、
cross-field result cap、precedence、redaction（异常不泄漏 raw value/secret/
URL/path）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.settings import (
    SETTINGS_PARSE_ERROR,
    SETTINGS_SECURITY_POLICY_ERROR,
    SETTINGS_VALIDATION_ERROR,
    STARTUP_CONFIGURATION_ERROR,
    EnvironmentProfile,
    Settings,
    SettingsValidationError,
    validate_role_configuration,
)

_ENV_BASE = {
    "LOCAL_AGENT_ENVIRONMENT_PROFILE": None,
    "LOCAL_AGENT_REMOTE_API_BASE_URL": "https://example.test/v1",
    "LOCAL_AGENT_TOOL_ALLOWED_READ_ROOTS": None,
}
_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load(monkeypatch, **env):
    for key, value in _ENV_BASE.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    if (
        EnvironmentProfile.parse(os.getenv("LOCAL_AGENT_ENVIRONMENT_PROFILE"))
        is EnvironmentProfile.PRODUCTION
        and "LOCAL_AGENT_TOOL_ALLOWED_READ_ROOTS" not in env
    ):
        monkeypatch.setenv(
            "LOCAL_AGENT_TOOL_ALLOWED_READ_ROOTS", str(_PROJECT_ROOT.resolve())
        )
    return Settings.load()


# ---- canonical bool ----

@pytest.mark.parametrize("raw", ["1", "true", "True", " TRUE ", "TrUe"])
def test_strict_bool_true_variants(monkeypatch, raw) -> None:
    settings = _load(monkeypatch, LOCAL_AGENT_REMOTE_VERIFY_TLS=raw)
    assert settings.remote_verify_tls is True


@pytest.mark.parametrize("raw", ["0", "false", "False", " FALSE "])
def test_strict_bool_false_variants(monkeypatch, raw) -> None:
    settings = _load(monkeypatch, LOCAL_AGENT_REMOTE_VERIFY_TLS=raw)
    assert settings.remote_verify_tls is False


def test_strict_bool_true_parses_tls_true_regression(monkeypatch) -> None:
    """`true` 必须解析为 True（旧 `== \"1\"` 会错误变 False）。"""
    assert _load(monkeypatch, LOCAL_AGENT_REMOTE_VERIFY_TLS="true").remote_verify_tls is True


def test_strict_bool_typo_fails_closed(monkeypatch) -> None:
    with pytest.raises(SettingsValidationError) as captured:
        _load(monkeypatch, LOCAL_AGENT_REMOTE_VERIFY_TLS="tru")
    assert captured.value.safe_error_code == SETTINGS_PARSE_ERROR
    assert captured.value.field == "LOCAL_AGENT_REMOTE_VERIFY_TLS"


def test_strict_bool_empty_explicit_fails_closed(monkeypatch) -> None:
    with pytest.raises(SettingsValidationError):
        _load(monkeypatch, LOCAL_AGENT_REMOTE_VERIFY_TLS="")


# ---- int / float ----

def test_invalid_int_fails_closed(monkeypatch) -> None:
    with pytest.raises(SettingsValidationError) as captured:
        _load(monkeypatch, LOCAL_AGENT_API_PORT="not-a-port")
    assert captured.value.safe_error_code == SETTINGS_PARSE_ERROR
    assert captured.value.field == "LOCAL_AGENT_API_PORT"


def test_workers_below_minimum_fails_closed(monkeypatch) -> None:
    with pytest.raises(SettingsValidationError) as captured:
        _load(monkeypatch, LOCAL_AGENT_BLOCKING_MAX_WORKERS="0")
    assert captured.value.safe_error_code == SETTINGS_VALIDATION_ERROR
    assert captured.value.field == "LOCAL_AGENT_BLOCKING_MAX_WORKERS"


def test_pending_tasks_negative_fails_closed(monkeypatch) -> None:
    with pytest.raises(SettingsValidationError):
        _load(monkeypatch, LOCAL_AGENT_BLOCKING_MAX_PENDING_TASKS="-1")


def test_pending_tasks_zero_is_allowed(monkeypatch) -> None:
    assert _load(monkeypatch, LOCAL_AGENT_BLOCKING_MAX_PENDING_TASKS="0").blocking_max_pending_tasks == 0


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf", "Infinity"])
def test_non_finite_float_fails_closed(monkeypatch, raw) -> None:
    with pytest.raises(SettingsValidationError) as captured:
        _load(monkeypatch, RUNTIME_SHUTDOWN_GRACE_SECONDS=raw)
    assert captured.value.safe_error_code == SETTINGS_VALIDATION_ERROR
    assert captured.value.field == "RUNTIME_SHUTDOWN_GRACE_SECONDS"


def test_planning_timeout_non_positive_fails_closed(monkeypatch) -> None:
    with pytest.raises(SettingsValidationError) as captured:
        _load(monkeypatch, LOCAL_AGENT_PLANNING_TIMEOUT_SECONDS="0")
    assert captured.value.safe_error_code == SETTINGS_VALIDATION_ERROR
    assert captured.value.field == "LOCAL_AGENT_PLANNING_TIMEOUT_SECONDS"


def test_planning_timeout_nan_fails_closed(monkeypatch) -> None:
    with pytest.raises(SettingsValidationError):
        _load(monkeypatch, LOCAL_AGENT_PLANNING_TIMEOUT_SECONDS="nan")


def test_rag_score_out_of_range_fails_closed(monkeypatch) -> None:
    for raw in ("1.5", "-0.1", "1.0001"):
        with pytest.raises(SettingsValidationError):
            _load(monkeypatch, LOCAL_AGENT_RAG_MIN_SCORE=raw)


# ---- enum / profile / backend ----

def test_unknown_model_profile_fails_closed(monkeypatch) -> None:
    with pytest.raises(SettingsValidationError) as captured:
        _load(monkeypatch, LOCAL_AGENT_MODEL_PROFILE="huge")
    assert captured.value.safe_error_code == SETTINGS_VALIDATION_ERROR
    assert captured.value.field == "LOCAL_AGENT_MODEL_PROFILE"


def test_blank_model_profile_fails_closed(monkeypatch) -> None:
    with pytest.raises(SettingsValidationError):
        _load(monkeypatch, LOCAL_AGENT_MODEL_PROFILE="   ")


def test_unknown_backend_fails_closed(monkeypatch) -> None:
    with pytest.raises(SettingsValidationError) as captured:
        _load(monkeypatch, LOCAL_AGENT_LLM_BACKEND="mega")
    assert captured.value.field == "LOCAL_AGENT_LLM_BACKEND"
    assert captured.value.reason_code == "unknown_backend"


def test_blank_backend_fails_closed(monkeypatch) -> None:
    with pytest.raises(SettingsValidationError):
        _load(monkeypatch, LOCAL_AGENT_LLM_BACKEND="")


def test_unknown_environment_profile_fails_closed(monkeypatch) -> None:
    with pytest.raises(SettingsValidationError) as captured:
        _load(monkeypatch, LOCAL_AGENT_ENVIRONMENT_PROFILE="dev")
    assert captured.value.safe_error_code == SETTINGS_VALIDATION_ERROR
    assert captured.value.field == "LOCAL_AGENT_ENVIRONMENT_PROFILE"


def test_blank_environment_profile_fails_closed(monkeypatch) -> None:
    with pytest.raises(SettingsValidationError):
        _load(monkeypatch, LOCAL_AGENT_ENVIRONMENT_PROFILE="")


# ---- cross-field ----

def test_result_cap_cross_field_fails_closed(monkeypatch) -> None:
    with pytest.raises(SettingsValidationError) as captured:
        _load(
            monkeypatch,
            LOCAL_AGENT_STEP_RESULT_RUN_TOTAL_CHARS="10000",
            LOCAL_AGENT_STEP_RESULT_PER_RESULT_CHARS="20000",
        )
    assert captured.value.safe_error_code == SETTINGS_VALIDATION_ERROR
    assert captured.value.field == "LOCAL_AGENT_STEP_RESULT_RUN_TOTAL_CHARS"
    assert captured.value.reason_code == "run_total_below_per_result"


def test_result_cap_equal_run_total_is_allowed(monkeypatch) -> None:
    settings = _load(
        monkeypatch,
        LOCAL_AGENT_STEP_RESULT_RUN_TOTAL_CHARS="20000",
        LOCAL_AGENT_STEP_RESULT_PER_RESULT_CHARS="20000",
    )
    assert settings.step_result_run_total_chars == 20000


# ---- precedence ----

def test_explicit_env_overrides_code_default(monkeypatch) -> None:
    settings = _load(monkeypatch, LOCAL_AGENT_BLOCKING_MAX_WORKERS="7")
    assert settings.blocking_max_workers == 7


def test_missing_env_uses_application_defaults(monkeypatch) -> None:
    settings = _load(monkeypatch)
    assert settings.blocking_max_workers == 4
    assert settings.blocking_max_pending_tasks == 8
    assert settings.event_channel_capacity == 32
    assert settings.planning_timeout_seconds == 15.0
    assert settings.step_result_per_result_chars == 20000
    assert settings.step_result_run_total_chars == 60000
    assert settings.step_result_max_entries == 16


def test_derived_api_base_url_uses_merged_host_port(monkeypatch) -> None:
    monkeypatch.delenv("LOCAL_AGENT_API_BASE_URL", raising=False)
    settings = _load(monkeypatch, LOCAL_AGENT_API_HOST="0.0.0.0", LOCAL_AGENT_API_PORT="9000")
    assert settings.api_base_url == "http://0.0.0.0:9000"


def test_explicit_api_base_url_wins(monkeypatch) -> None:
    settings = _load(monkeypatch, LOCAL_AGENT_API_BASE_URL="http://custom:1234")
    assert settings.api_base_url == "http://custom:1234"


# ---- redaction / error taxonomy ----

def test_settings_validation_error_is_value_error_subclass() -> None:
    assert issubclass(SettingsValidationError, ValueError)


def test_error_str_repr_cause_never_leak_raw_value_secret_url_or_path(monkeypatch) -> None:
    """注入 secret/URL/绝对路径/raw value，断言异常不包含它们。"""
    sentinel_secret = "sk-sentinel-7f3a9c"
    sentinel_url = "https://secret-endpoint.example.internal/v1"
    sentinel_path = "C:\\Users\\sentinel\\private\\secret.db"
    with pytest.raises(SettingsValidationError) as captured:
        _load(
            monkeypatch,
            LOCAL_AGENT_REMOTE_VERIFY_TLS="maybe",
            LOCAL_AGENT_REMOTE_API_KEY=sentinel_secret,
            LOCAL_AGENT_REMOTE_API_BASE_URL=sentinel_url,
            LOCAL_AGENT_MEMORY_DB_PATH=sentinel_path,
        )
    error = captured.value
    assert error.safe_error_code == SETTINGS_PARSE_ERROR
    assert error.field == "LOCAL_AGENT_REMOTE_VERIFY_TLS"
    assert error.reason_code == "invalid_boolean"
    for representation in (str(error), repr(error)):
        assert sentinel_secret not in representation
        assert sentinel_url not in representation
        assert sentinel_path not in representation
        assert "maybe" not in representation
    assert error.__cause__ is None


def test_production_security_error_redacts_endpoint(monkeypatch) -> None:
    endpoint = "http://insecure.example.internal"
    settings = _load(
        monkeypatch,
        LOCAL_AGENT_ENVIRONMENT_PROFILE="PRODUCTION",
        LOCAL_AGENT_ENVIRONMENT_ID="prod-a",
        LOCAL_AGENT_REMOTE_API_BASE_URL=endpoint,
    )
    with pytest.raises(SettingsValidationError) as captured:
        validate_role_configuration(settings, role="SERVER")
    error = captured.value
    assert error.safe_error_code == SETTINGS_SECURITY_POLICY_ERROR
    assert error.field == "LOCAL_AGENT_REMOTE_API_BASE_URL"
    assert endpoint not in str(error)
    assert endpoint not in repr(error)


def test_startup_role_missing_endpoint_redacts_nothing_sensitive(monkeypatch) -> None:
    settings = _load(
        monkeypatch,
        LOCAL_AGENT_REMOTE_API_BASE_URL=None,
        LOCAL_AGENT_LLM_BACKEND="remote",
    )
    with pytest.raises(SettingsValidationError) as captured:
        validate_role_configuration(settings, role="SERVER")
    assert captured.value.safe_error_code == STARTUP_CONFIGURATION_ERROR
    assert captured.value.field == "LOCAL_AGENT_REMOTE_API_BASE_URL"
    assert "http" not in str(captured.value).lower()


def test_environment_profile_enum_values() -> None:
    assert {item.value for item in EnvironmentProfile} == {"LOCAL", "TEST", "PRODUCTION"}


# ---- P1-1：numeric contract range validation ----

# (env, reason_code, invalid explicit value, boundary-accepted value)
_POSITIVE_CONTRACT_FIELDS = (
    ("LOCAL_AGENT_REMOTE_TIMEOUT_SECONDS", "0", "1"),
    ("LOCAL_AGENT_REMOTE_CONTEXT_WINDOW", "0", "1"),
    ("LOCAL_AGENT_MODEL_THREADS", "0", "1"),
    ("LOCAL_AGENT_MODEL_CONTEXT", "0", "1"),
    ("LOCAL_AGENT_MODEL_MAX_TOKENS", "0", "1"),
    ("LOCAL_AGENT_HISTORY_WINDOW_SIZE", "0", "1"),
    ("LOCAL_AGENT_SUMMARY_TRIGGER_MESSAGES", "0", "1"),
    ("LOCAL_AGENT_SUMMARY_KEEP_RECENT", "0", "1"),
    ("LOCAL_AGENT_SUMMARY_MAX_CHARS", "0", "1"),
    ("LOCAL_AGENT_RAG_TOP_K", "0", "1"),
    ("LOCAL_AGENT_RAG_DOC_MAX_CHARS", "0", "1"),
    ("LOCAL_AGENT_RAG_CONTEXT_MAX_CHARS", "0", "1"),
    ("LOCAL_AGENT_ORCHESTRATION_MAX_AGENTS", "0", "1"),
)


@pytest.mark.parametrize(("env_name", "invalid", "boundary"), _POSITIVE_CONTRACT_FIELDS)
def test_positive_contract_fields_reject_zero_and_negative(
    monkeypatch, env_name, invalid, boundary
) -> None:
    for raw in (invalid, "-1"):
        with pytest.raises(SettingsValidationError) as captured:
            _load(monkeypatch, **{env_name: raw})
        assert captured.value.safe_error_code == SETTINGS_VALIDATION_ERROR
        assert captured.value.field == env_name
        assert captured.value.reason_code == "below_minimum"
    # 边界值 1 必须被接受。
    _load(monkeypatch, **{env_name: boundary})


def test_remote_timeout_zero_fails_closed_before_engine_construction(
    monkeypatch,
) -> None:
    """P1-1 最小复现：Production timeout=0 必须 Settings 期失败，不得进入 engine/requests。"""
    with pytest.raises(SettingsValidationError) as captured:
        _load(
            monkeypatch,
            LOCAL_AGENT_ENVIRONMENT_PROFILE="PRODUCTION",
            LOCAL_AGENT_ENVIRONMENT_ID="prod-a",
            LOCAL_AGENT_REMOTE_API_BASE_URL="https://example.test/v1",
            LOCAL_AGENT_REMOTE_TIMEOUT_SECONDS="0",
        )
    assert captured.value.safe_error_code == SETTINGS_VALIDATION_ERROR
    assert captured.value.field == "LOCAL_AGENT_REMOTE_TIMEOUT_SECONDS"


def test_remote_timeout_positive_boundary_is_accepted(monkeypatch) -> None:
    settings = _load(monkeypatch, LOCAL_AGENT_REMOTE_TIMEOUT_SECONDS="1")
    assert settings.remote_timeout_seconds == 1


def test_model_gpu_layers_backend_contract(monkeypatch) -> None:
    """llama-cpp-python==0.2.90：-1=全部层 offload、0=CPU、正数=指定层数。"""
    with pytest.raises(SettingsValidationError) as below:
        _load(monkeypatch, LOCAL_AGENT_MODEL_GPU_LAYERS="-2")
    assert below.value.reason_code == "below_minimum"
    assert _load(monkeypatch, LOCAL_AGENT_MODEL_GPU_LAYERS="-1").model_gpu_layers == -1
    assert _load(monkeypatch, LOCAL_AGENT_MODEL_GPU_LAYERS="0").model_gpu_layers == 0
    assert _load(monkeypatch, LOCAL_AGENT_MODEL_GPU_LAYERS="1").model_gpu_layers == 1


def test_api_port_is_bounded_range(monkeypatch) -> None:
    with pytest.raises(SettingsValidationError) as low:
        _load(monkeypatch, LOCAL_AGENT_API_PORT="0")
    assert low.value.reason_code == "below_minimum"
    with pytest.raises(SettingsValidationError) as high:
        _load(monkeypatch, LOCAL_AGENT_API_PORT="70000")
    assert high.value.reason_code == "above_maximum"
    assert _load(monkeypatch, LOCAL_AGENT_API_PORT="65535").api_port == 65535
