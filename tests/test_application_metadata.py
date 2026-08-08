"""WP1-A Application metadata / instance identity 测试。

覆盖：frozen safe metadata、instance_id 每进程唯一且不可 env override、
service_version 来源真实（pyproject.toml）、environment_id 安全 identifier、
Production environment_id required。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from core.application_metadata import (
    ApplicationMetadata,
    create_application_metadata,
)
from core.settings import Settings, SettingsValidationError

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    with open(_PROJECT_ROOT / "pyproject.toml", "rb") as handle:
        data = tomllib.load(handle)
    return data["project"]["version"]


def _load(monkeypatch, **env):
    for key, value in {
        "LOCAL_AGENT_ENVIRONMENT_PROFILE": None,
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
    return Settings.load()


def test_application_metadata_is_frozen_and_safe(monkeypatch) -> None:
    settings = _load(monkeypatch)
    metadata = create_application_metadata(settings)
    assert isinstance(metadata, ApplicationMetadata)
    with pytest.raises(Exception):
        metadata.instance_id = "overwritten"  # frozen dataclass
    safe = metadata.to_safe_dict()
    assert set(safe) == {
        "environment_profile",
        "environment_id",
        "service_version",
        "instance_id",
    }
    assert safe["environment_profile"] == "LOCAL"
    assert safe["environment_id"] == "local"


def test_instance_id_is_uuid_and_stable_for_one_creation(monkeypatch) -> None:
    settings = _load(monkeypatch)
    metadata = create_application_metadata(settings)
    assert re.fullmatch(r"[0-9a-f]{32}", metadata.instance_id)
    assert metadata.instance_id == metadata.to_safe_dict()["instance_id"]


def test_instance_id_is_new_across_creations(monkeypatch) -> None:
    settings = _load(monkeypatch)
    first = create_application_metadata(settings).instance_id
    second = create_application_metadata(settings).instance_id
    assert first != second


def test_instance_id_cannot_be_overridden_by_env(monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_AGENT_INSTANCE_ID", "attacker-controlled")
    settings = _load(monkeypatch)
    metadata = create_application_metadata(settings)
    assert metadata.instance_id != "attacker-controlled"
    assert re.fullmatch(r"[0-9a-f]{32}", metadata.instance_id)


def test_settings_has_no_instance_id_field() -> None:
    assert "instance_id" not in {field.name for field in Settings.__dataclass_fields__.values()}


def test_service_version_matches_real_project_metadata(monkeypatch) -> None:
    settings = _load(monkeypatch)
    assert settings.service_version == _pyproject_version()


def test_service_version_is_valid_semver_like(monkeypatch) -> None:
    settings = _load(monkeypatch)
    assert re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,3}(?:[-+][0-9A-Za-z._-]+)?", settings.service_version)


def test_environment_id_requires_production_explicit(monkeypatch) -> None:
    with pytest.raises(SettingsValidationError) as captured:
        _load(monkeypatch, LOCAL_AGENT_ENVIRONMENT_PROFILE="PRODUCTION")
    assert captured.value.reason_code == "required_for_production"


def test_production_environment_id_is_low_cardinality_identifier(monkeypatch) -> None:
    settings = _load(
        monkeypatch,
        LOCAL_AGENT_ENVIRONMENT_PROFILE="PRODUCTION",
        LOCAL_AGENT_ENVIRONMENT_ID="prod-region-1",
    )
    metadata = create_application_metadata(settings)
    assert metadata.environment_id == "prod-region-1"
