"""WP1-A Environment Profile 契约测试。

覆盖三 Profile 默认矩阵、precedence、Production 安全不变量、Environment 与
Model Profile 字段集合不重叠、TLS/trust_env/KB required 默认值。
"""

from __future__ import annotations

import os

import pytest

from core.settings import (
    SETTINGS_SECURITY_POLICY_ERROR,
    SETTINGS_VALIDATION_ERROR,
    STARTUP_CONFIGURATION_ERROR,
    EnvironmentProfile,
    Settings,
    SettingsValidationError,
    validate_role_configuration,
)

_PROFILE_ENV = "LOCAL_AGENT_ENVIRONMENT_PROFILE"
_PROFILE_GOAL = "LOCAL_AGENT_MODEL_PROFILE"

# Environment Profile 只管理这些字段；Model Profile 只管理这些资源字段。
_ENVIRONMENT_PROFILE_ENVS = {
    "LOCAL_AGENT_REMOTE_VERIFY_TLS",
    "LOCAL_AGENT_REMOTE_TRUST_ENV",
    "LOCAL_AGENT_KB_REQUIRED",
    "LOCAL_AGENT_ENVIRONMENT_ID",
}
_MODEL_PROFILE_ENVS = {
    "LOCAL_AGENT_MODEL_THREADS",
    "LOCAL_AGENT_MODEL_CONTEXT",
    "LOCAL_AGENT_MODEL_MAX_TOKENS",
    "LOCAL_AGENT_HISTORY_WINDOW_SIZE",
    "LOCAL_AGENT_SUMMARY_TRIGGER_MESSAGES",
    "LOCAL_AGENT_SUMMARY_KEEP_RECENT",
    "LOCAL_AGENT_SUMMARY_MAX_CHARS",
    "LOCAL_AGENT_RAG_TOP_K",
    "LOCAL_AGENT_RAG_DOC_MAX_CHARS",
    "LOCAL_AGENT_RAG_CONTEXT_MAX_CHARS",
}


def _load(monkeypatch, **env):
    for key, value in {
        _PROFILE_ENV: None,
        "LOCAL_AGENT_REMOTE_API_BASE_URL": "https://example.test/v1",
        "LOCAL_AGENT_REMOTE_VERIFY_TLS": None,
        "LOCAL_AGENT_REMOTE_TRUST_ENV": None,
        "LOCAL_AGENT_KB_REQUIRED": None,
        "LOCAL_AGENT_ENVIRONMENT_ID": None,
    }.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    # PRODUCTION 必须显式 environment_id；未提供时用测试占位。
    if (
        os.getenv(_PROFILE_ENV, "").strip().upper() == "PRODUCTION"
        and "LOCAL_AGENT_ENVIRONMENT_ID" not in env
    ):
        monkeypatch.setenv("LOCAL_AGENT_ENVIRONMENT_ID", "prod-test-placeholder")
    return Settings.load()


def test_default_profile_is_local(monkeypatch) -> None:
    settings = _load(monkeypatch)
    assert settings.environment_profile is EnvironmentProfile.LOCAL


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("local", EnvironmentProfile.LOCAL),
        ("LOCAL", EnvironmentProfile.LOCAL),
        (" Local ", EnvironmentProfile.LOCAL),
        ("test", EnvironmentProfile.TEST),
        ("TEST", EnvironmentProfile.TEST),
        ("production", EnvironmentProfile.PRODUCTION),
        ("PRODUCTION", EnvironmentProfile.PRODUCTION),
    ],
)
def test_profile_normalization(monkeypatch, raw, expected) -> None:
    assert _load(monkeypatch, LOCAL_AGENT_ENVIRONMENT_PROFILE=raw).environment_profile is expected


def test_unknown_profile_fails_closed(monkeypatch) -> None:
    with pytest.raises(SettingsValidationError) as captured:
        _load(monkeypatch, LOCAL_AGENT_ENVIRONMENT_PROFILE="staging")
    assert captured.value.safe_error_code == SETTINGS_VALIDATION_ERROR
    assert captured.value.field == "LOCAL_AGENT_ENVIRONMENT_PROFILE"


def test_blank_profile_fails_closed_not_fallback_to_local(monkeypatch) -> None:
    with pytest.raises(SettingsValidationError):
        _load(monkeypatch, LOCAL_AGENT_ENVIRONMENT_PROFILE="   ")


def test_verify_tls_profile_defaults(monkeypatch) -> None:
    assert _load(monkeypatch, LOCAL_AGENT_ENVIRONMENT_PROFILE="LOCAL").remote_verify_tls is False
    assert _load(monkeypatch, LOCAL_AGENT_ENVIRONMENT_PROFILE="TEST").remote_verify_tls is True
    assert _load(monkeypatch, LOCAL_AGENT_ENVIRONMENT_PROFILE="PRODUCTION").remote_verify_tls is True


def test_trust_env_profile_defaults(monkeypatch) -> None:
    assert _load(monkeypatch, LOCAL_AGENT_ENVIRONMENT_PROFILE="LOCAL").remote_trust_env is True
    assert _load(monkeypatch, LOCAL_AGENT_ENVIRONMENT_PROFILE="TEST").remote_trust_env is False
    assert _load(monkeypatch, LOCAL_AGENT_ENVIRONMENT_PROFILE="PRODUCTION").remote_trust_env is False


def test_kb_required_profile_defaults(monkeypatch) -> None:
    assert _load(monkeypatch, LOCAL_AGENT_ENVIRONMENT_PROFILE="LOCAL").knowledge_base_required is False
    assert _load(monkeypatch, LOCAL_AGENT_ENVIRONMENT_PROFILE="TEST").knowledge_base_required is False
    assert _load(monkeypatch, LOCAL_AGENT_ENVIRONMENT_PROFILE="PRODUCTION").knowledge_base_required is True


def test_environment_id_profile_defaults(monkeypatch) -> None:
    assert _load(monkeypatch, LOCAL_AGENT_ENVIRONMENT_PROFILE="LOCAL").environment_id == "local"
    assert _load(monkeypatch, LOCAL_AGENT_ENVIRONMENT_PROFILE="TEST").environment_id == "test"


def test_production_requires_explicit_environment_id(monkeypatch) -> None:
    with pytest.raises(SettingsValidationError) as captured:
        _load(
            monkeypatch,
            LOCAL_AGENT_ENVIRONMENT_PROFILE="PRODUCTION",
            LOCAL_AGENT_ENVIRONMENT_ID=None,
        )
    assert captured.value.field == "LOCAL_AGENT_ENVIRONMENT_ID"
    assert captured.value.reason_code == "required_for_production"


@pytest.mark.parametrize("raw", ["prod-east-1", "a", "x.y_z-0", "k8s-prod-01"])
def test_environment_id_valid_identifiers(monkeypatch, raw) -> None:
    settings = _load(
        monkeypatch,
        LOCAL_AGENT_ENVIRONMENT_PROFILE="PRODUCTION",
        LOCAL_AGENT_ENVIRONMENT_ID=raw,
    )
    assert settings.environment_id == raw


@pytest.mark.parametrize(
    "raw",
    ["https://example.test", "a b", "", "-bad", "Upper.Case", "../relative", "x" * 65],
)
def test_environment_id_invalid_identifiers_fail_closed(monkeypatch, raw) -> None:
    with pytest.raises(SettingsValidationError) as captured:
        _load(
            monkeypatch,
            LOCAL_AGENT_ENVIRONMENT_PROFILE="PRODUCTION",
            LOCAL_AGENT_ENVIRONMENT_ID=raw,
        )
    assert captured.value.field == "LOCAL_AGENT_ENVIRONMENT_ID"
    assert captured.value.reason_code == "invalid_identifier"


def test_explicit_env_overrides_profile_default(monkeypatch) -> None:
    settings = _load(
        monkeypatch,
        LOCAL_AGENT_ENVIRONMENT_PROFILE="TEST",
        LOCAL_AGENT_REMOTE_VERIFY_TLS="0",
    )
    assert settings.remote_verify_tls is False
    settings = _load(
        monkeypatch,
        LOCAL_AGENT_ENVIRONMENT_PROFILE="PRODUCTION",
        LOCAL_AGENT_ENVIRONMENT_ID="prod-a",
        LOCAL_AGENT_KB_REQUIRED="false",
    )
    assert settings.knowledge_base_required is False


def test_production_http_endpoint_fails_server_role(monkeypatch) -> None:
    settings = _load(
        monkeypatch,
        LOCAL_AGENT_ENVIRONMENT_PROFILE="PRODUCTION",
        LOCAL_AGENT_ENVIRONMENT_ID="prod-a",
        LOCAL_AGENT_REMOTE_API_BASE_URL="http://insecure.example.test/v1",
    )
    with pytest.raises(SettingsValidationError) as captured:
        validate_role_configuration(settings, role="SERVER")
    assert captured.value.safe_error_code == SETTINGS_SECURITY_POLICY_ERROR
    assert captured.value.field == "LOCAL_AGENT_REMOTE_API_BASE_URL"
    assert captured.value.reason_code == "production_requires_https"


def test_production_verify_tls_false_fails_server_role(monkeypatch) -> None:
    settings = _load(
        monkeypatch,
        LOCAL_AGENT_ENVIRONMENT_PROFILE="PRODUCTION",
        LOCAL_AGENT_ENVIRONMENT_ID="prod-a",
        LOCAL_AGENT_REMOTE_API_BASE_URL="https://example.test/v1",
        LOCAL_AGENT_REMOTE_VERIFY_TLS="false",
    )
    with pytest.raises(SettingsValidationError) as captured:
        validate_role_configuration(settings, role="SERVER")
    assert captured.value.safe_error_code == SETTINGS_SECURITY_POLICY_ERROR
    assert captured.value.field == "LOCAL_AGENT_REMOTE_VERIFY_TLS"
    assert captured.value.reason_code == "production_requires_tls_verification"


def test_production_valid_server_role_passes(monkeypatch) -> None:
    settings = _load(
        monkeypatch,
        LOCAL_AGENT_ENVIRONMENT_PROFILE="PRODUCTION",
        LOCAL_AGENT_ENVIRONMENT_ID="prod-a",
        LOCAL_AGENT_REMOTE_API_BASE_URL="https://example.test/v1",
    )
    validate_role_configuration(settings, role="SERVER")


def test_production_trust_env_true_is_explicit_operator_choice(monkeypatch) -> None:
    settings = _load(
        monkeypatch,
        LOCAL_AGENT_ENVIRONMENT_PROFILE="PRODUCTION",
        LOCAL_AGENT_ENVIRONMENT_ID="prod-a",
        LOCAL_AGENT_REMOTE_API_BASE_URL="https://example.test/v1",
        LOCAL_AGENT_REMOTE_TRUST_ENV="true",
    )
    assert settings.remote_trust_env is True


def test_test_profile_does_not_inherit_host_proxy(monkeypatch) -> None:
    settings = _load(monkeypatch, LOCAL_AGENT_ENVIRONMENT_PROFILE="TEST")
    assert settings.remote_trust_env is False


def test_environment_and_model_profile_fields_are_disjoint() -> None:
    assert _ENVIRONMENT_PROFILE_ENVS.isdisjoint(_MODEL_PROFILE_ENVS)


def test_model_profile_preset_is_independent_from_environment_profile(monkeypatch) -> None:
    for profile in ("LOCAL", "TEST", "PRODUCTION"):
        settings = _load(
            monkeypatch,
            LOCAL_AGENT_ENVIRONMENT_PROFILE=profile,
            LOCAL_AGENT_ENVIRONMENT_ID=(
                "prod-a" if profile == "PRODUCTION" else None
            ),
            LOCAL_AGENT_MODEL_PROFILE="deep",
        )
        assert settings.model_profile == "deep"
        assert settings.environment_profile.value == profile


def test_model_profile_does_not_change_environment_security_defaults(monkeypatch) -> None:
    settings = _load(
        monkeypatch,
        LOCAL_AGENT_ENVIRONMENT_PROFILE="TEST",
        LOCAL_AGENT_MODEL_PROFILE="fast",
    )
    assert settings.remote_verify_tls is True
    assert settings.remote_trust_env is False
