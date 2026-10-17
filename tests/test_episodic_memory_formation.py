"""仅保留仍有效的纯合同/本地测试与共享 fixture；废弃 agent-only 节点见 .ai/archive/tests/scoped_memory_lifecycle/。"""

from __future__ import annotations

import pytest
from dataclasses import replace

from core.advanced_memory import AdvancedMemoryStore
from core.memory_manager import MemoryManager
from core.runtime.episodic_memory_formation import (
    EpisodeEvidenceAssembler,
    EpisodeEvidenceInput,
    EpisodicFormationOutcome,
    EpisodicMemoryFormation,
    LessonProposalValidator,
    SpecialistEpisodeEvidenceInput,
    SpecialistEpisodicMemoryFormation,
)
from core.runtime.state import AgentState, RunStatus, StopReason


def source(*, request: str = "修复数据库迁移", failed: bool = False) -> EpisodeEvidenceInput:
    state = AgentState.for_run_context("run-episode")
    state.mark_running()
    state.add_step("step-1", "执行数据库迁移")
    state.start_step("step-1")
    if failed:
        state.fail_step("step-1", error_code="TOOL_FAILED", error_message="safe failure")
        terminal = RunStatus.FAILED
        reason = StopReason.UNHANDLED_ERROR
    else:
        state.succeed_step("step-1")
        terminal = RunStatus.SUCCEEDED
        reason = StopReason.COMPLETED
    return EpisodeEvidenceInput(
        run_id="run-episode",
        agent_id="core_router",
        memory_scope="direct",
        user_request=request,
        plan_goal="安全完成数据库迁移",
        agent_state=state,
        terminal_status=terminal,
        stop_reason=reason,
        delivery_status="NOT_DELIVERED" if failed else "DELIVERED",
    )


def formation(tmp_path) -> EpisodicMemoryFormation:
    path = tmp_path / "memory.db"
    MemoryManager(db_path=str(path))
    return EpisodicMemoryFormation(AdvancedMemoryStore(str(path)))



def test_trivial_secret_and_cot_boundaries() -> None:
    assembler = EpisodeEvidenceAssembler()
    assert assembler.assemble(source(request="你好")) is None
    record = assembler.assemble(source(request="修复 token=super-secret 的迁移问题"))
    assert record is not None
    assert "super-secret" not in record.canonical_text
    assert LessonProposalValidator.validate("chain_of_thought: hidden") is None
    assert LessonProposalValidator.validate("先检查 schema 再执行迁移") is not None
