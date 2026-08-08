from __future__ import annotations

import pytest

from core.settings import (
    SETTINGS_VALIDATION_ERROR,
    Settings,
    SettingsValidationError,
)


def test_collection_environment_override(monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_AGENT_KB_COLLECTION", "local_agent_mock_v1")

    assert Settings.load().knowledge_collection_name == "local_agent_mock_v1"


def test_embedding_settings_environment_override(monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_AGENT_EMBEDDING_BATCH_SIZE", "4")
    monkeypatch.setenv("LOCAL_AGENT_EMBEDDING_QUERY_PROMPT_NAME", "query")

    settings = Settings.load()

    assert settings.embedding_batch_size == 4
    assert settings.embedding_query_prompt_name == "query"


def test_embedding_batch_size_below_minimum_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_AGENT_EMBEDDING_BATCH_SIZE", "0")

    with pytest.raises(SettingsValidationError) as captured:
        Settings.load()

    assert captured.value.safe_error_code == SETTINGS_VALIDATION_ERROR
    assert captured.value.field == "LOCAL_AGENT_EMBEDDING_BATCH_SIZE"


def test_default_collection_is_valid(monkeypatch) -> None:
    monkeypatch.delenv("LOCAL_AGENT_KB_COLLECTION", raising=False)

    assert Settings.load().knowledge_collection_name


def test_rag_min_score_environment_override(monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_AGENT_RAG_MIN_SCORE", "0.72")

    assert Settings.load().rag_min_score == 0.72


def test_rag_min_score_out_of_range_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_AGENT_RAG_MIN_SCORE", "1.5")

    with pytest.raises(SettingsValidationError) as captured:
        Settings.load()

    assert captured.value.safe_error_code == SETTINGS_VALIDATION_ERROR
    assert captured.value.field == "LOCAL_AGENT_RAG_MIN_SCORE"
