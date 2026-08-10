"""WP1-D Chroma LocalAgent collection marker contract tests。

证明：empty unmarked → marker 可初始化；non-empty matching → CURRENT；
non-empty 缺 marker / digest mismatch / dimension mismatch → REBUILD_REQUIRED；
required KB mismatch → never READY；optional KB mismatch → READY_DEGRADED；
startup mismatch 绝不调用 clear_collection；rebuild 成功 marker 最后发布、
失败不发布匹配 marker。

使用真实 repository-local embedding 模型与 tmp Chroma 目录（无网络依赖）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.persistence_migration import (
    PERSISTENCE_PREFLIGHT_FAILED,
    MigrationAction,
    PreflightStatus,
)
from core.runtime import RuntimeInitializationError, RuntimeLifecycleState
from core.knowledge_base.vector_db_manager import VectorDBManager

_REPO_ROOT = Path(__file__).resolve().parents[1]
_EMBEDDING_MODEL_DIR = _REPO_ROOT / "data" / "models" / "Qwen3-Embedding-0.6B"


def _make_manager(chroma_dir: Path) -> VectorDBManager:
    return VectorDBManager(
        db_persist_dir=str(chroma_dir),
        local_model_path=str(_EMBEDDING_MODEL_DIR),
        collection_name="wiki_collection",
    )


def _ingest(manager: VectorDBManager) -> None:
    manager.ingest_chunks(
        [
            {
                "page_content": "LocalAgent persistence migration documentation chunk",
                "metadata": {
                    "source": "docs/migration.md",
                    "chunk_id": "chunk-1",
                    "schema_version": "kb_chunk_schema_v2",
                },
            }
        ]
    )


def _tamper_marker(manager: VectorDBManager, key: str, value) -> None:
    collection = manager.vector_store._collection
    metadata = dict(collection.metadata or {})
    metadata[key] = value
    collection.modify(metadata=metadata)


@pytest.fixture
def chroma_env(monkeypatch, tmp_path):
    if not _EMBEDDING_MODEL_DIR.is_dir():
        pytest.skip(
            "EMBEDDING_MODEL_PREREQUISITE_ABSENT: "
            f"repository-local embedding model missing: {_EMBEDDING_MODEL_DIR}"
        )
    monkeypatch.setenv("LOCAL_AGENT_EMBEDDING_MODEL_PATH", str(_EMBEDDING_MODEL_DIR))
    monkeypatch.setenv("LOCAL_AGENT_CHROMA_DIR", str(tmp_path / "chroma"))
    monkeypatch.setenv("LOCAL_AGENT_MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("LOCAL_AGENT_EVENT_JOURNAL_DB_PATH", str(tmp_path / "journal.db"))
    monkeypatch.setenv("LOCAL_AGENT_SNAPSHOT_DB_PATH", str(tmp_path / "snap.db"))
    monkeypatch.setenv(
        "LOCAL_AGENT_OBSERVABILITY_CHECKPOINT_DB_PATH", str(tmp_path / "obs.db")
    )
    monkeypatch.setenv("LOCAL_AGENT_KB_REQUIRED", "false")
    monkeypatch.setenv("LOCAL_AGENT_REMOTE_API_BASE_URL", "https://example.test/v1")
    return tmp_path


# ---------------------------------------------------------------------------
# Marker primitives
# ---------------------------------------------------------------------------


def test_empty_unmarked_collection_marker_initializes(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path / "chroma")
    result = manager.collection_preflight()
    assert result.status is PreflightStatus.NEW
    assert result.action is MigrationAction.INITIALIZE
    manager.publish_collection_marker()
    marker = manager.read_collection_marker()
    assert marker["localagent_collection_contract_version"] == 1
    assert marker["chunk_schema_version"] == "kb_chunk_schema_v2"
    assert len(marker["embedding_compatibility_digest"]) == 64
    assert isinstance(marker["embedding_dimension"], int)
    assert marker["embedding_dimension"] > 0


def test_non_empty_matching_marker_is_current(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path / "chroma")
    _ingest(manager)
    manager.publish_collection_marker()
    result = manager.collection_preflight()
    assert result.status is PreflightStatus.CURRENT
    assert result.action is MigrationAction.NONE


def test_non_empty_unmarked_is_rebuild_required(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path / "chroma")
    _ingest(manager)
    result = manager.collection_preflight()
    assert result.status is PreflightStatus.REBUILD_REQUIRED
    assert result.action is MigrationAction.REBUILD


def test_digest_mismatch_is_rebuild_required(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path / "chroma")
    _ingest(manager)
    manager.publish_collection_marker()
    _tamper_marker(manager, "embedding_compatibility_digest", "0" * 64)
    result = manager.collection_preflight()
    assert result.status is PreflightStatus.REBUILD_REQUIRED


def test_dimension_mismatch_is_rebuild_required(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path / "chroma")
    _ingest(manager)
    manager.publish_collection_marker()
    _tamper_marker(manager, "embedding_dimension", 42)
    result = manager.collection_preflight()
    assert result.status is PreflightStatus.REBUILD_REQUIRED


def test_rebuild_success_publishes_marker_last(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path / "chroma")
    _ingest(manager)
    manager.publish_collection_marker()
    # 模拟 rebuild：先失效 marker → 清空 → 重新 ingest → 最后发布匹配 marker
    manager.remove_collection_marker()
    marker = manager.read_collection_marker()
    assert marker.get("localagent_collection_contract_version") != 1
    manager.clear_collection()
    assert manager.count() == 0
    _ingest(manager)
    manager.publish_collection_marker()
    assert manager.collection_preflight().status is PreflightStatus.CURRENT
    assert manager.read_collection_marker()["embedding_dimension"] > 0


def test_rebuild_failure_leaves_marker_invalid(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path / "chroma")
    _ingest(manager)
    manager.publish_collection_marker()
    # 模拟 rebuild：invalidate → clear 成功 → ingest 失败 → 不发布新 marker
    manager.remove_collection_marker()
    manager.clear_collection()
    # ingest 失败路径 = 不调用 publish_collection_marker
    marker = manager.read_collection_marker()
    assert marker.get("localagent_collection_contract_version") != 1
    # 非空旧数据已被清空 → 空 collection 重新可初始化（但不假装有匹配 marker）
    assert manager.count() == 0
    assert manager.collection_preflight().status is PreflightStatus.NEW


def test_marker_publish_preserves_non_localagent_metadata(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path / "chroma")
    collection = manager.vector_store._collection
    collection.modify(metadata={"external_team": "docs", "owner": "kb-team"})
    manager.publish_collection_marker()
    marker = manager.read_collection_marker()
    assert marker["localagent_collection_contract_version"] == 1
    metadata = dict(collection.metadata or {})
    assert metadata["external_team"] == "docs"
    assert metadata["owner"] == "kb-team"
    manager.remove_collection_marker()
    metadata = dict(collection.metadata or {})
    assert "localagent_collection_contract_version" not in metadata
    assert metadata["external_team"] == "docs"


# ---------------------------------------------------------------------------
# Startup / lifespan integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_required_kb_mismatch_never_ready(chroma_env, monkeypatch) -> None:
    import server
    from fastapi import FastAPI

    from core.settings import Settings

    chroma_dir = chroma_env / "chroma"
    manager = _make_manager(chroma_dir)
    _ingest(manager)  # non-empty unmarked → REBUILD_REQUIRED

    def factory(*args, **kwargs):
        return manager

    monkeypatch.setattr(server, "VectorDBManager", factory)
    monkeypatch.setenv("LOCAL_AGENT_KB_REQUIRED", "true")
    settings = Settings.load()
    monkeypatch.setattr(server, "settings", settings)
    app = FastAPI()
    with pytest.raises(RuntimeInitializationError) as captured:
        async with server.lifespan(app):
            pass
    assert captured.value.component == "knowledge_base"
    assert app.state.runtime_lifecycle_state is RuntimeLifecycleState.STARTING


@pytest.mark.asyncio
async def test_optional_kb_mismatch_degrades_to_readyd(chroma_env, monkeypatch) -> None:
    import server
    from fastapi import FastAPI

    from core.settings import Settings

    chroma_dir = chroma_env / "chroma"
    manager = _make_manager(chroma_dir)
    _ingest(manager)  # non-empty unmarked → REBUILD_REQUIRED

    def factory(*args, **kwargs):
        return manager

    monkeypatch.setattr(server, "VectorDBManager", factory)
    monkeypatch.setenv("LOCAL_AGENT_KB_REQUIRED", "false")
    settings = Settings.load()
    monkeypatch.setattr(server, "settings", settings)
    app = FastAPI()
    async with server.lifespan(app):
        assert app.state.runtime_lifecycle_state is RuntimeLifecycleState.READY
        assert (
            app.state.runtime_services.startup_dependency_snapshot
            .knowledge_base_degraded
            is True
        )
        assert (
            app.state.chat_service.router.knowledge_base_error
            == "KNOWLEDGE_BASE_REBUILD_REQUIRED"
        )


@pytest.mark.asyncio
async def test_startup_mismatch_never_calls_clear_collection(
    chroma_env, monkeypatch
) -> None:
    import server
    from fastapi import FastAPI

    from core.settings import Settings

    chroma_dir = chroma_env / "chroma"
    manager = _make_manager(chroma_dir)
    _ingest(manager)  # non-empty unmarked → REBUILD_REQUIRED
    clear_calls = []

    class _RecordingVectorDB:
        def __init__(self, delegate):
            self._delegate = delegate

        def collection_preflight(self):
            return self._delegate.collection_preflight()

        def publish_collection_marker(self):
            return self._delegate.publish_collection_marker()

        def count(self):
            return self._delegate.count()

        def clear_collection(self):
            clear_calls.append("called")
            return self._delegate.clear_collection()

    def factory(*args, **kwargs):
        return _RecordingVectorDB(manager)

    monkeypatch.setattr(server, "VectorDBManager", factory)
    monkeypatch.setenv("LOCAL_AGENT_KB_REQUIRED", "false")
    settings = Settings.load()
    monkeypatch.setattr(server, "settings", settings)
    app = FastAPI()
    async with server.lifespan(app):
        assert app.state.runtime_lifecycle_state is RuntimeLifecycleState.READY
        assert (
            app.state.runtime_services.startup_dependency_snapshot
            .knowledge_base_degraded
            is True
        )
    assert clear_calls == []


@pytest.mark.asyncio
async def test_healthy_marked_kb_reaches_ready(chroma_env, monkeypatch) -> None:
    import server
    from fastapi import FastAPI

    from core.settings import Settings

    chroma_dir = chroma_env / "chroma"
    manager = _make_manager(chroma_dir)
    _ingest(manager)
    manager.publish_collection_marker()
    assert manager.collection_preflight().status is PreflightStatus.CURRENT

    def factory(*args, **kwargs):
        return manager

    monkeypatch.setattr(server, "VectorDBManager", factory)
    settings = Settings.load()
    monkeypatch.setattr(server, "settings", settings)
    app = FastAPI()
    async with server.lifespan(app):
        assert app.state.runtime_lifecycle_state is RuntimeLifecycleState.READY
        assert (
            app.state.runtime_services.startup_dependency_snapshot
            .knowledge_base_degraded
            is False
        )
