"""WP1-D explicit persistence migration contract tests。

证明：all-current → no-op；缺 --backup-confirmed → refusal 且零 mutation；
Memory legacy → v1；current-unversioned → adoption；Journal legacy →
current physical；Checkpoint incompatible → recreate；rerun → no-op；
transaction 失败 → rollback（version/shape 不前移）；partial cross-store
completion → rerun safe；unsupported anywhere → 不开始任何 mutation。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

import core.memory_manager as memory_module
import core.runtime.event_journal_store as journal_module
from core.memory_manager import MEMORY_SCHEMA_VERSION, MemoryManager
from core.persistence_migration import (
    PERSISTENCE_MIGRATION_FAILED,
    PersistenceError,
    PersistencePaths,
    PreflightMode,
    PreflightStatus,
    run_persistence_migration,
    run_persistence_preflight,
)
from core.runtime.event_consumer import (
    SQLiteEventConsumptionCheckpointStore,
    checkpoint_preflight,
    checkpoint_recreate,
)
from core.runtime.event_journal_store import (
    SQLiteRunEventJournal,
    journal_migrate,
    journal_preflight,
)
from test_persistence_preflight import (
    _checkpoint_current,
    _checkpoint_incompatible,
    _journal_current,
    _journal_legacy,
    _memory_legacy,
    _memory_unversioned_current,
    _memory_v1,
    _memory_v2,
    _paths,
    _snapshot_current,
)


def _memory_columns(path: Path) -> frozenset[str]:
    with sqlite3.connect(path) as conn:
        return frozenset(
            row[1] for row in conn.execute("PRAGMA table_info(messages)")
        )


def _memory_tables(path: Path) -> frozenset[str]:
    with sqlite3.connect(path) as conn:
        return frozenset(
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        )


def _user_version(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _journal_columns(path: Path) -> frozenset[str]:
    with sqlite3.connect(path) as conn:
        return frozenset(
            row[1]
            for row in conn.execute("PRAGMA table_info(runtime_event_journal)")
        )


def _checkpoint_rows(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        return int(
            conn.execute(
                "SELECT COUNT(1) FROM event_consumption_checkpoint"
            ).fetchone()[0]
        )


# ---------------------------------------------------------------------------
# all current → no-op；rerun → no-op
# ---------------------------------------------------------------------------


def test_all_current_migrate_is_noop(tmp_path: Path) -> None:
    memory = _memory_v2(tmp_path / "memory.db")
    journal = _journal_current(tmp_path / "journal.db")
    snapshot = tmp_path / "snap.db"  # absent → NEW（snapshot enabled 但未初始化）
    checkpoint = _checkpoint_current(tmp_path / "checkpoint.db")
    paths = _paths(memory, journal, snapshot, checkpoint)
    outcome = run_persistence_migration(paths, backup_confirmed=True)
    assert not outcome.failed
    assert all(result.action.value == "NONE" for result in outcome.results)
    # rerun → 同样 no-op
    second = run_persistence_migration(paths, backup_confirmed=True)
    assert not second.failed


# ---------------------------------------------------------------------------
# backup confirmation
# ---------------------------------------------------------------------------


def test_migrate_without_backup_confirmed_refuses_mutation(tmp_path: Path) -> None:
    memory = _memory_legacy(tmp_path / "memory.db")
    journal = _journal_current(tmp_path / "journal.db")
    checkpoint = _checkpoint_current(tmp_path / "checkpoint.db")
    paths = _paths(memory, journal, None, checkpoint)
    with pytest.raises(PersistenceError) as caught:
        run_persistence_migration(paths, backup_confirmed=False)
    assert caught.value.error_code == PERSISTENCE_MIGRATION_FAILED
    # 零 mutation：legacy 保持 legacy
    assert _user_version(memory) == 0
    assert "memory_scope" not in _memory_columns(memory)


def test_migrate_all_new_does_not_require_backup(tmp_path: Path) -> None:
    memory = tmp_path / "memory.db"
    journal = tmp_path / "journal.db"
    checkpoint = tmp_path / "checkpoint.db"
    outcome = run_persistence_migration(
        _paths(memory, journal, None, checkpoint), backup_confirmed=False
    )
    assert not outcome.failed
    assert not memory.exists()
    assert not journal.exists()
    assert not checkpoint.exists()


# ---------------------------------------------------------------------------
# Memory migration
# ---------------------------------------------------------------------------


def test_memory_legacy_migrates_to_v2_and_preserves_rows(tmp_path: Path) -> None:
    path = _memory_legacy(tmp_path / "memory.db")
    with sqlite3.connect(path) as conn:
        content_before = conn.execute(
            "SELECT content FROM messages WHERE agent_id='agent-a'"
        ).fetchone()[0]

    outcome = run_persistence_migration(
        _paths(path, _journal_current(tmp_path / "j.db"), None, _checkpoint_current(tmp_path / "c.db")),
        backup_confirmed=True,
    )
    memory_result = next(r for r in outcome.results if r.store_id.value == "MEMORY")
    assert memory_result.committed is True

    assert _user_version(path) == 5
    columns = _memory_columns(path)
    assert {"memory_scope", "exchange_id", "run_id", "sequence"} <= columns
    assert "message_exchanges" in _memory_tables(path)
    assert "long_term_memory" in _memory_tables(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT content FROM messages WHERE agent_id='agent-a'"
        ).fetchone()[0] == content_before
        assert conn.execute(
            "SELECT memory_scope FROM messages WHERE agent_id='agent-a'"
        ).fetchone()[0] == "direct"


def test_memory_current_unversioned_adoption(tmp_path: Path) -> None:
    path = _memory_unversioned_current(tmp_path / "memory.db")
    assert _user_version(path) == 0
    run_persistence_migration(
        _paths(path, _journal_current(tmp_path / "j.db"), None, _checkpoint_current(tmp_path / "c.db")),
        backup_confirmed=True,
    )
    assert _user_version(path) == 5


def test_memory_migration_rollback_on_partial_failure(tmp_path: Path, monkeypatch) -> None:
    path = _memory_legacy(tmp_path / "memory.db")

    def boom(conn):
        raise sqlite3.Error("forced schema failure")

    monkeypatch.setattr(memory_module, "_create_current_memory_schema", boom)
    outcome = run_persistence_migration(
        _paths(path, _journal_current(tmp_path / "j.db"), None, _checkpoint_current(tmp_path / "c.db")),
        backup_confirmed=True,
    )
    assert outcome.failed
    memory_result = next(r for r in outcome.results if r.store_id.value == "MEMORY")
    assert memory_result.committed is False
    assert memory_result.safe_error_code == PERSISTENCE_MIGRATION_FAILED
    monkeypatch.undo()
    # rollback：user_version 不前移、shape 不前移、正文保留
    assert _user_version(path) == 0
    assert "memory_scope" not in _memory_columns(path)
    assert "message_exchanges" not in _memory_tables(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT content FROM messages WHERE agent_id='agent-a'"
        ).fetchone()[0] == "legacy-content"
    # 修复后 rerun 成功
    run_persistence_migration(
        _paths(path, _journal_current(tmp_path / "j.db"), None, _checkpoint_current(tmp_path / "c.db")),
        backup_confirmed=True,
    )
    assert _user_version(path) == 5


# ---------------------------------------------------------------------------
# WP1-B v1 → v2 explicit additive migration
# ---------------------------------------------------------------------------


def test_memory_v1_migrates_to_v2_and_preserves_conversation(tmp_path: Path) -> None:
    """v1 → v2：保留 messages/summaries/exchanges/FTS，新增 long_term_memory。"""
    path = _memory_v1(tmp_path / "memory.db")
    assert _user_version(path) == 1
    assert "long_term_memory" not in _memory_tables(path)
    # 造 exchange + summary 数据（v1 已含 direct 消息）
    manager = MemoryManager(db_path=str(path))
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO conversation_summaries(agent_id, summary, last_message_id)"
            " VALUES ('agent-a', 'rolling-summary', 1)"
        )
        conn.commit()
    manager.append_exchange_atomic(
        "agent-a", "direct", "exchange-user", "exchange-assistant", run_id="run-e1"
    )

    outcome = run_persistence_migration(
        _paths(path, _journal_current(tmp_path / "j.db"), None, _checkpoint_current(tmp_path / "c.db")),
        backup_confirmed=True,
    )
    memory_result = next(r for r in outcome.results if r.store_id.value == "MEMORY")
    assert memory_result.committed is True

    assert _user_version(path) == 5
    assert "long_term_memory" in _memory_tables(path)
    with sqlite3.connect(path) as conn:
        # conversation rows 保留
        assert conn.execute(
            "SELECT COUNT(1) FROM messages WHERE agent_id='agent-a' AND content='hello'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(1) FROM messages WHERE agent_id='agent-a' AND content='exchange-user'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(1) FROM message_exchanges WHERE run_id='run-e1'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT summary FROM conversation_summaries WHERE agent_id='agent-a'"
        ).fetchone()[0] == "rolling-summary"
        # FTS 仍可检索（触发器保留）
        assert conn.execute(
            "SELECT COUNT(1) FROM messages_fts WHERE messages_fts MATCH 'hello'"
        ).fetchone()[0] == 1
        # long_term_memory 为空但结构存在
        assert conn.execute(
            "SELECT COUNT(1) FROM long_term_memory"
        ).fetchone()[0] == 0
    # 二次 preflight 变为 CURRENT；rerun no-op
    assert run_persistence_preflight(
        _paths(path, _journal_current(tmp_path / "j.db"), None, _checkpoint_current(tmp_path / "c.db")),
    )[0].status is PreflightStatus.CURRENT
    second = run_persistence_migration(
        _paths(path, _journal_current(tmp_path / "j.db"), None, _checkpoint_current(tmp_path / "c.db")),
        backup_confirmed=True,
    )
    memory_rerun = next(r for r in second.results if r.store_id.value == "MEMORY")
    assert memory_rerun.action.value == "NONE"


def test_memory_v1_to_v2_rollback_on_failure(tmp_path: Path, monkeypatch) -> None:
    """v1→v2 失败 → 完整 rollback：version 不前移、无 long_term_memory、
    conversation 保留；修复后 rerun 成功。"""
    path = _memory_v1(tmp_path / "memory.db")

    def boom(conn):
        raise sqlite3.Error("forced long_term_memory failure")

    monkeypatch.setattr(memory_module, "_create_long_term_memory_schema", boom)
    outcome = run_persistence_migration(
        _paths(path, _journal_current(tmp_path / "j.db"), None, _checkpoint_current(tmp_path / "c.db")),
        backup_confirmed=True,
    )
    assert outcome.failed
    memory_result = next(r for r in outcome.results if r.store_id.value == "MEMORY")
    assert memory_result.committed is False
    assert memory_result.safe_error_code == PERSISTENCE_MIGRATION_FAILED
    monkeypatch.undo()
    # rollback：仍为 v1，conversation 保留
    assert _user_version(path) == 1
    assert "long_term_memory" not in _memory_tables(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT content FROM messages WHERE agent_id='agent-a'"
        ).fetchone()[0] == "hello"
    # 修复后 rerun 成功 → v2
    run_persistence_migration(
        _paths(path, _journal_current(tmp_path / "j.db"), None, _checkpoint_current(tmp_path / "c.db")),
        backup_confirmed=True,
    )
    assert _user_version(path) == 5
    assert "long_term_memory" in _memory_tables(path)


def test_memory_v1_unversioned_migrates_to_v2(tmp_path: Path) -> None:
    """v1 shape + user_version=0 也按 v1→v2 additive 迁移（保持一致 fail-safe）。"""
    path = _memory_v1(tmp_path / "memory.db")
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version = 0")
    assert run_persistence_preflight(
        _paths(path, _journal_current(tmp_path / "j.db"), None, _checkpoint_current(tmp_path / "c.db")),
    )[0].status is PreflightStatus.MIGRATION_REQUIRED
    run_persistence_migration(
        _paths(path, _journal_current(tmp_path / "j.db"), None, _checkpoint_current(tmp_path / "c.db")),
        backup_confirmed=True,
    )
    assert _user_version(path) == 5
    assert "long_term_memory" in _memory_tables(path)


def test_journal_legacy_migrates_to_current_and_preserves_rows(tmp_path: Path) -> None:
    path = _journal_legacy(tmp_path / "j.db")
    # 构造一条真实 v1 legacy row（v1 digest 语义）
    import hashlib
    import json
    from datetime import UTC, datetime

    safe_payload = {"text": "legacy-safe-payload"}
    payload_digest = hashlib.sha256(
        json.dumps(safe_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    legacy_source = {
        "journal_schema_version": 1,
        "event_schema_version": 1,
        "event_id": "legacy-ev-1",
        "run_id": "legacy-run-1",
        "trace_id": "legacy-trace-1",
        "sequence": 1,
        "emitted_at": "2026-01-01T00:00:00+00:00",
        "event_type": "RUN_STARTED",
        "component": "legacy",
        "step_id": None,
        "step_sequence": None,
        "safe_payload": safe_payload,
        "payload_digest": payload_digest,
    }
    event_digest = hashlib.sha256(
        json.dumps(legacy_source, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO runtime_event_journal VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                1, 1, "legacy-ev-1", "legacy-run-1", "legacy-trace-1", 1,
                "2026-01-01T00:00:00+00:00", datetime.now(UTC).isoformat(),
                "RUN_STARTED", "legacy", None, None,
                json.dumps(safe_payload, ensure_ascii=False),
                payload_digest, event_digest,
            ),
        )

    outcome = run_persistence_migration(
        _paths(_memory_v2(tmp_path / "memory.db"), path, None, _checkpoint_current(tmp_path / "c.db")),
        backup_confirmed=True,
    )
    journal_result = next(r for r in outcome.results if r.store_id.value == "EVENT_JOURNAL")
    assert journal_result.committed is True

    columns = _journal_columns(path)
    assert "span_id" in columns
    assert "parent_span_id" in columns
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT journal_schema_version, event_schema_version, event_id, "
            "sequence, safe_payload, payload_digest, event_digest "
            "FROM runtime_event_journal WHERE event_id = 'legacy-ev-1'"
        ).fetchone()
        assert row[0] == 1  # journal_schema_version 未改写
        assert row[1] == 1  # event_schema_version 未改写
        assert row[2] == "legacy-ev-1"
        assert row[3] == 1
        assert json.loads(row[4]) == safe_payload
        assert row[5] == payload_digest
        assert row[6] == event_digest
    assert journal_preflight(str(path)).status is PreflightStatus.CURRENT


def test_journal_unsupported_row_version_blocks_migration(tmp_path: Path) -> None:
    path = _journal_legacy(tmp_path / "j.db")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO runtime_event_journal VALUES (99, 2, 'e1', 'r1', 't1', 1, "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', "
            "'RUN_STARTED', 'c', NULL, NULL, '{}', 'd1', 'd2')"
        )
    with pytest.raises(PersistenceError):
        run_persistence_migration(
            _paths(_memory_v2(tmp_path / "memory.db"), path, None, _checkpoint_current(tmp_path / "c.db")),
            backup_confirmed=True,
        )
    # 未修改
    assert "span_id" not in _journal_columns(path)
    with sqlite3.connect(path) as conn:
        assert int(
            conn.execute("SELECT COUNT(1) FROM runtime_event_journal").fetchone()[0]
        ) == 1


# ---------------------------------------------------------------------------
# Checkpoint recreate
# ---------------------------------------------------------------------------


def test_checkpoint_incompatible_recreate_then_current(tmp_path: Path) -> None:
    path = _checkpoint_incompatible(tmp_path / "c.db")
    assert checkpoint_preflight(str(path)).status is PreflightStatus.MIGRATION_REQUIRED

    memory = _memory_v2(tmp_path / "memory.db")
    journal = _journal_current(tmp_path / "journal.db")
    outcome = run_persistence_migration(
        _paths(memory, journal, None, path), backup_confirmed=True
    )
    checkpoint_result = next(
        r for r in outcome.results if r.store_id.value == "OBSERVABILITY_CHECKPOINT"
    )
    assert checkpoint_result.committed is True

    assert checkpoint_preflight(str(path)).status is PreflightStatus.CURRENT
    # 两个 consumer 可重新写入
    store = SQLiteEventConsumptionCheckpointStore(str(path))
    store.close()


def test_checkpoint_recreate_does_not_touch_memory_or_journal(tmp_path: Path) -> None:
    memory = _memory_v2(tmp_path / "memory.db")
    journal = _journal_current(tmp_path / "journal.db")
    checkpoint = _checkpoint_incompatible(tmp_path / "checkpoint.db")
    memory_bytes = memory.read_bytes()
    journal_bytes = journal.read_bytes()
    run_persistence_migration(
        _paths(memory, journal, None, checkpoint), backup_confirmed=True
    )
    assert memory.read_bytes() == memory_bytes
    assert journal.read_bytes() == journal_bytes


# ---------------------------------------------------------------------------
# Partial cross-store completion → rerun safe
# ---------------------------------------------------------------------------


def test_partial_completion_then_rerun_is_safe(tmp_path: Path, monkeypatch) -> None:
    memory = _memory_legacy(tmp_path / "memory.db")
    journal = _journal_legacy(tmp_path / "journal.db")
    checkpoint = _checkpoint_current(tmp_path / "checkpoint.db")

    def boom(_path):
        raise PersistenceError(PERSISTENCE_MIGRATION_FAILED, "forced journal failure")

    monkeypatch.setattr(journal_module, "journal_migrate", boom)
    outcome = run_persistence_migration(
        _paths(memory, journal, None, checkpoint), backup_confirmed=True
    )
    assert outcome.failed
    memory_result = next(r for r in outcome.results if r.store_id.value == "MEMORY")
    journal_result = next(r for r in outcome.results if r.store_id.value == "EVENT_JOURNAL")
    assert memory_result.committed is True
    assert journal_result.committed is False
    assert journal_result.safe_error_code == PERSISTENCE_MIGRATION_FAILED
    monkeypatch.undo()

    # Memory 已 commit；Journal 仍 legacy。rerun 从实际 facts 继续。
    assert _user_version(memory) == 5
    rerun = run_persistence_migration(
        _paths(memory, journal, None, checkpoint), backup_confirmed=True
    )
    assert not rerun.failed
    memory_rerun = next(r for r in rerun.results if r.store_id.value == "MEMORY")
    journal_rerun = next(r for r in rerun.results if r.store_id.value == "EVENT_JOURNAL")
    assert memory_rerun.action.value == "NONE"
    assert journal_rerun.committed is True


# ---------------------------------------------------------------------------
# Unsupported anywhere → no mutation starts
# ---------------------------------------------------------------------------


def test_unsupported_anywhere_prevents_all_mutation(tmp_path: Path) -> None:
    memory = _memory_legacy(tmp_path / "memory.db")
    journal = _journal_current(tmp_path / "journal.db")
    checkpoint = _checkpoint_current(tmp_path / "checkpoint.db")
    # future memory（user_version=3）→ UNSUPPORTED
    with sqlite3.connect(memory) as conn:
        conn.execute("PRAGMA user_version = 6")
    with pytest.raises(PersistenceError):
        run_persistence_migration(
            _paths(memory, journal, None, checkpoint), backup_confirmed=True
        )
    # 零 mutation：legacy memory 保持 legacy
    assert _user_version(memory) == 6
    assert "memory_scope" not in _memory_columns(memory)


# ---------------------------------------------------------------------------
# Backup / Restore contract（stopped-server synthetic set）
# ---------------------------------------------------------------------------


def test_offline_copy_backup_restore_full_preflight_pass(tmp_path: Path) -> None:
    """stopped-server synthetic data set → 复制 MUST_BACKUP 集合 → 针对 copy
    的显式 full preflight PASS（offline copy + preflight contract，不是
    automatic backup tooling）。"""
    import shutil

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    memory = _memory_v2(source_dir / "memory.db")
    journal = _journal_current(source_dir / "journal.db")
    snapshot = _snapshot_current(source_dir / "snapshot.db")
    kb_source = source_dir / "kb"
    kb_source.mkdir()
    (kb_source / "doc.md").write_text("# knowledge source", encoding="utf-8")

    restore_dir = tmp_path / "restore"
    restore_dir.mkdir()
    for name in ("memory.db", "journal.db", "snapshot.db"):
        shutil.copy2(source_dir / name, restore_dir / name)

    results = run_persistence_preflight(
        _paths(
            restore_dir / "memory.db",
            restore_dir / "journal.db",
            restore_dir / "snapshot.db",
            restore_dir / "checkpoint.db",
        ),
        mode=PreflightMode.FULL,
    )
    assert all(
        result.status in {PreflightStatus.CURRENT, PreflightStatus.NEW}
        for result in results
    )


def test_restore_newer_memory_fails_closed_without_mutation(tmp_path: Path) -> None:
    """恢复 wrong/newer 版本（user_version=3）→ full preflight fail closed，
    且不修改 fixture。"""
    memory = _memory_v2(tmp_path / "memory.db")
    with sqlite3.connect(memory) as conn:
        conn.execute("PRAGMA user_version = 6")
    before = memory.read_bytes()
    results = run_persistence_preflight(
        _paths(
            memory,
            _journal_current(tmp_path / "journal.db"),
            None,
            _checkpoint_current(tmp_path / "checkpoint.db"),
        ),
        mode=PreflightMode.FULL,
    )
    memory_result = next(r for r in results if r.store_id.value == "MEMORY")
    assert memory_result.status is PreflightStatus.UNSUPPORTED
    assert memory.read_bytes() == before


# ---------------------------------------------------------------------------
# P1 Remediation：malformed from-state 拒绝 mutation
# ---------------------------------------------------------------------------


def test_migrate_refuses_malformed_memory_from_state(tmp_path: Path) -> None:
    """user_version=0 但 physical signature malformed（约束错误）→
    coordinator FULL preflight 判 UNSUPPORTED → 不开始任何 mutation。"""
    from test_persistence_preflight import _memory_malformed_constraints

    path = _memory_malformed_constraints(tmp_path / "memory.db")
    before = path.read_bytes()
    with pytest.raises(PersistenceError):
        run_persistence_migration(
            _paths(
                path,
                _journal_current(tmp_path / "journal.db"),
                None,
                _checkpoint_current(tmp_path / "checkpoint.db"),
            ),
            backup_confirmed=True,
        )
    assert path.read_bytes() == before


def test_migrate_refuses_malformed_journal_from_state(tmp_path: Path) -> None:
    """current 列名但无约束的 Journal → UNSUPPORTED → 零 mutation。"""
    from test_persistence_preflight import _journal_malformed

    path = _journal_malformed(tmp_path / "journal.db")
    before = path.read_bytes()
    with pytest.raises(PersistenceError):
        run_persistence_migration(
            _paths(
                _memory_v2(tmp_path / "memory.db"),
                path,
                None,
                _checkpoint_current(tmp_path / "checkpoint.db"),
            ),
            backup_confirmed=True,
        )
    assert path.read_bytes() == before
