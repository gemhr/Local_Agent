"""HYBRID_RRF 启动边界与降级 fail-closed 合同测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.knowledge_base import vector_db_manager as vector_module
from core.knowledge_base.production_build import (
    BUILD_PURPOSE_PRODUCTION,
    build_production_generation,
)
from core.agent_router import AgentRouter
from core.runtime.application_services import RuntimeLifecycleState
from core.runtime.hybrid_provenance_validator import (
    HybridProvenanceValidationError,
    ValidatedHybridGeneration,
    validate_active_hybrid_generation,
)
from core.settings import Settings


_SHARED_COLLECTIONS: dict[str, FakeCollection] = {}


class FakeEmbeddings:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    def embed_query(self, query):
        return [0.1, 0.2, 0.3]


class FakeCollection:
    def __init__(self) -> None:
        self.metadata: dict = {}
        self._count = 0

    def count(self) -> int:
        return self._count

    def modify(self, *, metadata) -> None:
        self.metadata = dict(metadata)


class FakeChroma:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        collection_name = kwargs.get("collection_name", "default")
        # 按 collection 名共享状态，模拟本地 Chroma 持久化语义。
        self._collection = _SHARED_COLLECTIONS.setdefault(collection_name, FakeCollection())
        self.added = []

    def delete(self, **kwargs) -> None:
        return None

    def add_documents(self, documents, ids) -> None:
        self._collection._count += len(documents)
        self.added.extend(zip(ids, documents))


@pytest.fixture
def _fake_dense(monkeypatch, tmp_path):
    _SHARED_COLLECTIONS.clear()
    monkeypatch.setattr(vector_module, "HuggingFaceEmbeddings", FakeEmbeddings)
    monkeypatch.setattr(vector_module, "Chroma", FakeChroma)
    yield
    _SHARED_COLLECTIONS.clear()


def _corpus(tmp_path: Path) -> Path:
    source = tmp_path / "corpus"
    source.mkdir()
    (source / "a.md").write_text("# 文档A\n\nCDT 字段映射介绍。", encoding="utf-8")
    return source


def _model(tmp_path: Path) -> Path:
    model = tmp_path / "model"
    model.mkdir()
    (model / "model.safetensors").write_bytes(b"model")
    return model


def _build_valid_generation(tmp_path: Path):
    """构建一次完整 production generation（fake dense + 真实 BM25）。"""
    source = _corpus(tmp_path)
    model = _model(tmp_path)
    chroma_dir = tmp_path / "chroma"
    result = build_production_generation(
        source_dir=source,
        logical_collection_name="kb",
        chroma_dir=chroma_dir,
        embedding_model_path=model,
        chunk_size=1400,
        chunk_overlap=180,
        purpose=BUILD_PURPOSE_PRODUCTION,
        publish_active=True,
    )
    # 复用 build 同款 manager（fake Chroma 无状态，但 marker 在 build 内已校验）。
    manager = vector_module.VectorDBManager(
        str(chroma_dir),
        str(model),
        collection_name=result.dense_collection_name,
    )
    return chroma_dir, model, result, manager


def test_valid_provenance_retains_bm25_runtime_dependency(
    tmp_path, _fake_dense
) -> None:
    chroma_dir, model, result, manager = _build_valid_generation(tmp_path)
    validated = validate_active_hybrid_generation(
        db_manager=manager,
        chroma_dir=chroma_dir,
        logical_collection_name="kb",
        embedding_model_path=model,
    )
    assert validated.generation_id == result.generation_id
    assert validated.dense_collection_name == result.dense_collection_name
    # WP2：完整校验同时保留 application-scoped 的已加载 BM25 artifact。
    assert validated.bm25_artifact.index is not None


def test_missing_active_descriptor_fails_validation(tmp_path, _fake_dense) -> None:
    model = _model(tmp_path)
    manager = vector_module.VectorDBManager(
        str(tmp_path / "empty-chroma"),
        str(model),
        collection_name="la_x",
    )
    with pytest.raises(HybridProvenanceValidationError) as captured:
        validate_active_hybrid_generation(
            db_manager=manager,
            chroma_dir=tmp_path / "empty-chroma",
            logical_collection_name="kb",
            embedding_model_path=model,
        )
    assert captured.value.safe_error_code == "ACTIVE_GENERATION_DESCRIPTOR_MISSING"


def test_valid_generation_exposes_only_loaded_bm25_dependency(tmp_path, _fake_dense) -> None:
    """validator 仅提供已验证依赖，不承担 query/fusion 编排。"""
    chroma_dir, model, result, manager = _build_valid_generation(tmp_path)
    validated = validate_active_hybrid_generation(
        db_manager=manager,
        chroma_dir=chroma_dir,
        logical_collection_name="kb",
        embedding_model_path=model,
    )
    assert validated.bm25_artifact.index is not None
    # 校验结果仍没有 Router/adapter 的查询或 fusion 接口。
    assert not hasattr(validated, "search")
    assert not hasattr(validated, "retrieve")
    assert not hasattr(validated, "fuse")


def test_hybrid_invocation_caps_dense_candidate_budget_at_eight() -> None:
    captured = []

    class CaptureService:
        def execute(self, invocation, **kwargs):
            captured.append(invocation)
            return "result"

    router = object.__new__(AgentRouter)
    router.retrieval_execution_service = CaptureService()
    router.db_manager = SimpleNamespace(collection_name="kb")
    router.retrieval_strategy = "HYBRID_RRF"
    router.rag_top_k = 8

    assert router._execute_knowledge_retrieval("query") == "result"
    assert captured[0].top_k == 8


def _fake_validated_generation() -> ValidatedHybridGeneration:
    return ValidatedHybridGeneration(
        generation_id="12345678-1234-4234-8234-123456789abc",
        provenance_sha256="b" * 64,
        corpus_id="kb",
        dense_collection_name="la_kb_g_generation",
        dense_persist_dir_ref="LOCAL_AGENT_CHROMA_DIR",
        manifest_path=Path("generations/g/manifest.json"),
        bm25_artifact_path=Path("generations/g/bm25_index.json"),
        artifact_metadata_path=Path("generations/g/artifact_metadata.json"),
        provenance=SimpleNamespace(),
        expected_v2_marker={},
        bm25_artifact=SimpleNamespace(index=SimpleNamespace()),
    )


def _set_db_paths(monkeypatch, tmp_path) -> None:
    """与 test_chroma_persistence_contract.chroma_env 一致：全部 DB 指向 tmp。"""
    monkeypatch.setenv("LOCAL_AGENT_CHROMA_DIR", str(tmp_path / "chroma"))
    monkeypatch.setenv("LOCAL_AGENT_MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("LOCAL_AGENT_EVENT_JOURNAL_DB_PATH", str(tmp_path / "journal.db"))
    monkeypatch.setenv("LOCAL_AGENT_SNAPSHOT_DB_PATH", str(tmp_path / "snap.db"))
    monkeypatch.setenv(
        "LOCAL_AGENT_OBSERVABILITY_CHECKPOINT_DB_PATH", str(tmp_path / "obs.db")
    )
    monkeypatch.setenv("LOCAL_AGENT_REMOTE_API_BASE_URL", "https://example.test/v1")


def test_lifespan_hybrid_required_no_longer_fails_as_not_implemented(
    tmp_path, monkeypatch
) -> None:
    import asyncio

    import server

    from fastapi import FastAPI

    from core.runtime import hybrid_provenance_validator

    monkeypatch.setattr(
        hybrid_provenance_validator,
        "validate_active_hybrid_generation",
        lambda **kwargs: _fake_validated_generation(),
    )
    monkeypatch.setattr(
        hybrid_provenance_validator,
        "load_active_hybrid_descriptor",
        lambda **kwargs: SimpleNamespace(dense_collection_name="la_kb_g_generation"),
    )
    _set_db_paths(monkeypatch, tmp_path)
    monkeypatch.setenv("LOCAL_AGENT_RETRIEVAL_STRATEGY", "HYBRID_RRF")
    monkeypatch.setenv("LOCAL_AGENT_KB_REQUIRED", "true")
    monkeypatch.setenv("LOCAL_AGENT_LLM_BACKEND", "local")
    settings = Settings.load()
    monkeypatch.setattr(server, "settings", settings)
    app = FastAPI()

    async def _run():
        async with server.lifespan(app):
            return (
                app.state.runtime_lifecycle_state,
                app.state.chat_service.router.retrieval_execution_service,
            )

    state, service = asyncio.run(_run())
    assert state is RuntimeLifecycleState.READY
    assert service is not None
    assert service.adapter.retrieval_strategy == "HYBRID_RRF"


def test_lifespan_hybrid_optional_degrades_fail_closed_when_dependencies_unavailable(
    tmp_path, monkeypatch
) -> None:
    import asyncio

    import server

    from fastapi import FastAPI

    from core.runtime import hybrid_provenance_validator

    def _unavailable(**kwargs):
        raise HybridProvenanceValidationError("BM25_ARTIFACT_INVALID", "invalid")

    monkeypatch.setattr(hybrid_provenance_validator, "validate_active_hybrid_generation", _unavailable)
    monkeypatch.setattr(
        hybrid_provenance_validator,
        "load_active_hybrid_descriptor",
        lambda **kwargs: SimpleNamespace(dense_collection_name="la_kb_g_generation"),
    )
    _set_db_paths(monkeypatch, tmp_path)
    monkeypatch.setenv("LOCAL_AGENT_RETRIEVAL_STRATEGY", "HYBRID_RRF")
    monkeypatch.setenv("LOCAL_AGENT_KB_REQUIRED", "false")
    monkeypatch.setenv("LOCAL_AGENT_LLM_BACKEND", "local")
    settings = Settings.load()
    monkeypatch.setattr(server, "settings", settings)
    app = FastAPI()

    async def _run():
        async with server.lifespan(app):
            return (
                app.state.runtime_lifecycle_state,
                app.state.runtime_services.startup_dependency_snapshot.knowledge_base_degraded,
                app.state.chat_service.router.knowledge_base_error,
            )

    state, kb_degraded, kb_error = asyncio.run(_run())
    # optional KB：允许 degraded 启动；请求边界使用 Hybrid unavailable safe code。
    assert state is RuntimeLifecycleState.READY
    assert kb_degraded is True
    assert kb_error == "HYBRID_STRATEGY_UNAVAILABLE"
