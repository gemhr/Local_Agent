"""WP6-C post-terminal Episodic Memory formation.

This component owns factual evidence projection only.  It never decides a Run
terminal, invokes a model, reads raw journal content, or changes delivery.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from uuid import uuid4

from core.advanced_memory import (
    AdvancedMemoryStore,
    EpisodeGoal,
    EpisodeGoalAuthority,
    EpisodeKind,
    EpisodeObservation,
    EpisodeResult,
    EpisodeSituation,
    EpisodicMemoryRecord,
    MemoryDomainError,
    MemoryOrigin,
)
from core.runtime.events import (
    EpisodicMemoryFormationCompletedPayload,
    RuntimeEventType,
)
from core.runtime.memory_authorization import (
    MemoryAccessAuthorizer,
    MemoryAccessPrincipal,
)
from core.runtime.state import AgentState, RunStatus, StopReason, StepStatus


_TRIVIAL = re.compile(r"^(?:你好|您好|嗨|hi|hello|谢谢|感谢|再见)[!！,.，。\s]*$", re.I)
_SECRET = re.compile(r"(?i)\b(api[_-]?key|authorization|token|password)\s*[:=]\s*[^\s,;]+|\bbearer\s+[^\s,;]+")
_PATH = re.compile(r"(?:(?:[A-Za-z]:\\|/home/|/Users/)[^\s,;]+)")
_COT = re.compile(r"(?i)\b(chain[_ -]?of[_ -]?thought|reasoning|internal_reasoning|scratchpad|hidden_state|model_thought)\b")
_MAX_SOURCE_CHARS = 400


class EpisodicFormationOutcome(str, Enum):
    CREATED = "CREATED"
    REUSED = "REUSED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class LessonStatus(str, Enum):
    ABSENT = "ABSENT"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class EpisodeEvidenceInput:
    run_id: str
    agent_id: str
    memory_scope: str
    user_request: str | None
    plan_goal: str | None
    agent_state: AgentState
    terminal_status: RunStatus
    stop_reason: StopReason
    delivery_status: str


@dataclass(frozen=True, slots=True)
class EpisodicFormationResult:
    run_id: str
    outcome: EpisodicFormationOutcome
    memory_id: str | None = None
    lesson_status: LessonStatus = LessonStatus.ABSENT
    safe_reason: str | None = None


def _sanitize(value: str) -> str:
    text = " ".join(value.split())
    text = _SECRET.sub("[REDACTED]", text)
    text = _PATH.sub("[PATH_REDACTED]", text)
    return text[:_MAX_SOURCE_CHARS]


class LessonProposalValidator:
    """Optional lesson-only boundary for future scripted/model proposals."""

    @staticmethod
    def validate(proposal: object) -> str | None:
        if not isinstance(proposal, str) or not proposal.strip():
            return None
        candidate = _sanitize(proposal)
        if _COT.search(candidate) or len(candidate) > _MAX_SOURCE_CHARS:
            return None
        return candidate


class EpisodeEvidenceAssembler:
    """Code-owned factual evidence owner; no model narrative is accepted."""

    def assemble(self, source: EpisodeEvidenceInput) -> EpisodicMemoryRecord | None:
        if (
            not source.run_id or not source.agent_id or source.memory_scope != "direct"
            or not source.user_request or _TRIVIAL.fullmatch(source.user_request)
        ):
            return None
        observations = tuple(
            EpisodeObservation(
                observation_type="STEP",
                name=_sanitize(step.name),
                status=step.status.value,
                safe_error_code=(
                    _sanitize(step.error_code) if step.error_code else None
                ),
            )
            for step in source.agent_state.steps.values()
            if step.status in {StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.CANCELLED}
        )
        if not observations:
            return None
        goal_text = _sanitize(source.plan_goal or source.user_request)
        goal_authority = (
            EpisodeGoalAuthority.RUNTIME_OBSERVED_PLAN_GOAL
            if source.plan_goal else EpisodeGoalAuthority.USER_PROVIDED
        )
        return EpisodicMemoryRecord(
            memory_id="episode-" + uuid4().hex,
            agent_id=source.agent_id,
            memory_scope=source.memory_scope,
            origin_run_id=source.run_id,
            situation=EpisodeSituation(_sanitize(source.user_request)),
            goal=EpisodeGoal(goal_text, goal_authority),
            observations=observations,
            result=EpisodeResult(
                source.terminal_status.value,
                source.stop_reason.value,
                source.delivery_status,
            ),
            origin=MemoryOrigin(
                origin_type="RUN_FINALIZATION",
                origin_run_id=source.run_id,
                origin_exchange_id=source.run_id,
                origin_agent_id=source.agent_id,
                origin_memory_scope=source.memory_scope,
                formation_method="EPISODIC_RUNTIME_EVIDENCE_V1",
            ),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )


class EpisodicMemoryFormation:
    """Run-finalization observer.  All failures are safe result facts."""

    def __init__(
        self,
        store: AdvancedMemoryStore,
        event_emitter=None,
        *,
        requester: MemoryAccessPrincipal | None = None,
        authorizer: MemoryAccessAuthorizer | None = None,
    ) -> None:
        self._store = store
        self._event_emitter = event_emitter
        self._assembler = EpisodeEvidenceAssembler()
        if requester is not None and not isinstance(requester, MemoryAccessPrincipal):
            raise TypeError("requester 必须是 MemoryAccessPrincipal 或 None")
        if authorizer is not None and not isinstance(authorizer, MemoryAccessAuthorizer):
            raise TypeError("authorizer 必须是 MemoryAccessAuthorizer 或 None")
        self._requester = requester
        self._authorizer = authorizer or MemoryAccessAuthorizer()

    async def run_formation(self, source: EpisodeEvidenceInput) -> EpisodicFormationResult:
        try:
            requester = self._requester or MemoryAccessPrincipal(source.agent_id)
            authorization = self._authorizer.authorize_private_create(
                requester,
                source.agent_id,
                source.memory_scope,
                requested_memory_scope=source.memory_scope,
            )
            if not authorization.allowed:
                result = EpisodicFormationResult(
                    source.run_id,
                    EpisodicFormationOutcome.FAILED,
                    safe_reason=authorization.reason,
                )
                await self._emit(result)
                return result
            record = self._assembler.assemble(source)
            if record is None:
                result = EpisodicFormationResult(source.run_id, EpisodicFormationOutcome.SKIPPED, safe_reason="SKIPPED_INELIGIBLE")
            else:
                persisted = self._store.create_or_get_episode(record)
                outcome = (
                    EpisodicFormationOutcome.REUSED
                    if persisted.memory_id != record.memory_id
                    else EpisodicFormationOutcome.CREATED
                )
                result = EpisodicFormationResult(source.run_id, outcome, persisted.memory_id)
        except (MemoryDomainError, Exception):
            result = EpisodicFormationResult(source.run_id, EpisodicFormationOutcome.FAILED, safe_reason="FORMATION_INTERNAL_ERROR")
        await self._emit(result)
        return result

    async def _emit(self, result: EpisodicFormationResult) -> None:
        if self._event_emitter is None:
            return
        try:
            await self._event_emitter.emit(
                RuntimeEventType.EPISODIC_MEMORY_FORMATION_COMPLETED,
                EpisodicMemoryFormationCompletedPayload(
                    origin_run_id=result.run_id,
                    outcome=result.outcome.value,
                    memory_id=result.memory_id,
                    lesson_status=result.lesson_status.value,
                    safe_reason=result.safe_reason,
                ),
                component="episodic_memory_formation",
                ignore_run_cancellation=True,
            )
        except Exception:
            return


@dataclass(frozen=True, slots=True)
class SpecialistEpisodeEvidenceInput:
    """已提交 delegated Step 的最小、无正文 formation evidence。"""

    run_id: str
    step_id: str
    specialist_agent_id: str
    memory_scope: str
    user_request: str
    step_name: str
    step_status: str


class SpecialistEpisodicMemoryFormation:
    """仅为 verified delegated specialist 创建 PRIVATE STEP episode。"""

    def __init__(self, store: AdvancedMemoryStore, event_emitter=None, *, authorizer: MemoryAccessAuthorizer | None = None) -> None:
        self._store = store
        self._event_emitter = event_emitter
        self._authorizer = authorizer or MemoryAccessAuthorizer()
        self._last_source: SpecialistEpisodeEvidenceInput | None = None

    async def run_formation(self, source: SpecialistEpisodeEvidenceInput) -> EpisodicFormationResult:
        self._last_source = source
        try:
            authorization = self._authorizer.authorize_private_create(
                MemoryAccessPrincipal(source.specialist_agent_id), source.specialist_agent_id,
                source.memory_scope, requested_memory_scope=source.memory_scope,
            )
            if not authorization.allowed:
                return EpisodicFormationResult(source.run_id, EpisodicFormationOutcome.FAILED, safe_reason=authorization.reason)
            if source.step_status not in {StepStatus.SUCCEEDED.value, StepStatus.FAILED.value} or not source.user_request.strip():
                return EpisodicFormationResult(source.run_id, EpisodicFormationOutcome.SKIPPED, safe_reason="SKIPPED_INELIGIBLE")
            record = EpisodicMemoryRecord(
                memory_id="episode-" + uuid4().hex,
                agent_id=source.specialist_agent_id,
                memory_scope=source.memory_scope,
                origin_run_id=source.run_id,
                episode_kind=EpisodeKind.STEP,
                origin_step_id=source.step_id,
                situation=EpisodeSituation(_sanitize(source.user_request)),
                goal=EpisodeGoal(_sanitize(source.step_name), EpisodeGoalAuthority.RUNTIME_OBSERVED_PLAN_GOAL),
                observations=(EpisodeObservation("STEP", _sanitize(source.step_name), source.step_status),),
                result=EpisodeResult(source.step_status, "STEP_TERMINAL", "NOT_APPLICABLE"),
                origin=MemoryOrigin(
                    origin_type="DELEGATED_STEP_TERMINAL", origin_run_id=source.run_id,
                    origin_exchange_id=source.run_id, origin_agent_id=source.specialist_agent_id,
                    origin_memory_scope=source.memory_scope,
                    formation_method="SPECIALIST_EPISODIC_RUNTIME_EVIDENCE_V1",
                ),
                created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
            )
            persisted = self._store.create_or_get_episode(record)
            outcome = EpisodicFormationOutcome.REUSED if persisted.memory_id != record.memory_id else EpisodicFormationOutcome.CREATED
            result = EpisodicFormationResult(source.run_id, outcome, persisted.memory_id)
        except Exception:
            result = EpisodicFormationResult(source.run_id, EpisodicFormationOutcome.FAILED, safe_reason="FORMATION_INTERNAL_ERROR")
        await self._emit(result)
        return result

    async def _emit(self, result: EpisodicFormationResult) -> None:
        if self._event_emitter is None:
            return
        try:
            await self._event_emitter.emit(
                RuntimeEventType.EPISODIC_MEMORY_FORMATION_COMPLETED,
                EpisodicMemoryFormationCompletedPayload(
                    origin_run_id=result.run_id, outcome=result.outcome.value,
                    memory_id=result.memory_id, lesson_status=result.lesson_status.value,
                    safe_reason=result.safe_reason,
                    episode_kind="STEP",
                    origin_step_id=self._last_source.step_id if self._last_source else None,
                    verified_performer_agent_id=(self._last_source.specialist_agent_id if self._last_source else None),
                    owner_match=True,
                ), component="specialist_episodic_memory_formation", ignore_run_cancellation=True,
            )
        except Exception:
            return
