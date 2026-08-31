"""WP6-C factual Episode Formation contracts."""

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


@pytest.mark.asyncio
async def test_meaningful_success_forms_once_and_failed_run_is_truthful(tmp_path) -> None:
    service = formation(tmp_path)
    first = await service.run_formation(source())
    second = await service.run_formation(source())
    assert first.outcome is EpisodicFormationOutcome.CREATED
    assert second.outcome is EpisodicFormationOutcome.REUSED
    failed = source(failed=True)
    failed = replace(failed, run_id="run-failed")
    result = await service.run_formation(failed)
    assert result.outcome is EpisodicFormationOutcome.CREATED
    record = service._store.get_episode_by_origin_run_id("run-failed", "core_router", "direct")
    assert record.result.terminal_status == "FAILED"
    assert record.result.delivery_status == "NOT_DELIVERED"


def test_trivial_secret_and_cot_boundaries() -> None:
    assembler = EpisodeEvidenceAssembler()
    assert assembler.assemble(source(request="你好")) is None
    record = assembler.assemble(source(request="修复 token=super-secret 的迁移问题"))
    assert record is not None
    assert "super-secret" not in record.canonical_text
    assert LessonProposalValidator.validate("chain_of_thought: hidden") is None
    assert LessonProposalValidator.validate("先检查 schema 再执行迁移") is not None


@pytest.mark.asyncio
async def test_persistence_failure_is_safe_formation_failure(tmp_path, monkeypatch) -> None:
    service = formation(tmp_path)

    def fail(_record):
        raise RuntimeError("db failure")

    monkeypatch.setattr(service._store, "create_or_get_episode", fail)
    result = await service.run_formation(source())
    assert result.outcome is EpisodicFormationOutcome.FAILED


@pytest.mark.asyncio
async def test_specialist_step_episode_is_private_and_idempotent(tmp_path) -> None:
    path = tmp_path / "memory.db"
    MemoryManager(db_path=str(path))
    service = SpecialistEpisodicMemoryFormation(AdvancedMemoryStore(str(path)))
    source = SpecialistEpisodeEvidenceInput(
        run_id="run-specialists", step_id="step-code", specialist_agent_id="code_expert",
        memory_scope="direct", user_request="修复数据库迁移", step_name="实现迁移",
        step_status="SUCCEEDED",
    )
    first = await service.run_formation(source)
    second = await service.run_formation(source)
    assert first.outcome is EpisodicFormationOutcome.CREATED
    assert second.outcome is EpisodicFormationOutcome.REUSED
    record = service._store.get_episode(first.memory_id, "code_expert", "direct")
    assert record.episode_kind.value == "STEP"
    assert record.origin_step_id == "step-code"


@pytest.mark.asyncio
async def test_specialist_failed_terminal_is_truthful_when_evidence_is_available(tmp_path) -> None:
    path = tmp_path / "memory.db"
    MemoryManager(db_path=str(path))
    service = SpecialistEpisodicMemoryFormation(AdvancedMemoryStore(str(path)))
    result = await service.run_formation(SpecialistEpisodeEvidenceInput(
        run_id="run-failed-specialist", step_id="step-tool", specialist_agent_id="tool_expert",
        memory_scope="direct", user_request="执行工具检查", step_name="运行检查",
        step_status="FAILED",
    ))
    assert result.outcome is EpisodicFormationOutcome.CREATED
    assert service._store.get_episode(result.memory_id, "tool_expert", "direct").result.terminal_status == "FAILED"
