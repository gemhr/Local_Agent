#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""TEST_ONLY isolated WP6 Episodic Layer1 evaluation harness.

This module is the LocalAgent-side evaluation seam owner.  It is never wired
into the normal production Runtime, normal ``/api/chat``, normal events,
ranking, formation, persistence, ContextBuilder trust semantics, Semantic
Memory or Knowledge RAG.  Every capability is explicitly enabled by the
isolated evaluation execution path through a strict typed
``EpisodicEvaluationControl``; nothing is activated by environment variables,
global debug flags or normal user prompts.

Owned capabilities (54 Gate contracts):

- ``DETERMINISTIC_FAILED_RUN``: a fixed, allowlisted ``FaultPlan`` reused from
  the existing test-scope ``FaultPoint`` machinery makes a real Coordinated
  Run fail deterministically; the normal finalization observer still runs and
  forms a truthful FAILED Episode.
- ``REPLAY_EPISODIC_FORMATION_OBSERVER``: reruns the real
  ``EpisodicMemoryFormation.run_formation()`` on the frozen
  ``EpisodeEvidenceInput`` of the same authoritative run -> REUSED.
- ``INSTALL_EPISODIC_FIXTURE``: a strict typed fixture DTO is installed as a
  real ``EpisodicMemoryRecord`` through ``create_or_get_episode()``.
- ``CAPTURE_EPISODIC_PIPELINE``: observation-only projection of the real
  selected/supplied/injected episodic identities into a private artifact.

Privacy boundary: the capture artifact and runtime receipt never contain
canonical text, situation, lesson, goal text, user request, prompt, tool
output, provider response, secrets or raw provenance.  The artifact is a
``PRIVATE_EVALUATION_ARTIFACT`` and must never enter the production journal.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
import threading
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Final, Protocol

from core.advanced_memory import (
    AdvancedMemoryStore,
    EpisodeGoal,
    EpisodeGoalAuthority,
    EpisodeObservation,
    EpisodeResult,
    EpisodeSituation,
    EpisodicMemoryRecord,
    MemoryOrigin,
)
from core.runtime.episodic_memory_formation import (
    EpisodeEvidenceAssembler,
    EpisodeEvidenceInput,
    EpisodicFormationOutcome,
    EpisodicFormationResult,
    EpisodicMemoryFormation,
    LessonStatus,
    _sanitize,
)
from core.runtime.fault_injection import FaultInjectionController
from core.runtime.fault_injection_contract import (
    FaultAction,
    FaultPlan,
    FaultPoint,
    FaultRule,
    FaultScope,
    FaultTrigger,
    InjectedFaultCode,
)
from core.runtime.memory_retrieval import MemoryContextBundle
from core.runtime.agent_registry import DEFAULT_AGENT_REGISTRY
from core.runtime.multi_agent_planning import (
    DelegatedPlanDecision,
    DelegatedTaskDecision,
    PlanningRequest,
    PlanningSource,
    PlanResolver,
)
from core.runtime.plan_compiler import PlanCompiler
from core.runtime.model_context import (
    ContextBuildResult,
    ContextSourceType,
    ContextTrustLevel,
)
from core.runtime.state import StepStatus

CAPTURE_SCHEMA_VERSION: Final[str] = "episodic-evaluation-capture.v1"
EVALUATION_CONTROL_SCHEMA_VERSION: Final[str] = "episodic-evaluation-control.v1"
FIXTURE_ORIGIN_KIND: Final[str] = "DATASET_CONTROLLED_INITIAL_FIXTURE"
_SAFE_RECEIPT_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class EpisodicEvaluationError(ValueError):
    """Typed evaluation-harness failure; never a production Runtime error."""

    def __init__(self, error_code: str, safe_message: str) -> None:
        if not isinstance(error_code, str) or not error_code.strip():
            raise TypeError("error_code must be a non-empty string")
        self.error_code = error_code
        self.safe_message = safe_message
        super().__init__(f"{safe_message} (error_code={error_code})")

    def __repr__(self) -> str:
        return f"EpisodicEvaluationError(error_code={self.error_code!r})"


class EpisodicEvaluationCapability(str, Enum):
    """Strict allowlisted capability vocabulary for WP6 Layer1 evaluation."""

    NONE = "NONE"
    DETERMINISTIC_FAILED_RUN = "DETERMINISTIC_FAILED_RUN"
    REPLAY_EPISODIC_FORMATION_OBSERVER = (
        "REPLAY_EPISODIC_FORMATION_OBSERVER"
    )
    INSTALL_EPISODIC_FIXTURE = "INSTALL_EPISODIC_FIXTURE"
    CAPTURE_EPISODIC_PIPELINE = "CAPTURE_EPISODIC_PIPELINE"
    DETERMINISTIC_EPISODIC_SUCCESS_RUN = "DETERMINISTIC_EPISODIC_SUCCESS_RUN"


_C = EpisodicEvaluationCapability

#: Explicit legal compositions.  ``frozenset()`` means NONE.  Any other
#: combination is rejected fail-closed; there are no arbitrary flags.
_LEGAL_CAPABILITY_COMPOSITIONS: Final[frozenset[frozenset[EpisodicEvaluationCapability]]] = frozenset(
    {
        frozenset(),
        frozenset({_C.DETERMINISTIC_FAILED_RUN}),
        frozenset({_C.CAPTURE_EPISODIC_PIPELINE}),
        frozenset({_C.DETERMINISTIC_EPISODIC_SUCCESS_RUN}),
        frozenset({_C.DETERMINISTIC_EPISODIC_SUCCESS_RUN, _C.CAPTURE_EPISODIC_PIPELINE}),
        frozenset(
            {
                _C.DETERMINISTIC_FAILED_RUN,
                _C.CAPTURE_EPISODIC_PIPELINE,
            }
        ),
        frozenset({_C.REPLAY_EPISODIC_FORMATION_OBSERVER}),
        frozenset({_C.INSTALL_EPISODIC_FIXTURE}),
        frozenset({_C.INSTALL_EPISODIC_FIXTURE, _C.CAPTURE_EPISODIC_PIPELINE}),
        frozenset(
            {
                _C.INSTALL_EPISODIC_FIXTURE,
                _C.DETERMINISTIC_FAILED_RUN,
                _C.CAPTURE_EPISODIC_PIPELINE,
            }
        ),
    }
)

#: Allowed fixture memory scopes are the existing exact scope vocabulary only.
#: ``orchestration`` is legal persistence vocabulary for the E09 foreign
#: fixture; it never extends production Episodic Formation (which stays
#: ``direct`` only).
_ALLOWED_FIXTURE_SCOPES: Final[frozenset[str]] = frozenset(
    {"direct", "orchestration"}
)
_MAX_OBSERVATIONS: Final[int] = 8
TARGET_EVALUATION_SEMANTIC_SOURCE_FILES: Final[tuple[str, ...]] = (
    "core/advanced_memory.py", "core/runtime/fault_injection_contract.py",
    "core/runtime/fault_injection.py", "core/runtime/multi_agent_driver.py",
    "core/runtime/multi_agent_planning.py", "core/runtime/plan_compiler.py",
    "core/runtime/episodic_evaluation.py", "core/runtime/run_coordinator.py",
    "core/runtime/runtime_factory.py", "core/chat_service.py", "core/agent_router.py",
    "core/runtime/episodic_memory_formation.py", "core/runtime/memory_retrieval.py",
    "core/runtime/model_context.py", "core/runtime/memory_authorization.py",
    "core/runtime/project_memory.py", "server.py", "tests/test_episodic_evaluation_harness.py",
    "tests/test_wp7_evaluation_v4_endpoint.py",
    "core/settings.py", "core/llm_engine.py",
)


def target_evaluation_implementation_ref() -> str:
    """Stable bytes digest for isolated target-side Layer1 semantics."""
    root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    for relative in TARGET_EVALUATION_SEMANTIC_SOURCE_FILES:
        path = root / relative
        digest.update(relative.encode("utf-8")); digest.update(b"\0")
        digest.update(path.read_bytes()); digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _require_non_empty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EpisodicEvaluationError(
            "EPISODIC_EVALUATION_INVALID_ARGUMENT",
            f"{name} must be a non-empty string",
        )
    return value


def _require_bounded(value: str, name: str, limit: int) -> None:
    if len(value) > limit:
        raise EpisodicEvaluationError(
            "EPISODIC_EVALUATION_INVALID_ARGUMENT",
            f"{name} exceeds the bounded length limit",
        )


# ---------------------------------------------------------------------------
# Strict typed evaluation control
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EpisodicFixtureObservation:
    observation_type: str
    name: str
    status: str
    safe_error_code: str | None = None
    outcome_classification: str | None = None
    result_digest: str | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.observation_type, "observation_type"),
            (self.name, "name"),
            (self.status, "status"),
        ):
            _require_non_empty(value, f"fixture.observation.{name}")
        for value, name in (
            (self.safe_error_code, "safe_error_code"),
            (self.outcome_classification, "outcome_classification"),
            (self.result_digest, "result_digest"),
        ):
            if value is not None:
                _require_non_empty(value, f"fixture.observation.{name}")


@dataclass(frozen=True, slots=True)
class EpisodicFixtureResult:
    terminal_status: str
    stop_reason: str
    delivery_status: str

    def __post_init__(self) -> None:
        _require_non_empty(self.terminal_status, "fixture.result.terminal_status")
        _require_non_empty(self.stop_reason, "fixture.result.stop_reason")
        _require_non_empty(self.delivery_status, "fixture.result.delivery_status")


@dataclass(frozen=True, slots=True)
class EpisodicFixtureSpec:
    """Strict typed fixture DTO; caller-provided canonical_text is impossible.

    ``canonical_text`` is always produced by the LocalAgent renderer inside
    ``EpisodicMemoryRecord``; this DTO carries no content-free override field.
    """

    fixture_ref: str
    agent_id: str
    memory_scope: str
    origin_run_id: str
    situation: str
    goal: str
    observations: tuple[EpisodicFixtureObservation, ...]
    result: EpisodicFixtureResult
    lesson: str | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.fixture_ref, "fixture_ref"),
            (self.agent_id, "agent_id"),
            (self.memory_scope, "memory_scope"),
            (self.origin_run_id, "origin_run_id"),
        ):
            _require_non_empty(value, f"fixture.{name}")
        if _SAFE_RECEIPT_TOKEN.fullmatch(self.fixture_ref) is None:
            raise EpisodicEvaluationError(
                "EPISODIC_EVALUATION_INVALID_ARGUMENT",
                "fixture_ref must be a safe symbolic token",
            )
        if self.memory_scope not in _ALLOWED_FIXTURE_SCOPES:
            raise EpisodicEvaluationError(
                "EPISODIC_EVALUATION_INVALID_ARGUMENT",
                "fixture memory_scope is not an allowed exact scope",
            )
        for value, name in (
            (self.situation, "situation"),
            (self.goal, "goal"),
        ):
            _require_non_empty(value, f"fixture.{name}")
            _require_bounded(value, f"fixture.{name}", 400)
        if not isinstance(self.observations, tuple):
            object.__setattr__(self, "observations", tuple(self.observations))
        if (
            not self.observations
            or len(self.observations) > _MAX_OBSERVATIONS
            or any(
                not isinstance(item, EpisodicFixtureObservation)
                for item in self.observations
            )
        ):
            raise EpisodicEvaluationError(
                "EPISODIC_EVALUATION_INVALID_ARGUMENT",
                "fixture.observations must be a bounded typed tuple",
            )
        if not isinstance(self.result, EpisodicFixtureResult):
            raise EpisodicEvaluationError(
                "EPISODIC_EVALUATION_INVALID_ARGUMENT",
                "fixture.result must be a typed EpisodicFixtureResult",
            )
        if self.lesson is not None:
            _require_non_empty(self.lesson, "fixture.lesson")
            _require_bounded(self.lesson, "fixture.lesson", 400)


@dataclass(frozen=True, slots=True)
class EpisodicEvaluationControl:
    """Strict typed control with explicit legal capability compositions."""

    capabilities: frozenset[EpisodicEvaluationCapability] = frozenset()
    fixture: EpisodicFixtureSpec | None = None
    replay_run_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.capabilities, frozenset):
            object.__setattr__(self, "capabilities", frozenset(self.capabilities))
        if any(
            not isinstance(capability, EpisodicEvaluationCapability)
            for capability in self.capabilities
        ):
            raise EpisodicEvaluationError(
                "EPISODIC_EVALUATION_INVALID_ARGUMENT",
                "capabilities must be EpisodicEvaluationCapability values",
            )
        if self.capabilities not in _LEGAL_CAPABILITY_COMPOSITIONS:
            raise EpisodicEvaluationError(
                "EPISODIC_EVALUATION_ILLEGAL_COMPOSITION",
                "capability composition is not explicitly allowlisted",
            )
        has_fixture = _C.INSTALL_EPISODIC_FIXTURE in self.capabilities
        if has_fixture and not isinstance(self.fixture, EpisodicFixtureSpec):
            raise EpisodicEvaluationError(
                "EPISODIC_EVALUATION_FIXTURE_REQUIRED",
                "INSTALL_EPISODIC_FIXTURE requires a strict typed fixture",
            )
        if not has_fixture and self.fixture is not None:
            raise EpisodicEvaluationError(
                "EPISODIC_EVALUATION_INVALID_ARGUMENT",
                "fixture is only allowed with INSTALL_EPISODIC_FIXTURE",
            )
        has_replay = _C.REPLAY_EPISODIC_FORMATION_OBSERVER in self.capabilities
        if has_replay:
            if not isinstance(self.replay_run_id, str) or not self.replay_run_id:
                raise EpisodicEvaluationError(
                    "EPISODIC_EVALUATION_REPLAY_RUN_ID_REQUIRED",
                    "REPLAY requires the authoritative replay_run_id",
                )
            try:
                uuid.UUID(self.replay_run_id)
            except ValueError:
                raise EpisodicEvaluationError(
                    "EPISODIC_EVALUATION_INVALID_ARGUMENT",
                    "replay_run_id must be a valid UUID",
                ) from None
        elif self.replay_run_id is not None:
            raise EpisodicEvaluationError(
                "EPISODIC_EVALUATION_INVALID_ARGUMENT",
                "replay_run_id is only allowed with REPLAY capability",
            )

    @property
    def is_none(self) -> bool:
        return not self.capabilities

    @classmethod
    def none(cls) -> "EpisodicEvaluationControl":
        return cls()


# ---------------------------------------------------------------------------
# Deterministic successful execution profile (E08 only)
# ---------------------------------------------------------------------------

DETERMINISTIC_EPISODIC_SUCCESS_PROFILE = "DETERMINISTIC_EPISODIC_SUCCESS_V1"
_E08_PLAN_GOAL = "整理项目生产环境的发布清单并记录部署方式与回滚步骤"


class DeterministicEpisodicSuccessResolver(PlanResolver):
    """Target-owned E08 plan profile; it accepts no caller plan or prompt."""

    def __init__(self) -> None:
        super().__init__(DEFAULT_AGENT_REGISTRY, PlanCompiler(DEFAULT_AGENT_REGISTRY))

    async def resolve(self, request: PlanningRequest, run_context, **_kwargs):
        if request.selected_agent_id != "core_router":
            raise EpisodicEvaluationError("EPISODIC_EVALUATION_INVALID_ARGUMENT", "profile requires core_router entry")
        decision = DelegatedPlanDecision(
            (
                DelegatedTaskDecision("release_list", "code_expert", "Prepare release checklist."),
                DelegatedTaskDecision("rollback_plan", "data_analyst", "Prepare rollback plan."),
            ),
            synthesis_required=True,
        )
        resolved = self._compiler.compile(decision, planning_source=PlanningSource.DETERMINISTIC_RULE)
        # The profile owns this bounded renderer input; Dataset callers cannot override it.
        from dataclasses import replace
        from core.runtime.multi_agent_planning import ResolvedPlan
        plan = replace(resolved.plan, task_summary=_E08_PLAN_GOAL)
        return ResolvedPlan(plan, resolved.invocation_bindings, resolved.planning_source)


def deterministic_episodic_success_resolver() -> DeterministicEpisodicSuccessResolver:
    return DeterministicEpisodicSuccessResolver()


# ---------------------------------------------------------------------------
# Deterministic FAILED Run mechanism
# ---------------------------------------------------------------------------

_DFR_RULE_ID: Final[str] = "episodic-eval-deterministic-failed-run"


def deterministic_failed_run_plan(plan_id: str = "episodic-eval-failed-run-v1") -> FaultPlan:
    """Fixed, allowlisted FaultPlan reusing the existing test-scope seam.

    The seam fires at ``STEP_BEFORE_DRIVER_EXECUTE`` inside the real
    MultiAgentDriver so a genuinely claimed/started Step fails deterministically
    before any provider call.  The Run then reaches a real FAILED terminal
    through ``RunCoordinator._finalize_once()`` and the normal Episodic
    Formation observer still runs.
    """
    rule = FaultRule(
        rule_id=_DFR_RULE_ID,
        fault_point=FaultPoint.STEP_BEFORE_DRIVER_EXECUTE,
        action=FaultAction.RAISE_TYPED_ERROR,
        trigger=FaultTrigger.ALWAYS,
        scope=FaultScope.RUN_SCOPE,
        max_hits=1,
        component="multi_agent_driver",
        operation_kind="DRIVER_EXECUTE",
        safe_fault_code=InjectedFaultCode.INJECTED_PERMANENT_FAILURE,
        dangerous_window=False,
    )
    return FaultPlan(plan_id, (rule,), created_at=datetime.now(UTC))


def deterministic_failed_run_controller() -> FaultInjectionController:
    return FaultInjectionController(
        deterministic_failed_run_plan(),
        enabled=True,
    )


# ---------------------------------------------------------------------------
# Fixture installer (scenario pre-run phase only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EpisodicFixtureReceipt:
    fixture_ref: str
    memory_id: str
    origin_run_id: str
    origin_kind: str
    memory_scope: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.fixture_ref, "fixture_ref"),
            (self.memory_id, "memory_id"),
            (self.origin_run_id, "origin_run_id"),
            (self.origin_kind, "origin_kind"),
            (self.memory_scope, "memory_scope"),
        ):
            _require_non_empty(value, f"receipt.{name}")
        if self.origin_kind != FIXTURE_ORIGIN_KIND:
            raise EpisodicEvaluationError(
                "EPISODIC_EVALUATION_INVALID_ARGUMENT",
                "fixture receipt origin_kind must be DATASET_CONTROLLED_INITIAL_FIXTURE",
            )

    def to_wire_dict(self) -> dict[str, str]:
        return {
            "fixture_ref": self.fixture_ref,
            "memory_id": self.memory_id,
            "origin_run_id": self.origin_run_id,
            "origin_kind": self.origin_kind,
            "memory_scope": self.memory_scope,
        }


class EpisodicFixtureInstaller:
    """Installs a strict typed fixture through the Store API only.

    Never uses raw SQL, arbitrary DB mutation, or caller-provided
    ``canonical_text``.  The receipt is evaluation provenance only; it is not
    a ``RUN_FORMED`` Episode and must never be treated as selection evidence.
    """

    def __init__(self, store: AdvancedMemoryStore) -> None:
        if not isinstance(store, AdvancedMemoryStore):
            raise TypeError("EpisodicFixtureInstaller requires AdvancedMemoryStore")
        self._store = store

    def install(self, spec: EpisodicFixtureSpec) -> EpisodicFixtureReceipt:
        if not isinstance(spec, EpisodicFixtureSpec):
            raise EpisodicEvaluationError(
                "EPISODIC_EVALUATION_INVALID_ARGUMENT",
                "installer only accepts EpisodicFixtureSpec",
            )
        record = EpisodicMemoryRecord(
            memory_id="episode-" + uuid.uuid4().hex,
            agent_id=spec.agent_id,
            memory_scope=spec.memory_scope,
            origin_run_id=spec.origin_run_id,
            situation=EpisodeSituation(spec.situation),
            goal=EpisodeGoal(spec.goal, EpisodeGoalAuthority.USER_PROVIDED),
            observations=tuple(
                EpisodeObservation(
                    observation_type=item.observation_type,
                    name=item.name,
                    status=item.status,
                    safe_error_code=item.safe_error_code,
                    outcome_classification=item.outcome_classification,
                    result_digest=item.result_digest,
                )
                for item in spec.observations
            ),
            result=EpisodeResult(
                spec.result.terminal_status,
                spec.result.stop_reason,
                spec.result.delivery_status,
            ),
            lesson=spec.lesson,
            origin=MemoryOrigin(
                origin_type=FIXTURE_ORIGIN_KIND,
                origin_run_id=spec.origin_run_id,
                origin_exchange_id=spec.origin_run_id,
                origin_agent_id=spec.agent_id,
                origin_memory_scope=spec.memory_scope,
                formation_method="EPISODIC_FIXTURE_V1",
            ),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        persisted = self._store.create_or_get_episode(record)
        return EpisodicFixtureReceipt(
            fixture_ref=spec.fixture_ref,
            memory_id=persisted.memory_id,
            origin_run_id=spec.origin_run_id,
            origin_kind=FIXTURE_ORIGIN_KIND,
            memory_scope=spec.memory_scope,
        )


# ---------------------------------------------------------------------------
# Layer1 capture (observation only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EpisodicSelectionItem:
    memory_id: str
    rank: int
    lexical_match_score: int
    selected: bool
    drop_reason: str | None = None


@dataclass(frozen=True, slots=True)
class EpisodicSelectionEvidence:
    candidate_count: int
    selected: tuple[EpisodicSelectionItem, ...]


@dataclass(frozen=True, slots=True)
class EpisodicSuppliedEvidence:
    episodic_memory_ids: tuple[str, ...]
    record_count: int


@dataclass(frozen=True, slots=True)
class EpisodicInjectedEvidence:
    target: str
    episodic_memory_ids: tuple[str, ...]
    context_record_count: int
    source_type: str
    trust_level: str


@dataclass(frozen=True, slots=True)
class EpisodicCaptureArtifact:
    """PRIVATE_EVALUATION_ARTIFACT; never enters the production journal."""

    schema_version: str
    run_id: str
    capture_outcome: str
    selection: EpisodicSelectionEvidence | None
    supplied: EpisodicSuppliedEvidence | None
    injected: tuple[EpisodicInjectedEvidence, ...]

    def __post_init__(self) -> None:
        if self.schema_version != CAPTURE_SCHEMA_VERSION:
            raise EpisodicEvaluationError(
                "EPISODIC_EVALUATION_INVALID_ARGUMENT",
                "unsupported capture schema_version",
            )
        _require_non_empty(self.run_id, "capture.run_id")
        if self.capture_outcome not in {"COMPLETE", "PARTIAL", "FAILED"}:
            raise EpisodicEvaluationError(
                "EPISODIC_EVALUATION_INVALID_ARGUMENT",
                "unknown capture_outcome",
            )

    def to_wire_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "capture_outcome": self.capture_outcome,
            "selection": (
                {
                    "candidate_count": self.selection.candidate_count,
                    "selected": [
                        {
                            "memory_id": item.memory_id,
                            "rank": item.rank,
                            "lexical_match_score": item.lexical_match_score,
                            "selected": item.selected,
                            "drop_reason": item.drop_reason,
                        }
                        for item in self.selection.selected
                    ],
                }
                if self.selection is not None
                else None
            ),
            "supplied": (
                {
                    "episodic_memory_ids": list(self.supplied.episodic_memory_ids),
                    "record_count": self.supplied.record_count,
                }
                if self.supplied is not None
                else None
            ),
            "injected": [
                {
                    "target": item.target,
                    "episodic_memory_ids": list(item.episodic_memory_ids),
                    "context_record_count": item.context_record_count,
                    "source_type": item.source_type,
                    "trust_level": item.trust_level,
                }
                for item in self.injected
            ],
        }


class EpisodicCaptureCollector:
    """Run-scoped observation-only collector (never mutates the Runtime).

    Authorities: selected = ``MemoryContextBundle.episodic_evidence``,
    supplied = ``MemoryContextBundle.episodic_records``, injected =
    ``ContextBuilder.build(...).included_items`` of episodic source type.
    It never copies canonical_text, situation, lesson, goal, user request,
    prompt, tool output, provider response, secret or raw provenance.
    """

    def __init__(self, run_id: str) -> None:
        try:
            uuid.UUID(run_id)
        except ValueError:
            raise EpisodicEvaluationError(
                "EPISODIC_EVALUATION_INVALID_ARGUMENT",
                "capture run_id must be a valid UUID",
            ) from None
        self.run_id = run_id
        self._lock = threading.Lock()
        self._selection: EpisodicSelectionEvidence | None = None
        self._supplied: EpisodicSuppliedEvidence | None = None
        self._injected: list[EpisodicInjectedEvidence] = []
        self._error_code: str | None = None

    def _record_failure_locked(self, code: str) -> None:
        if self._error_code is None:
            self._error_code = code

    def record_failure(self, code: str) -> None:
        with self._lock:
            self._record_failure_locked(code)

    def observe_retrieval(self, *, run_id: str, bundle: MemoryContextBundle) -> None:
        """Observe the immutable bundle once after ``MemoryRetrievalService`` returns."""
        with self._lock:
            if run_id != self.run_id:
                self._record_failure_locked("EPISODIC_EVALUATION_RUN_ID_MISMATCH")
                return
            if self._selection is not None or self._supplied is not None:
                return
            self._selection = EpisodicSelectionEvidence(
                candidate_count=bundle.episodic_candidate_count,
                selected=tuple(
                    EpisodicSelectionItem(
                        memory_id=item.memory_id,
                        rank=item.rank,
                        lexical_match_score=item.lexical_match_score,
                        selected=item.selected,
                        drop_reason=item.drop_reason,
                    )
                    for item in bundle.episodic_evidence
                ),
            )
            self._supplied = EpisodicSuppliedEvidence(
                episodic_memory_ids=tuple(
                    record.provenance.memory_id
                    for record in bundle.episodic_records
                ),
                record_count=len(bundle.episodic_records),
            )

    def observe_injection(
        self, *, target: str, context_result: ContextBuildResult
    ) -> None:
        """Observe only accepted episodic items from the real build result."""
        with self._lock:
            if self.run_id is None:
                return
            episodic_items = tuple(
                item
                for item in context_result.included_items
                if item.source_type is ContextSourceType.EPISODIC_MEMORY_RETRIEVAL
            )
            if not episodic_items:
                return
            for item in episodic_items:
                if item.trust_level is not ContextTrustLevel.USER_CONTENT:
                    self._record_failure_locked(
                        "EPISODIC_EVALUATION_TRUST_VIOLATION"
                    )
            self._injected.append(
                EpisodicInjectedEvidence(
                    target=target,
                    episodic_memory_ids=tuple(
                        item.item_id for item in episodic_items
                    ),
                    context_record_count=len(episodic_items),
                    source_type=ContextSourceType.EPISODIC_MEMORY_RETRIEVAL.value,
                    trust_level=ContextTrustLevel.USER_CONTENT.value,
                )
            )

    def envelope(self) -> EpisodicCaptureArtifact | None:
        with self._lock:
            selection = self._selection
            supplied = self._supplied
            injected = tuple(self._injected)
            error_code = self._error_code
        if selection is None and supplied is None and not injected:
            return None
        if error_code is None:
            outcome = "COMPLETE"
        elif selection is not None or injected:
            outcome = "PARTIAL"
        else:
            outcome = "FAILED"
        return EpisodicCaptureArtifact(
            schema_version=CAPTURE_SCHEMA_VERSION,
            run_id=self.run_id,
            capture_outcome=outcome,
            selection=selection,
            supplied=supplied,
            injected=injected,
        )


_CURRENT_CAPTURE_COLLECTOR: ContextVar[EpisodicCaptureCollector | None] = (
    ContextVar("episodic_evaluation_capture_collector", default=None)
)


def current_episodic_capture_collector() -> EpisodicCaptureCollector | None:
    return _CURRENT_CAPTURE_COLLECTOR.get()


def install_episodic_capture_collector(
    collector: EpisodicCaptureCollector,
) -> Token[EpisodicCaptureCollector | None]:
    if not isinstance(collector, EpisodicCaptureCollector):
        raise TypeError("collector must be EpisodicCaptureCollector")
    return _CURRENT_CAPTURE_COLLECTOR.set(collector)


def reset_episodic_capture_collector(
    token: Token[EpisodicCaptureCollector | None],
) -> None:
    _CURRENT_CAPTURE_COLLECTOR.reset(token)


def observe_episodic_retrieval(
    *, run_id: str, bundle: MemoryContextBundle
) -> None:
    """No-op unless an isolated evaluation collector is installed."""
    collector = current_episodic_capture_collector()
    if collector is None:
        return
    collector.observe_retrieval(run_id=run_id, bundle=bundle)


def observe_episodic_injection(
    *, target: str, context_result: ContextBuildResult
) -> None:
    """No-op unless an isolated evaluation collector is installed."""
    collector = current_episodic_capture_collector()
    if collector is None:
        return
    collector.observe_injection(target=target, context_result=context_result)


# ---------------------------------------------------------------------------
# Formation replay + deterministic runtime receipt
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EpisodicFormationReceipt:
    """Safe formation outcome projection (never carries episode body)."""

    run_id: str
    outcome: str
    memory_id: str | None = None
    lesson_status: str = LessonStatus.ABSENT.value
    safe_reason: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty(self.run_id, "receipt.run_id")
        if self.outcome not in {item.value for item in EpisodicFormationOutcome}:
            raise EpisodicEvaluationError(
                "EPISODIC_EVALUATION_INVALID_ARGUMENT",
                "unknown formation outcome",
            )
        if self.memory_id is not None:
            _require_non_empty(self.memory_id, "receipt.memory_id")
        if self.safe_reason is not None:
            _require_non_empty(self.safe_reason, "receipt.safe_reason")

    def to_wire_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "outcome": self.outcome,
            "memory_id": self.memory_id,
            "lesson_status": self.lesson_status,
            "safe_reason": self.safe_reason,
        }

    @classmethod
    def from_result(
        cls, result: EpisodicFormationResult
    ) -> "EpisodicFormationReceipt":
        return cls(
            run_id=result.run_id,
            outcome=result.outcome.value,
            memory_id=result.memory_id,
            lesson_status=result.lesson_status.value,
            safe_reason=result.safe_reason,
        )


@dataclass(frozen=True, slots=True)
class EpisodicRuntimeReceipt:
    """Deterministic runtime evidence for the dataset Gate (no body content)."""

    run_id: str
    plan_goal: str | None
    step_names: tuple[str, ...]
    step_statuses: tuple[str, ...]
    terminal_status: str
    stop_reason: str
    delivery_status: str
    formed_memory_id: str | None
    formation_outcome: str | None
    canonical_text_sha256: str | None

    def __post_init__(self) -> None:
        _require_non_empty(self.run_id, "receipt.run_id")
        for value, name in (
            (self.plan_goal, "plan_goal"),
            (self.terminal_status, "terminal_status"),
            (self.stop_reason, "stop_reason"),
            (self.delivery_status, "delivery_status"),
            (self.formed_memory_id, "formed_memory_id"),
            (self.formation_outcome, "formation_outcome"),
        ):
            if value is not None:
                _require_non_empty(value, f"receipt.{name}")
        if self.canonical_text_sha256 is not None and (
            _SHA256.fullmatch(self.canonical_text_sha256) is None
        ):
            raise EpisodicEvaluationError(
                "EPISODIC_EVALUATION_INVALID_ARGUMENT",
                "canonical_text_sha256 must be a lowercase SHA-256 digest",
            )

    def to_wire_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "plan_goal": self.plan_goal,
            "step_names": list(self.step_names),
            "step_statuses": list(self.step_statuses),
            "terminal_status": self.terminal_status,
            "stop_reason": self.stop_reason,
            "delivery_status": self.delivery_status,
            "formed_memory_id": self.formed_memory_id,
            "formation_outcome": self.formation_outcome,
            "canonical_text_sha256": self.canonical_text_sha256,
        }


class EpisodicEvaluationObserver(Protocol):
    """RunCoordinator-side observer hooks used only when explicitly injected."""

    def on_evidence(self, source: EpisodeEvidenceInput) -> None:
        """Receives the frozen typed evidence input before formation."""

    def on_formation(self, result: EpisodicFormationResult) -> None:
        """Receives the real formation result after the observer ran."""


class EpisodicEvidenceRetainer:
    """Run-scoped, bounded, cleanup-able frozen evidence retention.

    Only exists when the isolated evaluation path explicitly enables replay.
    Normal production finalization never retains ``EpisodeEvidenceInput``.
    """

    def __init__(self) -> None:
        self._source: EpisodeEvidenceInput | None = None
        self._assembled: EpisodicMemoryRecord | None = None
        self._formation_result: EpisodicFormationResult | None = None
        self._assembler = EpisodeEvidenceAssembler()

    def on_evidence(self, source: EpisodeEvidenceInput) -> None:
        self._source = source
        try:
            self._assembled = self._assembler.assemble(source)
        except Exception:
            self._assembled = None

    def on_formation(self, result: EpisodicFormationResult) -> None:
        self._formation_result = result

    @property
    def retained_source(self) -> EpisodeEvidenceInput | None:
        return self._source

    @property
    def formation_result(self) -> EpisodicFormationResult | None:
        return self._formation_result

    def runtime_receipt(self) -> EpisodicRuntimeReceipt | None:
        source = self._source
        if source is None:
            return None
        assembled = self._assembled
        formation = self._formation_result
        if assembled is not None:
            plan_goal = assembled.goal.text
            step_names = tuple(item.name for item in assembled.observations)
            step_statuses = tuple(item.status for item in assembled.observations)
            terminal_status = assembled.result.terminal_status
            stop_reason = assembled.result.stop_reason
            delivery_status = assembled.result.delivery_status
            canonical_digest = hashlib.sha256(
                assembled.canonical_text.encode("utf-8")
            ).hexdigest()
        else:
            plan_goal = _sanitize(source.plan_goal or source.user_request or "")
            step_names = tuple(
                _sanitize(step.name)
                for step in source.agent_state.steps.values()
                if step.status
                in {StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.CANCELLED}
            )
            step_statuses = tuple(
                step.status.value
                for step in source.agent_state.steps.values()
                if step.status
                in {StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.CANCELLED}
            )
            terminal_status = source.terminal_status.value
            stop_reason = source.stop_reason.value
            delivery_status = source.delivery_status
            canonical_digest = None
        return EpisodicRuntimeReceipt(
            run_id=source.run_id,
            plan_goal=plan_goal,
            step_names=step_names,
            step_statuses=step_statuses,
            terminal_status=terminal_status,
            stop_reason=stop_reason,
            delivery_status=delivery_status,
            formed_memory_id=formation.memory_id if formation else None,
            formation_outcome=formation.outcome.value if formation else None,
            canonical_text_sha256=canonical_digest,
        )

    def first_formation_receipt(self) -> EpisodicFormationReceipt | None:
        if self._formation_result is None:
            return None
        return EpisodicFormationReceipt.from_result(self._formation_result)

    def clear(self) -> None:
        self._source = None
        self._assembled = None
        self._formation_result = None


class EpisodicReplayRunner:
    """Replays the real formation observer for the same authoritative run.

    Never creates a second Run, never writes the Store directly, never changes
    the terminal, and never re-executes model/tool.  Idempotency is owned by
    ``AdvancedMemoryStore.create_or_get_episode`` origin uniqueness, so the
    second outcome is REUSED.
    """

    def __init__(self, store: AdvancedMemoryStore) -> None:
        if not isinstance(store, AdvancedMemoryStore):
            raise TypeError("EpisodicReplayRunner requires AdvancedMemoryStore")
        self._store = store

    async def replay(
        self, *, run_id: str, retainer: EpisodicEvidenceRetainer
    ) -> EpisodicFormationReceipt:
        if not isinstance(retainer, EpisodicEvidenceRetainer):
            raise EpisodicEvaluationError(
                "EPISODIC_EVALUATION_INVALID_ARGUMENT",
                "replay requires an EpisodicEvidenceRetainer",
            )
        source = retainer.retained_source
        if source is None or source.run_id != run_id:
            raise EpisodicEvaluationError(
                "EPISODIC_EVALUATION_REPLAY_UNKNOWN_RUN_ID",
                "no frozen evidence input is retained for the requested run_id",
            )
        result = await EpisodicMemoryFormation(
            self._store, event_emitter=None
        ).run_formation(source)
        return EpisodicFormationReceipt.from_result(result)


__all__ = [
    "CAPTURE_SCHEMA_VERSION",
    "EVALUATION_CONTROL_SCHEMA_VERSION",
    "FIXTURE_ORIGIN_KIND",
    "EpisodicCaptureArtifact",
    "EpisodicCaptureCollector",
    "EpisodicEvaluationCapability",
    "EpisodicEvaluationControl",
    "EpisodicEvaluationError",
    "EpisodicEvaluationObserver",
    "EpisodicEvidenceRetainer",
    "EpisodicFixtureInstaller",
    "EpisodicFixtureObservation",
    "EpisodicFixtureReceipt",
    "EpisodicFixtureResult",
    "EpisodicFixtureSpec",
    "EpisodicFormationReceipt",
    "EpisodicInjectedEvidence",
    "EpisodicReplayRunner",
    "EpisodicRuntimeReceipt",
    "EpisodicSelectionEvidence",
    "EpisodicSelectionItem",
    "EpisodicSuppliedEvidence",
    "current_episodic_capture_collector",
    "deterministic_failed_run_controller",
    "deterministic_failed_run_plan",
    "install_episodic_capture_collector",
    "observe_episodic_injection",
    "observe_episodic_retrieval",
    "reset_episodic_capture_collector",
]
