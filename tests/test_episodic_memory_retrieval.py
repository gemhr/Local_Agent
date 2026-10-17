"""仅保留仍有效的纯合同/本地测试与共享 fixture；废弃 agent-only 节点见 .ai/archive/tests/scoped_memory_lifecycle/。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.advanced_memory import (
    AdvancedMemoryStore, EpisodeGoal, EpisodeGoalAuthority, EpisodeObservation,
    EpisodeResult, EpisodeSituation, EpisodicMemoryRecord, MemoryOrigin,
)
from core.memory_manager import MemoryManager
from core.runtime.memory_retrieval import EPISODIC_MAX_CONTEXT_CHARS, MemoryRetrievalService
from core.runtime.memory_authorization import MemoryAccessPrincipal
from core.runtime.episodic_memory_formation import EpisodeEvidenceInput, EpisodicMemoryFormation
from core.runtime.model_context import (
    ContextBuildRequest, ContextBuilder, ContextSourceType, ContextTrustLevel,
)
from core.runtime.state import AgentState, RunStatus, StopReason


def _store(tmp_path) -> AdvancedMemoryStore:
    path = tmp_path / "episode-retrieval.db"
    MemoryManager(db_path=str(path))
    return AdvancedMemoryStore(str(path))


def _episode(memory_id: str, run_id: str, text: str, *, agent_id: str = "core_router", scope: str = "direct", status: str = "SUCCEEDED") -> EpisodicMemoryRecord:
    created = datetime.now(UTC) + timedelta(seconds=int(memory_id[-1]) if memory_id[-1].isdigit() else 0)
    return EpisodicMemoryRecord(
        memory_id=memory_id, agent_id=agent_id, memory_scope=scope, origin_run_id=run_id,
        situation=EpisodeSituation(text), goal=EpisodeGoal("完成当前任务", EpisodeGoalAuthority.USER_PROVIDED),
        observations=(EpisodeObservation("STEP", "work", status),),
        result=EpisodeResult(status, "COMPLETED", "DELIVERED"),
        origin=MemoryOrigin("runtime_terminal", run_id, f"exchange-{run_id}", agent_id, scope, "EPISODIC_V1"),
        created_at=created, updated_at=created,
    )
