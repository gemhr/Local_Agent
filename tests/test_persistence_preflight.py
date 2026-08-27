"""WP1-D Persistence preflight read-only contract tests。

证明：absent 文件不被创建、已有文件 bytes/schema 不被修改、quick_check
corrupt → FAILED、unsupported → UNSUPPORTED、migration-required →
MIGRATION_REQUIRED、FULL 模式校验 record-level facts、safe report 只含
allowlist 字段（无 absolute path / raw exception）。
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.memory_manager import MEMORY_SCHEMA_VERSION, MemoryManager, memory_preflight
from core.persistence_migration import (
    PERSISTENCE_PREFLIGHT_FAILED,
    PERSISTENCE_SCHEMA_UNSUPPORTED,
    PersistencePaths,
    PreflightMode,
    PreflightStatus,
    StoreId,
    run_persistence_preflight,
)
from core.runtime.event_consumer import (
    SQLiteEventConsumptionCheckpointStore,
    checkpoint_preflight,
)
from core.runtime.event_journal_store import (
    SQLiteRunEventJournal,
    journal_preflight,
)
from core.runtime.events import OutputDeltaPayload, RuntimeEvent, RuntimeEventType
from core.runtime.snapshot_store import SQLiteSnapshotStore, snapshot_preflight
from test_snapshot_contract import make_snapshot

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _memory_v2(path: Path) -> Path:
    """当前 v2 库：MemoryManager 直接初始化 v2 exact shape。"""
    manager = MemoryManager(db_path=str(path))
    manager.add_message("agent-a", "user", "hello")
    return path


def _memory_v1(path: Path) -> Path:
    """真实 v1 库：v2 库去掉 Long-term Memory 结构并回退版本标记。

    保留 messages/summaries/exchanges/FTS/triggers，正是 v1→v2 migration
    的已批准 from-state。
    """
    _memory_v2(path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE long_term_memory")
        conn.execute("PRAGMA user_version = 1")
    return path


def _memory_unversioned_current(path: Path) -> Path:
    _memory_v2(path)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version = 0")
    return path


def _memory_legacy(path: Path) -> Path:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                metadata TEXT
            );
            CREATE TABLE conversation_summaries (
                agent_id TEXT PRIMARY KEY,
                summary TEXT NOT NULL,
                last_message_id INTEGER NOT NULL DEFAULT 0,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX idx_messages_agent_time
                ON messages(agent_id, timestamp DESC, id DESC);
            CREATE INDEX idx_messages_timestamp
                ON messages(timestamp DESC, id DESC);
            INSERT INTO messages (agent_id, role, content)
                VALUES ('agent-a', 'user', 'legacy-content');
            """
        )
    return path


def _memory_future(path: Path) -> Path:
    """未来/未知版本（v3）：CURRENT 之外 must fail closed。"""
    _memory_v2(path)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version = 3")
    return path


def _memory_corrupt(path: Path) -> Path:
    path.write_bytes(b"NOT A SQLITE DATABASE" * 8)
    return path


def _journal_current(path: Path) -> Path:
    journal = SQLiteRunEventJournal(str(path))
    journal.close()
    return path


def _journal_legacy(path: Path) -> Path:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE runtime_event_journal (
                journal_schema_version INTEGER NOT NULL,
                event_schema_version INTEGER NOT NULL,
                event_id TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL,
                trace_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                emitted_at TEXT NOT NULL,
                journaled_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                component TEXT NOT NULL,
                step_id TEXT,
                step_sequence INTEGER,
                safe_payload TEXT NOT NULL,
                payload_digest TEXT NOT NULL,
                event_digest TEXT NOT NULL,
                PRIMARY KEY (run_id, sequence)
            );
            CREATE INDEX idx_runtime_event_journal_run_type
                ON runtime_event_journal(run_id, event_type);
            """
        )
    return path


def _snapshot_current(path: Path) -> Path:
    store = SQLiteSnapshotStore(str(path))
    store.save(make_snapshot())
    store.close()
    return path


def _checkpoint_current(path: Path) -> Path:
    store = SQLiteEventConsumptionCheckpointStore(str(path))
    store.save(_make_checkpoint())
    store.close()
    return path


def _make_checkpoint():
    from core.runtime.event_consumer import EventConsumptionCheckpoint
    from datetime import UTC, datetime

    return EventConsumptionCheckpoint(
        consumer_id="logger",
        event_id="evt-1",
        run_id="run-1",
        sequence=1,
        processed_at=datetime.now(UTC),
    )


def _checkpoint_incompatible(path: Path) -> Path:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE event_consumption_checkpoint (
                consumer_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                processed_at TEXT NOT NULL
            )
            """
        )
    return path


def _paths(memory: Path, journal: Path, snapshot: Path, checkpoint: Path) -> PersistencePaths:
    return PersistencePaths(
        memory_db_path=str(memory),
        event_journal_db_path=str(journal),
        observability_checkpoint_db_path=str(checkpoint),
        snapshot_store_db_path=str(snapshot),
    )


# ---------------------------------------------------------------------------
# Memory preflight
# ---------------------------------------------------------------------------


def test_memory_absent_is_new_and_does_not_create_file(tmp_path: Path) -> None:
    target = tmp_path / "absent.db"
    result = memory_preflight(str(target))
    assert result.status is PreflightStatus.NEW
    assert result.action.value == "INITIALIZE"
    assert not target.exists()


def test_memory_v2_is_current_and_unchanged(tmp_path: Path) -> None:
    path = _memory_v2(tmp_path / "m.db")
    before = path.read_bytes()
    result = memory_preflight(str(path), mode=PreflightMode.FULL)
    assert result.status is PreflightStatus.CURRENT
    assert result.detected_version == "2"
    after = path.read_bytes()
    assert after == before


def test_memory_v1_is_migration_required(tmp_path: Path) -> None:
    path = _memory_v1(tmp_path / "m.db")
    before = path.read_bytes()
    result = memory_preflight(str(path), mode=PreflightMode.FULL)
    assert result.status is PreflightStatus.MIGRATION_REQUIRED
    assert result.action.value == "MIGRATE"
    assert result.detected_version == "1"
    assert result.target_version == "2"
    assert path.read_bytes() == before


def test_memory_unversioned_current_is_migration_required(tmp_path: Path) -> None:
    path = _memory_unversioned_current(tmp_path / "m.db")
    result = memory_preflight(str(path))
    assert result.status is PreflightStatus.MIGRATION_REQUIRED
    assert result.action.value == "MIGRATE"


def test_memory_legacy_is_migration_required(tmp_path: Path) -> None:
    path = _memory_legacy(tmp_path / "m.db")
    result = memory_preflight(str(path))
    assert result.status is PreflightStatus.MIGRATION_REQUIRED
    assert result.action.value == "MIGRATE"


def test_memory_future_version_is_unsupported(tmp_path: Path) -> None:
    path = _memory_future(tmp_path / "m.db")
    result = memory_preflight(str(path))
    assert result.status is PreflightStatus.UNSUPPORTED
    assert result.safe_error_code == PERSISTENCE_SCHEMA_UNSUPPORTED


def test_memory_corrupt_quick_check_is_failed(tmp_path: Path) -> None:
    path = _memory_corrupt(tmp_path / "m.db")
    result = memory_preflight(str(path))
    assert result.status is PreflightStatus.FAILED
    assert result.safe_error_code == PERSISTENCE_PREFLIGHT_FAILED


# ---------------------------------------------------------------------------
# Journal preflight
# ---------------------------------------------------------------------------


def test_journal_absent_is_new(tmp_path: Path) -> None:
    target = tmp_path / "j.db"
    result = journal_preflight(str(target))
    assert result.status is PreflightStatus.NEW
    assert not target.exists()


def test_journal_current_is_current(tmp_path: Path) -> None:
    path = _journal_current(tmp_path / "j.db")
    before = path.read_bytes()
    result = journal_preflight(str(path), mode=PreflightMode.FULL)
    assert result.status is PreflightStatus.CURRENT
    assert path.read_bytes() == before


def test_journal_legacy_is_migration_required(tmp_path: Path) -> None:
    path = _journal_legacy(tmp_path / "j.db")
    result = journal_preflight(str(path))
    assert result.status is PreflightStatus.MIGRATION_REQUIRED
    assert result.action.value == "MIGRATE"


def test_journal_unknown_physical_shape_is_unsupported(tmp_path: Path) -> None:
    path = tmp_path / "j.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE runtime_event_journal (other TEXT)")
    result = journal_preflight(str(path))
    assert result.status is PreflightStatus.UNSUPPORTED
    assert result.safe_error_code == PERSISTENCE_SCHEMA_UNSUPPORTED


# ---------------------------------------------------------------------------
# Snapshot / Checkpoint preflight
# ---------------------------------------------------------------------------


def test_snapshot_absent_is_new_and_not_created(tmp_path: Path) -> None:
    target = tmp_path / "s.db"
    result = snapshot_preflight(str(target))
    assert result.status is PreflightStatus.NEW
    assert not target.exists()


def test_snapshot_current_is_current(tmp_path: Path) -> None:
    path = _snapshot_current(tmp_path / "s.db")
    before = path.read_bytes()
    result = snapshot_preflight(str(path), mode=PreflightMode.FULL)
    assert result.status is PreflightStatus.CURRENT
    assert path.read_bytes() == before


def test_checkpoint_absent_is_new(tmp_path: Path) -> None:
    target = tmp_path / "c.db"
    result = checkpoint_preflight(str(target))
    assert result.status is PreflightStatus.NEW
    assert not target.exists()


def test_checkpoint_current_is_current(tmp_path: Path) -> None:
    path = _checkpoint_current(tmp_path / "c.db")
    result = checkpoint_preflight(str(path))
    assert result.status is PreflightStatus.CURRENT


def test_checkpoint_incompatible_is_migration_required_recreate(tmp_path: Path) -> None:
    path = _checkpoint_incompatible(tmp_path / "c.db")
    result = checkpoint_preflight(str(path))
    assert result.status is PreflightStatus.MIGRATION_REQUIRED
    assert result.action.value == "RECREATE"


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


def test_coordinator_preflight_absent_creates_nothing(tmp_path: Path) -> None:
    memory = tmp_path / "memory.db"
    journal = tmp_path / "journal.db"
    snapshot = tmp_path / "snapshot.db"
    checkpoint = tmp_path / "checkpoint.db"
    results = run_persistence_preflight(
        _paths(memory, journal, snapshot, checkpoint), mode=PreflightMode.FULL
    )
    statuses = {result.store_id: result.status for result in results}
    assert statuses[StoreId.MEMORY] is PreflightStatus.NEW
    assert statuses[StoreId.EVENT_JOURNAL] is PreflightStatus.NEW
    assert statuses[StoreId.SNAPSHOT] is PreflightStatus.NEW
    assert statuses[StoreId.OBSERVABILITY_CHECKPOINT] is PreflightStatus.NEW
    for candidate in (memory, journal, snapshot, checkpoint):
        assert not candidate.exists()


def test_coordinator_preflight_current_all(tmp_path: Path) -> None:
    memory = _memory_v2(tmp_path / "memory.db")
    journal = _journal_current(tmp_path / "journal.db")
    snapshot = _snapshot_current(tmp_path / "snapshot.db")
    checkpoint = _checkpoint_current(tmp_path / "checkpoint.db")
    results = run_persistence_preflight(
        _paths(memory, journal, snapshot, checkpoint), mode=PreflightMode.FULL
    )
    assert all(
        result.status in {PreflightStatus.CURRENT, PreflightStatus.NEW}
        for result in results
    )


def test_full_preflight_validates_record_level_facts(tmp_path: Path) -> None:
    """FULL 模式逐 row 校验 digest；STARTUP 模式不校验 record 内容。"""
    path = tmp_path / "j.db"
    journal = SQLiteRunEventJournal(str(path))
    event = RuntimeEvent(
        schema_version=2,
        event_id="evt-1",
        run_id="run-1",
        trace_id="trace-1",
        sequence=1,
        emitted_at=datetime.now(UTC),
        component="test",
        event_type=RuntimeEventType.OUTPUT_DELTA,
        payload=OutputDeltaPayload(text="hello"),
    )
    journal.append(event)
    journal.close()

    assert journal_preflight(str(path)).status is PreflightStatus.CURRENT
    # 篡改 safe_payload → FULL 检测，STARTUP 不检测
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE runtime_event_journal SET safe_payload = ? WHERE event_id = ?",
            ('{"text":"TAMPERED"}', "evt-1"),
        )
    assert journal_preflight(str(path)).status is PreflightStatus.CURRENT
    result = journal_preflight(str(path), mode=PreflightMode.FULL)
    assert result.status is PreflightStatus.FAILED
    assert result.safe_error_code == PERSISTENCE_PREFLIGHT_FAILED


def test_safe_report_has_no_absolute_path_or_exception(tmp_path: Path) -> None:
    path = _memory_future(tmp_path / "m.db")
    result = memory_preflight(str(path), mode=PreflightMode.FULL)
    text = repr(result)
    assert "C:" not in text
    assert "\\" not in text
    assert "sqlite3." not in text
    assert "Traceback" not in text
    # 只含固定字段
    assert result.store_id.value == "MEMORY"
    assert result.status.value in {s.value for s in PreflightStatus}
    assert result.safe_error_code == PERSISTENCE_SCHEMA_UNSUPPORTED


# ---------------------------------------------------------------------------
# P1 Remediation：exact physical signature negative families
# ---------------------------------------------------------------------------


def _memory_malformed_constraints(path: Path) -> Path:
    """所有 current 对象名/索引名/FTS/trigger 存在，但 messages 约束错误：
    无 PK、全列可空、memory_scope 无 NOT NULL/DEFAULT。"""
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE messages (
                id INTEGER, agent_id TEXT, role TEXT, content TEXT, timestamp DATETIME,
                metadata TEXT, memory_scope TEXT, exchange_id TEXT, run_id TEXT, sequence INTEGER
            );
            CREATE TABLE conversation_summaries (
                agent_id TEXT PRIMARY KEY, summary TEXT NOT NULL,
                last_message_id INTEGER NOT NULL DEFAULT 0,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE message_exchanges (
                exchange_id TEXT PRIMARY KEY, run_id TEXT UNIQUE, agent_id TEXT NOT NULL,
                memory_scope TEXT NOT NULL DEFAULT 'direct', state TEXT NOT NULL,
                user_message_id INTEGER, assistant_message_id INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX idx_messages_agent_scope_time
                ON messages(agent_id, memory_scope, timestamp, id);
            CREATE INDEX idx_messages_agent_time
                ON messages(agent_id, timestamp, id);
            CREATE INDEX idx_messages_scope_time
                ON messages(memory_scope, timestamp, id);
            CREATE INDEX idx_messages_timestamp
                ON messages(timestamp, id);
            CREATE UNIQUE INDEX idx_messages_exchange_role
                ON messages(exchange_id, role) WHERE exchange_id IS NOT NULL;
            CREATE INDEX idx_exchanges_state ON message_exchanges(state);
            CREATE VIRTUAL TABLE messages_fts USING fts5(
                content, agent_id UNINDEXED, role UNINDEXED,
                content='messages', content_rowid='id'
            );
            CREATE TRIGGER messages_ai AFTER INSERT ON messages
                BEGIN INSERT INTO messages_fts(rowid, content, agent_id, role)
                VALUES (new.id, new.content, new.agent_id, new.role); END;
            CREATE TRIGGER messages_ad AFTER DELETE ON messages
                BEGIN INSERT INTO messages_fts(messages_fts, rowid, content, agent_id, role)
                VALUES ('delete', old.id, old.content, old.agent_id, old.role); END;
            CREATE TRIGGER messages_au AFTER UPDATE ON messages
                BEGIN INSERT INTO messages_fts(messages_fts, rowid, content, agent_id, role)
                VALUES ('delete', old.id, old.content, old.agent_id, old.role);
                INSERT INTO messages_fts(rowid, content, agent_id, role)
                VALUES (new.id, new.content, new.agent_id, new.role); END;
            """
        )
    return path


def _memory_break_index_columns(path: Path) -> Path:
    """valid current DB → 同名 index 改到错误列（且去掉 UNIQUE/partial）。"""
    with sqlite3.connect(path) as conn:
        conn.execute("DROP INDEX idx_messages_exchange_role")
        conn.execute(
            "CREATE INDEX idx_messages_exchange_role ON messages(content)"
        )
    return path


def _memory_break_trigger(path: Path) -> Path:
    """valid current DB → messages_ai 替换为 no-op trigger。"""
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER messages_ai")
        conn.execute(
            "CREATE TRIGGER messages_ai AFTER INSERT ON messages "
            "BEGIN SELECT 1; END"
        )
    return path


def _memory_break_fts(path: Path) -> Path:
    """valid current DB → messages_fts 替换为错误定义（丢失 content 映射）。"""
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE messages_fts")
        conn.execute("CREATE VIRTUAL TABLE messages_fts USING fts5(content)")
    return path


def _memory_current_formatted_variant(path: Path) -> Path:
    """语义相同的 current v2 schema，但使用小写关键字 / 不规则空白 /
    双引号标识符——验证 canonical signature 不过度拟合格式。"""
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            create table messages (id integer primary key autoincrement,
                agent_id text not null, role text not null, content text not null,
                timestamp datetime default current_timestamp, metadata text,
                memory_scope text not null default 'direct',
                exchange_id text, run_id text, sequence integer);
            create table conversation_summaries (agent_id text primary key,
                summary text not null, last_message_id integer not null default 0,
                updated_at datetime default current_timestamp);
            create table message_exchanges (exchange_id text primary key,
                run_id text unique, agent_id text not null,
                memory_scope text not null default 'direct', state text not null,
                user_message_id integer, assistant_message_id integer,
                created_at datetime default current_timestamp);
            create index idx_messages_agent_scope_time
                on messages(agent_id, memory_scope, timestamp, id);
            create index idx_messages_agent_time
                on messages(agent_id, timestamp, id);
            create index idx_messages_scope_time
                on messages(memory_scope, timestamp, id);
            create index idx_messages_timestamp
                on messages(timestamp, id);
            create unique index idx_messages_exchange_role
                on messages(exchange_id, role) where exchange_id is not null;
            create index idx_exchanges_state on message_exchanges(state);
            create virtual table messages_fts using fts5(
                content, agent_id unindexed, role unindexed,
                content='messages', content_rowid='id');
            create trigger messages_ai after insert on messages
                begin insert into "messages_fts"(rowid, "content", "agent_id", "role")
                values ("new"."id", "new"."content", "new"."agent_id", "new"."role"); end;
            create trigger messages_ad after delete on messages
                begin insert into "messages_fts"("messages_fts", rowid, content, agent_id, role)
                values ('delete', "old"."id", "old"."content", "old"."agent_id", "old"."role"); end;
            create trigger messages_au after update on messages
                begin insert into "messages_fts"("messages_fts", rowid, content, agent_id, role)
                values ('delete', "old"."id", "old"."content", "old"."agent_id", "old"."role");
                insert into "messages_fts"(rowid, content, agent_id, role)
                values ("new"."id", "new"."content", "new"."agent_id", "new"."role"); end;
            create table long_term_memory (memory_id text primary key,
                memory_type text not null, status text not null,
                agent_id text not null, memory_scope text not null,
                canonical_text text not null, payload text not null,
                logical_key text, origin_type text not null,
                origin_run_id text not null, origin_exchange_id text not null,
                origin_agent_id text not null, origin_memory_scope text not null,
                formation_method text, created_at text not null,
                updated_at text not null, superseded_by_memory_id text);
            create index idx_long_term_memory_agent_scope
                on long_term_memory(agent_id, memory_scope);
            """
        )
        conn.execute("PRAGMA user_version = 2")
    return path


def _memory_no_mutation_preflight(path: Path) -> PersistencePreflightResult:
    before = path.read_bytes()
    result = memory_preflight(str(path), mode=PreflightMode.FULL)
    after = path.read_bytes()
    assert after == before
    return result


def test_memory_malformed_constraints_is_unsupported(tmp_path: Path) -> None:
    path = _memory_malformed_constraints(tmp_path / "m.db")
    result = _memory_no_mutation_preflight(path)
    assert result.status is PreflightStatus.UNSUPPORTED
    assert result.safe_error_code == PERSISTENCE_SCHEMA_UNSUPPORTED


def test_memory_wrong_index_columns_is_unsupported(tmp_path: Path) -> None:
    path = _memory_break_index_columns(_memory_v2(tmp_path / "m.db"))
    result = _memory_no_mutation_preflight(path)
    assert result.status is PreflightStatus.UNSUPPORTED
    assert result.safe_error_code == PERSISTENCE_SCHEMA_UNSUPPORTED


def test_memory_noop_trigger_is_unsupported(tmp_path: Path) -> None:
    path = _memory_break_trigger(_memory_v2(tmp_path / "m.db"))
    result = _memory_no_mutation_preflight(path)
    assert result.status is PreflightStatus.UNSUPPORTED
    assert result.safe_error_code == PERSISTENCE_SCHEMA_UNSUPPORTED


def test_memory_wrong_fts_definition_is_unsupported(tmp_path: Path) -> None:
    path = _memory_break_fts(_memory_v2(tmp_path / "m.db"))
    result = _memory_no_mutation_preflight(path)
    assert result.status is PreflightStatus.UNSUPPORTED
    assert result.safe_error_code == PERSISTENCE_SCHEMA_UNSUPPORTED


def test_memory_user_version_one_but_malformed_is_unsupported(tmp_path: Path) -> None:
    """user_version=1 且全部对象名存在，但约束错误 → 必须 UNSUPPORTED。"""
    path = _memory_malformed_constraints(tmp_path / "m.db")
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version = 1")
    result = _memory_no_mutation_preflight(path)
    assert result.status is PreflightStatus.UNSUPPORTED
    assert result.safe_error_code == PERSISTENCE_SCHEMA_UNSUPPORTED


def test_memory_current_semantic_formatting_variation_is_current(tmp_path: Path) -> None:
    """§39：语义相同但格式不同（小写关键字/不规则空白/双引号标识符）
    的 current schema 不得被误判 unsupported。"""
    path = _memory_current_formatted_variant(tmp_path / "m.db")
    result = memory_preflight(str(path), mode=PreflightMode.FULL)
    assert result.status is PreflightStatus.CURRENT


def _journal_malformed(path: Path) -> Path:
    """全部 current 列名，但全列可空、无 event_id UNIQUE、无 (run_id, sequence) PK、
    无 required index。"""
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE runtime_event_journal (
                journal_schema_version INTEGER, event_schema_version INTEGER,
                event_id TEXT, run_id TEXT, trace_id TEXT, sequence INTEGER,
                emitted_at TEXT, journaled_at TEXT, event_type TEXT,
                component TEXT, step_id TEXT, step_sequence INTEGER,
                span_id TEXT, parent_span_id TEXT, safe_payload TEXT,
                payload_digest TEXT, event_digest TEXT
            );
            """
        )
    return path


def test_journal_malformed_no_constraints_is_unsupported(tmp_path: Path) -> None:
    path = _journal_malformed(tmp_path / "j.db")
    before = path.read_bytes()
    result = journal_preflight(str(path), mode=PreflightMode.FULL)
    assert path.read_bytes() == before
    assert result.status is PreflightStatus.UNSUPPORTED
    assert result.safe_error_code == PERSISTENCE_SCHEMA_UNSUPPORTED


def _snapshot_malformed(path: Path) -> Path:
    """全部列名，但无 PK/约束，approved index 名建在错误列。"""
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE runtime_snapshots (
                snapshot_schema_version INTEGER, snapshot_id TEXT, run_id TEXT,
                created_at TEXT, payload_json TEXT, payload_digest TEXT
            );
            CREATE INDEX idx_runtime_snapshots_run_created
                ON runtime_snapshots(payload_digest, snapshot_id);
            """
        )
    return path


def test_snapshot_malformed_no_pk_wrong_index_is_unsupported(tmp_path: Path) -> None:
    path = _snapshot_malformed(tmp_path / "s.db")
    before = path.read_bytes()
    result = snapshot_preflight(str(path), mode=PreflightMode.FULL)
    assert path.read_bytes() == before
    assert result.status is PreflightStatus.UNSUPPORTED
    assert result.safe_error_code == PERSISTENCE_SCHEMA_UNSUPPORTED


# ---------------------------------------------------------------------------
# Second P1 Remediation：semantic UNIQUE set exact + literal sensitivity
# ---------------------------------------------------------------------------


def _memory_missing_run_id_unique(path: Path) -> Path:
    """otherwise exact current schema，但 message_exchanges.run_id 缺 UNIQUE。"""
    _memory_v2(path)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            DROP INDEX idx_exchanges_state;
            ALTER TABLE message_exchanges RENAME TO message_exchanges_orig;
            CREATE TABLE message_exchanges (
                exchange_id TEXT PRIMARY KEY,
                run_id TEXT,
                agent_id TEXT NOT NULL,
                memory_scope TEXT NOT NULL DEFAULT 'direct',
                state TEXT NOT NULL,
                user_message_id INTEGER,
                assistant_message_id INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            DROP TABLE message_exchanges_orig;
            CREATE INDEX idx_exchanges_state ON message_exchanges(state);
            """
        )
    return path


def test_memory_missing_run_id_unique_is_unsupported(tmp_path: Path) -> None:
    path = _memory_missing_run_id_unique(tmp_path / "m.db")
    before = path.read_bytes()
    result = memory_preflight(str(path), mode=PreflightMode.FULL)
    assert path.read_bytes() == before
    assert result.status is PreflightStatus.UNSUPPORTED
    assert result.safe_error_code == PERSISTENCE_SCHEMA_UNSUPPORTED


def test_memory_extra_unique_constraint_is_unsupported(tmp_path: Path) -> None:
    path = _memory_v2(tmp_path / "m.db")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE UNIQUE INDEX extra_unique_content ON messages(content)"
        )
    before = path.read_bytes()
    result = memory_preflight(str(path), mode=PreflightMode.FULL)
    assert path.read_bytes() == before
    assert result.status is PreflightStatus.UNSUPPORTED
    assert result.safe_error_code == PERSISTENCE_SCHEMA_UNSUPPORTED


def test_memory_legacy_extra_unique_is_unsupported(tmp_path: Path) -> None:
    """known legacy + 额外 UNIQUE → 不再是 frozen historical legacy shape →
    UNSUPPORTED（不是 MIGRATION_REQUIRED）。"""
    path = _memory_legacy(tmp_path / "m.db")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE UNIQUE INDEX legacy_extra_unique ON messages(role)"
        )
    before = path.read_bytes()
    result = memory_preflight(str(path), mode=PreflightMode.FULL)
    assert path.read_bytes() == before
    assert result.status is PreflightStatus.UNSUPPORTED
    assert result.safe_error_code == PERSISTENCE_SCHEMA_UNSUPPORTED


def test_memory_trigger_literal_change_is_unsupported(tmp_path: Path) -> None:
    """canonicalizer 必须保留单引号字面量内容：'delete' 改 'DELETE' → UNSUPPORTED。"""
    path = _memory_v2(tmp_path / "m.db")
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER messages_ad")
        conn.execute(
            "CREATE TRIGGER messages_ad AFTER DELETE ON messages "
            "BEGIN INSERT INTO messages_fts(messages_fts, rowid, content, agent_id, role) "
            "VALUES ('DELETE', old.id, old.content, old.agent_id, old.role); END"
        )
    before = path.read_bytes()
    result = memory_preflight(str(path), mode=PreflightMode.FULL)
    assert path.read_bytes() == before
    assert result.status is PreflightStatus.UNSUPPORTED
    assert result.safe_error_code == PERSISTENCE_SCHEMA_UNSUPPORTED


def test_journal_extra_unique_is_unsupported(tmp_path: Path) -> None:
    path = _journal_current(tmp_path / "j.db")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE UNIQUE INDEX j_extra_trace ON runtime_event_journal(trace_id)"
        )
    before = path.read_bytes()
    result = journal_preflight(str(path), mode=PreflightMode.FULL)
    assert path.read_bytes() == before
    assert result.status is PreflightStatus.UNSUPPORTED
    assert result.safe_error_code == PERSISTENCE_SCHEMA_UNSUPPORTED


def test_journal_legacy_extra_unique_is_unsupported(tmp_path: Path) -> None:
    """spanless legacy + 额外 UNIQUE → 不是 legacy → UNSUPPORTED（不是
    MIGRATION_REQUIRED）。"""
    path = _journal_legacy(tmp_path / "j.db")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE UNIQUE INDEX j_legacy_extra ON runtime_event_journal(trace_id)"
        )
    before = path.read_bytes()
    result = journal_preflight(str(path), mode=PreflightMode.FULL)
    assert path.read_bytes() == before
    assert result.status is PreflightStatus.UNSUPPORTED
    assert result.safe_error_code == PERSISTENCE_SCHEMA_UNSUPPORTED


def test_snapshot_extra_unique_is_unsupported(tmp_path: Path) -> None:
    """额外 UNIQUE(run_id) 会阻止同一 Run 保存多个 Snapshot → UNSUPPORTED。"""
    path = _snapshot_current(tmp_path / "s.db")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE UNIQUE INDEX s_extra_run ON runtime_snapshots(run_id)"
        )
    before = path.read_bytes()
    result = snapshot_preflight(str(path), mode=PreflightMode.FULL)
    assert path.read_bytes() == before
    assert result.status is PreflightStatus.UNSUPPORTED
    assert result.safe_error_code == PERSISTENCE_SCHEMA_UNSUPPORTED


# ---------------------------------------------------------------------------
# WP1-B v2 Long-term Memory exact physical signature
# ---------------------------------------------------------------------------


def _memory_v2_drop_long_term_memory(path: Path) -> Path:
    """current v2 → 删掉 long_term_memory（但 user_version 仍为 2）：
    shape 变回 v1、版本标记为 2 → fail closed UNSUPPORTED。"""
    _memory_v2(path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE long_term_memory")
    return path


def _memory_v2_malformed_long_term_memory(path: Path) -> Path:
    """current v2 → long_term_memory 列集合被篡改（丢失 NOT NULL / 换列）。"""
    _memory_v2(path)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            DROP TABLE long_term_memory;
            CREATE TABLE long_term_memory (
                memory_id TEXT PRIMARY KEY,
                memory_type TEXT,
                status TEXT,
                agent_id TEXT NOT NULL,
                memory_scope TEXT NOT NULL,
                canonical_text TEXT,
                payload TEXT,
                logical_key TEXT,
                origin_type TEXT NOT NULL,
                origin_run_id TEXT NOT NULL,
                origin_exchange_id TEXT NOT NULL,
                origin_agent_id TEXT NOT NULL,
                origin_memory_scope TEXT NOT NULL,
                formation_method TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                superseded_by_memory_id TEXT
            );
            CREATE INDEX idx_long_term_memory_agent_scope
                ON long_term_memory(agent_id, memory_scope);
            """
        )
    return path


def test_memory_v2_missing_long_term_memory_is_unsupported(tmp_path: Path) -> None:
    """user_version=2 但 long_term_memory 缺席 → 版本与 shape 不一致 →
    UNSUPPORTED（不是 MIGRATION_REQUIRED）。"""
    path = _memory_v2_drop_long_term_memory(tmp_path / "m.db")
    before = path.read_bytes()
    result = memory_preflight(str(path), mode=PreflightMode.FULL)
    assert path.read_bytes() == before
    assert result.status is PreflightStatus.UNSUPPORTED
    assert result.safe_error_code == PERSISTENCE_SCHEMA_UNSUPPORTED


def test_memory_v2_malformed_long_term_memory_is_unsupported(tmp_path: Path) -> None:
    """v2 exact physical signature 必须覆盖 long_term_memory 的列/约束。"""
    path = _memory_v2_malformed_long_term_memory(tmp_path / "m.db")
    before = path.read_bytes()
    result = memory_preflight(str(path), mode=PreflightMode.FULL)
    assert path.read_bytes() == before
    assert result.status is PreflightStatus.UNSUPPORTED
    assert result.safe_error_code == PERSISTENCE_SCHEMA_UNSUPPORTED


def test_memory_v1_extra_long_term_memory_malformed_is_unsupported(
    tmp_path: Path,
) -> None:
    """v1 core 精确 + 错误 long_term_memory 表 → unknown → UNSUPPORTED。"""
    path = _memory_v1(tmp_path / "m.db")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE long_term_memory (memory_id TEXT PRIMARY KEY)"
        )
    before = path.read_bytes()
    result = memory_preflight(str(path), mode=PreflightMode.FULL)
    assert path.read_bytes() == before
    assert result.status is PreflightStatus.UNSUPPORTED
    assert result.safe_error_code == PERSISTENCE_SCHEMA_UNSUPPORTED
