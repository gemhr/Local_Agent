"""WP3-D：真实 MemoryManager SQLite seam 的 SQL 注入语料回归。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.memory_manager import MemoryManager


SQL_INJECTION_CORPUS = (
    "'",
    "''",
    "' OR 1=1 --",
    '"; DROP TABLE messages; --',
    "/* comment */",
    "-- comment",
    "; SELECT 1;",
    "'); DELETE FROM messages; --",
    "\u2018",
    "\u2019",
    "\u02bc",
    "\uff07",
)
MODEL_SQL_LOOKING_OUTPUTS = (
    "SYSTEM OR SQL-looking text",
    '"; DROP TABLE messages; --',
)


def _manager(tmp_path: Path, name: str = "memory.db") -> MemoryManager:
    return MemoryManager(str(tmp_path / name))


def _schema_signature(path: str) -> tuple[tuple[str, str], ...]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            """
            SELECT type, name FROM sqlite_master
            WHERE name IN (
                'messages', 'conversation_summaries', 'message_exchanges',
                'messages_fts', 'messages_ai', 'messages_ad', 'messages_au'
            )
            ORDER BY type, name
            """
        ).fetchall()
    return tuple((str(row[0]), str(row[1])) for row in rows)


def _all_rows(manager: MemoryManager) -> list[tuple[str, str, str]]:
    with sqlite3.connect(manager.db_path) as connection:
        return [
            (str(row[0]), str(row[1]), str(row[2]))
            for row in connection.execute(
                "SELECT agent_id, role, content FROM messages ORDER BY id"
            )
        ]


def test_add_message_stores_sql_looking_user_and_model_text_as_exact_data(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    before_schema = _schema_signature(manager.db_path)
    sentinel_id = manager.add_message("victim", "user", "UNRELATED_SENTINEL")
    payloads = SQL_INJECTION_CORPUS + MODEL_SQL_LOOKING_OUTPUTS

    inserted = [
        manager.add_message("core_router", "assistant", payload)
        for payload in payloads
    ]

    with sqlite3.connect(manager.db_path) as connection:
        stored = [
            connection.execute(
                "SELECT content FROM messages WHERE id = ?", (message_id,)
            ).fetchone()[0]
            for message_id in inserted
        ]
        sentinel = connection.execute(
            "SELECT content FROM messages WHERE id = ?", (sentinel_id,)
        ).fetchone()[0]
    assert stored == list(payloads)
    assert sentinel == "UNRELATED_SENTINEL"
    assert _schema_signature(manager.db_path) == before_schema


@pytest.mark.parametrize("user_text", SQL_INJECTION_CORPUS)
@pytest.mark.parametrize("final_text", MODEL_SQL_LOOKING_OUTPUTS)
def test_append_exchange_atomic_keeps_user_and_model_text_as_bound_data(
    tmp_path: Path,
    user_text: str,
    final_text: str,
) -> None:
    manager = _manager(tmp_path, f"exchange-{abs(hash((user_text, final_text)))}.db")
    before_schema = _schema_signature(manager.db_path)
    manager.add_message("victim", "user", "UNRELATED_SENTINEL")

    result = manager.append_exchange_atomic(
        "core_router",
        "direct",
        user_text,
        final_text,
        run_id="run-wp3d",
    )

    history = manager.get_chat_history("core_router", ascending=True)
    assert [row["content"] for row in history] == [user_text, final_text]
    assert [row["role"] for row in history] == ["user", "assistant"]
    assert result["exchange_id"] == "run-wp3d"
    assert manager.count_messages("victim", memory_scope=None) == 1
    assert _schema_signature(manager.db_path) == before_schema


@pytest.mark.parametrize(
    "injected_agent_id",
    ("a' OR 1=1 --",) + SQL_INJECTION_CORPUS,
)
def test_get_chat_history_never_broadens_injected_agent_id(
    tmp_path: Path,
    injected_agent_id: str,
) -> None:
    manager = _manager(tmp_path)
    manager.add_message("agent-a", "user", "A-PRIVATE")
    manager.add_message("agent-b", "user", "B-PRIVATE")
    before_schema = _schema_signature(manager.db_path)

    assert manager.get_chat_history(injected_agent_id, limit=100, offset=0) == []
    assert [
        row["content"]
        for row in manager.get_chat_history("agent-a", limit=1, offset=0)
    ] == ["A-PRIVATE"]
    assert _schema_signature(manager.db_path) == before_schema


@pytest.mark.parametrize("query", SQL_INJECTION_CORPUS)
def test_search_sql_injection_corpus_cannot_escape_match_or_like_value(
    tmp_path: Path,
    query: str,
) -> None:
    manager = _manager(tmp_path)
    manager.add_message("core_router", "user", "needle")
    manager.add_message("victim", "user", "UNRELATED_SECRET")
    before_schema = _schema_signature(manager.db_path)

    results = manager.search_messages(query, limit=50, memory_scope="direct")

    assert "UNRELATED_SECRET" not in {row["content"] for row in results}
    assert manager.count_messages("victim", memory_scope=None) == 1
    assert _schema_signature(manager.db_path) == before_schema


def test_fts_query_language_is_search_semantics_not_sql_injection(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    for content in ("needle", "other", "needle other", "unrelated"):
        manager.add_message("core_router", "user", content)
    before_schema = _schema_signature(manager.db_path)

    or_results = {row["content"] for row in manager.search_messages("needle OR other")}
    not_results = {row["content"] for row in manager.search_messages("needle NOT other")}
    near_results = {row["content"] for row in manager.search_messages("NEAR(needle other)")}
    malformed_results = manager.search_messages("'", limit=50)

    # SEARCH_QUERY_SEMANTIC_LIMITATION != SQL_INJECTION：FTS5 在 bound value
    # 内解析 OR/NOT/NEAR；malformed 输入只进入既有 parameterized LIKE fallback。
    assert or_results == {"needle", "other", "needle other"}
    assert not_results == {"needle"}
    assert near_results <= {"needle other"}
    assert malformed_results == []
    assert _schema_signature(manager.db_path) == before_schema


def test_like_wildcards_can_broaden_fallback_without_sql_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    for content in ("a", "bb", "needle"):
        manager.add_message("core_router", "user", content)
    before_schema = _schema_signature(manager.db_path)

    percent_results = {row["content"] for row in manager.search_messages("%")}
    assert percent_results == {"a", "bb", "needle"}

    real_connect = sqlite3.connect

    class FtsOperationalFailureConnection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            if " MATCH ?" in str(sql):
                raise sqlite3.OperationalError("synthetic FTS parse failure")
            return super().execute(sql, parameters)

    def fts_denied_connect(*args, **kwargs):
        return real_connect(*args, factory=FtsOperationalFailureConnection, **kwargs)

    monkeypatch.setattr("core.memory_manager.sqlite3.connect", fts_denied_connect)
    underscore_results = {row["content"] for row in manager.search_messages("_")}

    # SEARCH_QUERY_SEMANTIC_LIMITATION != SQL_INJECTION：在真实 LIKE fallback
    # 中 `%`/`_` 保留 wildcard 语义，但仍是单个 DB-API bound value。
    assert underscore_results == {"a", "bb", "needle"}
    assert _schema_signature(manager.db_path) == before_schema


class _RecordingConnection(sqlite3.Connection):
    calls: list[tuple[str, object]] = []

    def execute(self, sql, parameters=(), /):
        self.calls.append((str(sql), parameters))
        return super().execute(sql, parameters)


def test_delete_messages_uses_length_only_placeholder_shape_and_bound_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    target_ids = [
        manager.add_message("target", "user", content)
        for content in ("delete-one", "delete-two")
    ]
    victim_id = manager.add_message("victim", "user", "UNRELATED_SENTINEL")
    before_schema = _schema_signature(manager.db_path)
    real_connect = sqlite3.connect
    _RecordingConnection.calls = []

    def recording_connect(*args, **kwargs):
        return real_connect(*args, factory=_RecordingConnection, **kwargs)

    monkeypatch.setattr("core.memory_manager.sqlite3.connect", recording_connect)
    result = manager.delete_messages(target_ids)

    delete_calls = [
        (sql, parameters)
        for sql, parameters in _RecordingConnection.calls
        if sql.startswith("DELETE FROM messages WHERE id IN")
    ]
    assert len(delete_calls) == 1
    sql, parameters = delete_calls[0]
    assert sql.count("?") == len(target_ids)
    assert list(parameters) == target_ids
    assert "target" in result["affected_agent_ids"]
    with sqlite3.connect(manager.db_path) as connection:
        assert connection.execute(
            "SELECT content FROM messages WHERE id = ?", (victim_id,)
        ).fetchone()[0] == "UNRELATED_SENTINEL"
    assert _schema_signature(manager.db_path) == before_schema


def test_delete_messages_sql_looking_direct_seam_value_remains_data(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    victim_id = manager.add_message("victim", "user", "UNRELATED_SENTINEL")
    before_schema = _schema_signature(manager.db_path)

    result = manager.delete_messages(["1) OR 1=1 --"])

    assert result == {"affected_agent_ids": [], "refresh_agent_ids": []}
    with sqlite3.connect(manager.db_path) as connection:
        assert connection.execute(
            "SELECT content FROM messages WHERE id = ?", (victim_id,)
        ).fetchone()[0] == "UNRELATED_SENTINEL"
    assert _schema_signature(manager.db_path) == before_schema
