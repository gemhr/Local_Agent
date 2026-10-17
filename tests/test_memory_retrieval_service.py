"""仅保留仍有效的纯合同/本地测试与共享 fixture；废弃 agent-only 节点见 .ai/archive/tests/scoped_memory_lifecycle/。"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from core.advanced_memory import (
    AdvancedMemoryStore,
    MemoryOrigin,
    MemoryStatus,
    MemoryType,
    SemanticMemoryRecord,
)
from core.memory_manager import MemoryManager
from core.runtime.memory_retrieval import (
    MEMORY_DIRECT_SCOPE,
    MemoryContextBundle,
    MemoryRetrievalError,
    MemoryRetrievalErrorCode,
    MemoryRetrievalService,
)
from core.runtime.memory_authorization import MemoryAccessPrincipal
from core.runtime.model_context import ContextSourceType, ContextTrustLevel


def origin(rid="run-1", eid="exchange-1") -> MemoryOrigin:
    return MemoryOrigin(
        origin_type="DELIVERED_EXCHANGE",
        origin_run_id=rid,
        origin_exchange_id=eid,
        origin_agent_id="core_router",
        origin_memory_scope="direct",
        formation_method="HYBRID",
    )


def make_store(tmp_path, name="memory.db") -> AdvancedMemoryStore:
    db_path = str(tmp_path / name)
    MemoryManager(db_path=db_path)
    return AdvancedMemoryStore(db_path)


def make_record(
    memory_id: str,
    *,
    canonical_text: str,
    value=None,
    key: str | None = None,
    agent_id: str = "core_router",
    memory_scope: str = "direct",
    created_at: str = "2026-01-01T00:00:00+00:00",
    payload: dict | None = None,
) -> SemanticMemoryRecord:
    ts = datetime.fromisoformat(created_at)
    if payload is None:
        payload = {"value": value}
    return SemanticMemoryRecord(
        memory_id=memory_id,
        agent_id=agent_id,
        memory_scope=memory_scope,
        canonical_text=canonical_text,
        payload=payload,
        origin=origin(),
        memory_type=MemoryType.SEMANTIC,
        status=MemoryStatus.ACTIVE,
        logical_key=key,
        created_at=ts,
        updated_at=ts,
    )


def service(store: AdvancedMemoryStore, **kw) -> MemoryRetrievalService:
    return MemoryRetrievalService(store, **kw)


def retrieve(store: AdvancedMemoryStore, query: str, **kw) -> MemoryContextBundle:
    return service(store, **kw).retrieve(
        requester=MemoryAccessPrincipal("core_router"),
        target_owner_agent_id="core_router",
        memory_scope=MEMORY_DIRECT_SCOPE,
        query=query,
    )


# ---------------------------------------------------------------------------
# Lexical relevance / registered / OPEN
# ---------------------------------------------------------------------------









# ---------------------------------------------------------------------------
# Scope isolation
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# ACTIVE only / lifecycle integration
# ---------------------------------------------------------------------------





# ---------------------------------------------------------------------------
# Deterministic ranking / tie-break / top-K / budget
# ---------------------------------------------------------------------------









# ---------------------------------------------------------------------------
# Malformed rows / failure semantics
# ---------------------------------------------------------------------------






def test_invalid_configuration_fails_closed(tmp_path) -> None:
    store = make_store(tmp_path)
    with pytest.raises(MemoryRetrievalError):
        MemoryRetrievalService(store, top_k=0)
    with pytest.raises(MemoryRetrievalError):
        MemoryRetrievalService(store, candidate_limit=-1)
    with pytest.raises(MemoryRetrievalError):
        MemoryRetrievalService(store, max_memory_context_chars=0)
    with pytest.raises(MemoryRetrievalError):
        MemoryRetrievalService(object())  # type: ignore[arg-type]
