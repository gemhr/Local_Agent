#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Runtime 内部使用的强类型事件信封与安全 Payload。"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import ClassVar, TypeAlias
from uuid import uuid4


RUNTIME_EVENT_SCHEMA_VERSION = 2
TOOL_EVIDENCE_SCHEMA_VERSION = 1


class RuntimeEventType(str, Enum):
    """第一版 Runtime Event 的固定类型。"""

    RUN_STARTED = "RUN_STARTED"
    PLANNING_STARTED = "PLANNING_STARTED"
    PLAN_CREATED = "PLAN_CREATED"
    STEP_STARTED = "STEP_STARTED"
    MODEL_STARTED = "MODEL_STARTED"
    MODEL_COMPLETED = "MODEL_COMPLETED"
    TOOL_STARTED = "TOOL_STARTED"
    TOOL_COMPLETED = "TOOL_COMPLETED"
    RETRIEVAL_STARTED = "RETRIEVAL_STARTED"
    RETRIEVAL_STAGE_COMPLETED = "RETRIEVAL_STAGE_COMPLETED"
    RETRIEVAL_COMPLETED = "RETRIEVAL_COMPLETED"
    OUTPUT_DELTA = "OUTPUT_DELTA"
    STEP_COMPLETED = "STEP_COMPLETED"
    MEMORY_FORMATION_COMPLETED = "MEMORY_FORMATION_COMPLETED"
    EPISODIC_MEMORY_FORMATION_COMPLETED = "EPISODIC_MEMORY_FORMATION_COMPLETED"
    MEMORY_LIFECYCLE_RESOLVED = "MEMORY_LIFECYCLE_RESOLVED"
    MEMORY_RETRIEVAL_COMPLETED = "MEMORY_RETRIEVAL_COMPLETED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    TIMEOUT = "TIMEOUT"
    ERROR = "ERROR"
    CANCELLATION = "CANCELLATION"
    RUN_COMPLETED = "RUN_COMPLETED"


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 必须是非空字符串")


def _require_index(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} 必须是非负整数")


@dataclass(frozen=True, slots=True)
class RunStartedPayload:
    status: str

    def __post_init__(self) -> None:
        _require_text(self.status, "status")


@dataclass(frozen=True, slots=True)
class PlanningStartedPayload:
    planner_schema_version: int
    configured_timeout_ms: int

    def __post_init__(self) -> None:
        _require_index(self.planner_schema_version, "planner_schema_version")
        _require_index(self.configured_timeout_ms, "configured_timeout_ms")


@dataclass(frozen=True, slots=True)
class PlanCreatedPayload:
    plan_id: str
    plan_version: int
    fingerprint: str
    step_count: int
    planning_source: str
    # WP5: safe execution shape (0/1/2/3). Legacy records may omit it.
    shape: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.plan_id, "plan_id")
        _require_index(self.plan_version, "plan_version")
        _require_text(self.fingerprint, "fingerprint")
        _require_index(self.step_count, "step_count")
        _require_text(self.planning_source, "planning_source")
        if self.shape is not None:
            _require_text(self.shape, "shape")
            if self.shape not in {"0", "1", "2", "3"}:
                raise ValueError("shape 必须是 0/1/2/3")


@dataclass(frozen=True, slots=True)
class StepStartedPayload:
    status: str
    # WP5 safe step facts. Legacy records may omit them; new emissions always
    # set the full set from the frozen Plan + Registry claim.
    agent_id: str | None = None
    execution_kind: str | None = None
    output_policy: str | None = None
    dependency_count: int | None = None

    def __post_init__(self) -> None:
        _require_text(self.status, "status")
        for value, name in (
            (self.agent_id, "agent_id"),
            (self.execution_kind, "execution_kind"),
            (self.output_policy, "output_policy"),
        ):
            if value is not None:
                _require_text(value, name)
        if self.dependency_count is not None:
            _require_index(self.dependency_count, "dependency_count")


@dataclass(frozen=True, slots=True)
class ModelStartedPayload:
    profile_id: str
    candidate_index: int
    retry_index: int
    routing_adjustment: str
    breaker_key: str

    def __post_init__(self) -> None:
        _require_text(self.profile_id, "profile_id")
        _require_index(self.candidate_index, "candidate_index")
        _require_index(self.retry_index, "retry_index")
        _require_text(self.routing_adjustment, "routing_adjustment")
        _require_text(self.breaker_key, "breaker_key")


@dataclass(frozen=True, slots=True)
class ModelCompletedPayload:
    profile_id: str
    candidate_index: int
    retry_index: int
    succeeded: bool
    safe_error_code: str | None = None
    duration_ms: int = 0

    def __post_init__(self) -> None:
        _require_text(self.profile_id, "profile_id")
        _require_index(self.candidate_index, "candidate_index")
        _require_index(self.retry_index, "retry_index")
        if not isinstance(self.succeeded, bool):
            raise TypeError("succeeded 必须是 bool")
        if self.safe_error_code is not None:
            _require_text(self.safe_error_code, "safe_error_code")
        _require_index(self.duration_ms, "duration_ms")


@dataclass(frozen=True, slots=True)
class ToolStartedPayload:
    tool_name: str
    # Legacy v1/v2 identity fields. New evidence-bearing events never set
    # these fields and persist only stable digests.
    invocation_id: str | None = None
    attempt_id: str | None = None
    retry_index: int | None = None
    resource_key_digest: str | None = None
    tool_evidence_schema_version: int | None = None
    invocation_identity_digest: str | None = None
    attempt_identity_digest: str | None = None
    side_effect_kind: str | None = None
    idempotency_kind: str | None = None
    idempotency_key_digest: str | None = None
    replay_supported: bool | None = None
    side_effect_state: str | None = None
    compensation_state: str | None = None
    retry_disposition: str | None = None
    outcome_classification: str | None = None
    execution_detached: bool | None = None
    worker_terminated: bool | None = None
    provider_started: bool | None = None
    safe_error_code: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.tool_name, "tool_name")
        for value, name in (
            (self.invocation_id, "invocation_id"),
            (self.attempt_id, "attempt_id"),
            (self.resource_key_digest, "resource_key_digest"),
        ):
            if value is not None:
                _require_text(value, name)
        if self.retry_index is not None:
            _require_index(self.retry_index, "retry_index")
        _validate_tool_evidence_fields(self)


@dataclass(frozen=True, slots=True)
class ToolCompletedPayload:
    """Runtime Attempt 已结束等待；Detached 同步 Worker 可能仍在执行。"""

    tool_name: str
    succeeded: bool
    safe_error_code: str | None = None
    invocation_id: str | None = None
    attempt_id: str | None = None
    retry_index: int | None = None
    side_effect_state: str | None = None
    retry_disposition: str | None = None
    resource_key_digest: str | None = None
    worker_terminated: bool = True
    execution_detached: bool = False
    resource_release_pending: bool = False
    duration_ms: int = 0
    status: str | None = None
    tool_evidence_schema_version: int | None = None
    invocation_identity_digest: str | None = None
    attempt_identity_digest: str | None = None
    side_effect_kind: str | None = None
    idempotency_kind: str | None = None
    idempotency_key_digest: str | None = None
    replay_supported: bool | None = None
    compensation_state: str | None = None
    outcome_classification: str | None = None
    provider_started: bool | None = None
    result_present: bool | None = None
    result_digest: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.tool_name, "tool_name")
        if not isinstance(self.succeeded, bool):
            raise TypeError("succeeded 必须是 bool")
        if self.safe_error_code is not None:
            _require_text(self.safe_error_code, "safe_error_code")
        _require_index(self.duration_ms, "duration_ms")
        for value, name in (
            (self.invocation_id, "invocation_id"),
            (self.attempt_id, "attempt_id"),
            (self.side_effect_state, "side_effect_state"),
            (self.retry_disposition, "retry_disposition"),
            (self.resource_key_digest, "resource_key_digest"),
        ):
            if value is not None:
                _require_text(value, name)
        if self.retry_index is not None:
            _require_index(self.retry_index, "retry_index")
        if self.status is not None:
            _require_text(self.status, "status")
        for value, name in (
            (self.worker_terminated, "worker_terminated"),
            (self.execution_detached, "execution_detached"),
            (self.resource_release_pending, "resource_release_pending"),
        ):
            if not isinstance(value, bool):
                raise TypeError(f"{name} 必须是 bool")
        if self.execution_detached and self.worker_terminated:
            raise ValueError("Detached Worker 不能标记为已终止")
        if self.execution_detached and not self.resource_release_pending:
            raise ValueError("Detached Worker 必须等待资源清理")
        _validate_tool_evidence_fields(self)


@dataclass(frozen=True, slots=True)
class RetrievalBudgetPayload:
    """只包含 Retrieval 维度计数，不包含 Query、向量或正文。"""

    retrieval_calls: int = 0
    embedding_calls: int = 0
    vector_queries: int = 0
    keyword_queries: int = 0
    document_reads: int = 0
    context_chars: int = 0

    def __post_init__(self) -> None:
        for name in (
            "retrieval_calls",
            "embedding_calls",
            "vector_queries",
            "keyword_queries",
            "document_reads",
            "context_chars",
        ):
            _require_index(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class RetrievalStartedPayload:
    retrieval_id: str
    query_digest: str
    collection_count: int
    top_k: int

    def __post_init__(self) -> None:
        _require_text(self.retrieval_id, "retrieval_id")
        _require_text(self.query_digest, "query_digest")
        _require_index(self.collection_count, "collection_count")
        if self.collection_count <= 0:
            raise ValueError("collection_count 必须是正整数")
        _require_index(self.top_k, "top_k")
        if self.top_k <= 0:
            raise ValueError("top_k 必须是正整数")


@dataclass(frozen=True, slots=True)
class RetrievalStageCompletedPayload:
    stage: str
    status: str
    duration_ms: int
    input_count: int
    output_count: int
    degraded: bool
    budget_usage: RetrievalBudgetPayload
    safe_error_code: str | None = None
    worker_terminated: bool = True
    execution_detached: bool = False
    background_work_pending: bool = False

    def __post_init__(self) -> None:
        _require_text(self.stage, "stage")
        _require_text(self.status, "status")
        _require_index(self.duration_ms, "duration_ms")
        _require_index(self.input_count, "input_count")
        _require_index(self.output_count, "output_count")
        if not isinstance(self.degraded, bool):
            raise TypeError("degraded 必须是 bool")
        if not isinstance(self.budget_usage, RetrievalBudgetPayload):
            raise TypeError("budget_usage 必须是 RetrievalBudgetPayload")
        if self.safe_error_code is not None:
            _require_text(self.safe_error_code, "safe_error_code")
        for name in (
            "worker_terminated",
            "execution_detached",
            "background_work_pending",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} 必须是 bool")
        if self.execution_detached and self.worker_terminated:
            raise ValueError("Detached Worker 不能标记为已终止")
        if self.execution_detached and not self.background_work_pending:
            raise ValueError("Detached Worker 必须标记后台工作待完成")


@dataclass(frozen=True, slots=True)
class RetrievalCompletedPayload:
    retrieval_id: str
    status: str
    duration_ms: int
    chunk_count: int
    citation_count: int
    degraded: bool
    budget_usage: RetrievalBudgetPayload
    safe_error_code: str | None = None
    worker_terminated: bool = True
    execution_detached: bool = False
    background_work_pending: bool = False

    def __post_init__(self) -> None:
        _require_text(self.retrieval_id, "retrieval_id")
        _require_text(self.status, "status")
        _require_index(self.duration_ms, "duration_ms")
        _require_index(self.chunk_count, "chunk_count")
        _require_index(self.citation_count, "citation_count")
        if self.chunk_count != self.citation_count:
            raise ValueError("Retrieval 完成事件的 Chunk 与 Citation 数量必须一致")
        if not isinstance(self.degraded, bool):
            raise TypeError("degraded 必须是 bool")
        if not isinstance(self.budget_usage, RetrievalBudgetPayload):
            raise TypeError("budget_usage 必须是 RetrievalBudgetPayload")
        if self.safe_error_code is not None:
            _require_text(self.safe_error_code, "safe_error_code")
        for name in (
            "worker_terminated",
            "execution_detached",
            "background_work_pending",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} 必须是 bool")
        if self.execution_detached and self.worker_terminated:
            raise ValueError("Detached Worker 不能标记为已终止")
        if self.execution_detached and not self.background_work_pending:
            raise ValueError("Detached Worker 必须标记后台工作待完成")


@dataclass(frozen=True, slots=True)
class OutputDeltaPayload:
    """唯一允许承载最终用户可见正文的 Payload。"""

    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("text 必须是字符串")


@dataclass(frozen=True, slots=True)
class StepCompletedPayload:
    status: str
    safe_error_code: str | None = None
    duration_ms: int = 0
    # WP5 safe delivery/result facts. Legacy records may omit them.
    result_char_count: int = 0
    delivery_status: str | None = None
    delivery_duration_ms: int = 0
    execution_kind: str | None = None
    output_policy: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.status, "status")
        if self.safe_error_code is not None:
            _require_text(self.safe_error_code, "safe_error_code")
        _require_index(self.duration_ms, "duration_ms")
        _require_index(self.result_char_count, "result_char_count")
        _require_index(self.delivery_duration_ms, "delivery_duration_ms")
        if self.delivery_status is not None:
            _require_text(self.delivery_status, "delivery_status")
        for value, name in (
            (self.execution_kind, "execution_kind"),
            (self.output_policy, "output_policy"),
        ):
            if value is not None:
                _require_text(value, name)


@dataclass(frozen=True, slots=True)
class MemoryFormationCompletedPayload:
    """WP2 post-delivery Semantic Memory Formation 的安全 outcome 投影。

    Journal 只允许原子值，per-candidate outcome 以紧凑编码字符串表达：
    ``ordinal|OUTCOME|REASON|memory_id`` 以 ``;`` 连接；零 candidate 时为
    ``NONE``。禁止携带 user query、final answer、Memory 正文、payload value、
    source quote、prompt、CoT、raw exception、tool/RAG/provider 数据或路径。
    """

    exchange_id: str
    agent_id: str
    memory_scope: str
    formation_method: str
    status: str
    schema_version: int
    proposed_count: int
    accepted_count: int
    ignored_count: int
    persisted_count: int
    reused_count: int
    failed_count: int
    formation_total_duration_ms: int
    model_extraction_duration_ms: int
    persistence_duration_ms: int
    safe_error_code: str | None = None
    candidate_outcomes: str = "NONE"

    def __post_init__(self) -> None:
        _require_text(self.exchange_id, "exchange_id")
        _require_text(self.agent_id, "agent_id")
        _require_text(self.memory_scope, "memory_scope")
        _require_text(self.formation_method, "formation_method")
        _require_text(self.status, "status")
        if self.status not in {
            "SUCCEEDED",
            "PARTIAL",
            "FAILED",
            "CANCELLED",
            "TIMED_OUT",
        }:
            raise ValueError("未知 memory formation status")
        if self.formation_method != "HYBRID":
            raise ValueError("WP2 formation_method 只允许 HYBRID")
        _require_index(self.schema_version, "schema_version")
        for name in (
            "proposed_count",
            "accepted_count",
            "ignored_count",
            "persisted_count",
            "reused_count",
            "failed_count",
            "formation_total_duration_ms",
            "model_extraction_duration_ms",
            "persistence_duration_ms",
        ):
            _require_index(getattr(self, name), name)
        if self.safe_error_code is not None:
            _require_text(self.safe_error_code, "safe_error_code")
        _require_text(self.candidate_outcomes, "candidate_outcomes")
        if len(self.candidate_outcomes) > 2048:
            raise ValueError("candidate_outcomes 超过 bounded 长度")


@dataclass(frozen=True, slots=True)
class EpisodicMemoryFormationCompletedPayload:
    """WP6-C safe projection；永远不携带 episode 正文或 raw evidence。"""

    origin_run_id: str
    outcome: str
    memory_id: str | None
    lesson_status: str
    safe_reason: str | None = None
    episode_kind: str = "RUN"
    origin_step_id: str | None = None
    verified_performer_agent_id: str | None = None
    owner_match: bool = True

    def __post_init__(self) -> None:
        _require_text(self.origin_run_id, "origin_run_id")
        if self.outcome not in {"CREATED", "REUSED", "SKIPPED", "FAILED"}:
            raise ValueError("未知 episodic formation outcome")
        if self.memory_id is not None:
            _require_text(self.memory_id, "memory_id")
        if self.lesson_status not in {"ABSENT", "ACCEPTED", "REJECTED", "ERROR"}:
            raise ValueError("未知 lesson_status")
        if self.safe_reason is not None:
            _require_text(self.safe_reason, "safe_reason")
        if self.episode_kind not in {"RUN", "STEP"}:
            raise ValueError("未知 episode_kind")
        if self.origin_step_id is not None:
            _require_text(self.origin_step_id, "origin_step_id")
        if self.verified_performer_agent_id is not None:
            _require_text(self.verified_performer_agent_id, "verified_performer_agent_id")
        if not isinstance(self.owner_match, bool):
            raise TypeError("owner_match 必须是 bool")


@dataclass(frozen=True, slots=True)
class MemoryRetrievalCompletedPayload:
    """WP4-B Long-term Memory retrieval 的安全 outcome 投影。

    Business Authority 始终是 SQLite ``long_term_memory`` row；本 payload
    只是 derived observation。禁止携带 raw query、canonical text、payload、
    logical_key、prompt、source excerpt、origin/run/exchange IDs、
    raw exception、provider data 或路径。``selected_count`` 是 retrieval
    selection；``context_record_count`` 只表示 Planner ContextBuilder 实际接纳
    的 record 数；``direct_entry_supplied`` 不表示后续 direct invocation 的
    Builder acceptance。selection != supplied != injection。
    """

    retrieval_method: str
    ranking_method: str
    status: str
    schema_version: int
    candidate_count: int
    eligible_count: int
    selected_count: int
    context_record_count: int
    malformed_count: int
    omitted_count: int
    budget_used_chars: int
    registered_selected_count: int
    open_selected_count: int
    duration_ms: int
    planning_injected: bool
    direct_entry_supplied: bool
    safe_error_code: str | None = None
    episodic_candidate_count: int = 0
    episodic_selected_count: int = 0
    episodic_context_record_count: int = 0

    def __post_init__(self) -> None:
        _require_text(self.retrieval_method, "retrieval_method")
        _require_text(self.ranking_method, "ranking_method")
        _require_text(self.status, "status")
        if self.status not in {"SUCCEEDED", "FAILED"}:
            raise ValueError("未知 memory retrieval status")
        _require_index(self.schema_version, "schema_version")
        for name in (
            "candidate_count",
            "eligible_count",
            "selected_count",
            "context_record_count",
            "malformed_count",
            "omitted_count",
            "budget_used_chars",
            "registered_selected_count",
            "open_selected_count",
            "duration_ms",
            "episodic_candidate_count",
            "episodic_selected_count",
            "episodic_context_record_count",
        ):
            _require_index(getattr(self, name), name)
        for name in ("planning_injected", "direct_entry_supplied"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} 必须是 bool")
        if self.safe_error_code is not None:
            _require_text(self.safe_error_code, "safe_error_code")


@dataclass(frozen=True, slots=True)
class BudgetExhaustedPayload:
    component: str
    dimension: str = "unknown"
    safe_error_code: str = "BUDGET_EXHAUSTED"

    def __post_init__(self) -> None:
        _require_text(self.component, "component")
        _require_text(self.dimension, "dimension")
        _require_text(self.safe_error_code, "safe_error_code")


@dataclass(frozen=True, slots=True)
class MemoryLifecycleResolvedPayload:
    """WP3-B keyed Semantic Memory lifecycle 的安全 outcome 投影。

    Business Authority 是 SQLite ``SemanticMemoryRecord`` row；本 payload 只是
    derived observation。禁止携带 canonical text、payload value、logical key、
    forget query、source excerpt、prompt、CoT、raw exception、Tool/RAG/provider
    数据或文件路径。transition 以 ``memory_id|before|after`` 用 ``;`` 连接；
    有固定小上限并按 memory_id ASC deterministic 编码，超限显式标记。
    """

    exchange_id: str
    agent_id: str
    memory_scope: str
    memory_type: str
    operation: str
    outcome: str
    schema_version: int
    affected_count: int
    resolution_duration_ms: int
    mutation_duration_ms: int
    candidate_outcome: str | None = None
    winner_memory_id: str | None = None
    new_memory_id: str | None = None
    affected_transitions: str = "NONE"
    ids_truncated: bool = False
    omitted_count: int = 0
    safe_reason: str = ""
    safe_error_code: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.exchange_id, "exchange_id")
        _require_text(self.agent_id, "agent_id")
        _require_text(self.memory_scope, "memory_scope")
        _require_text(self.memory_type, "memory_type")
        _require_text(self.operation, "operation")
        if self.operation not in {"INSERT", "NO_CHANGE", "SUPERSEDE", "FORGET"}:
            raise ValueError("未知 lifecycle operation")
        _require_text(self.outcome, "outcome")
        if self.outcome not in {
            "OK",
            "NOT_FOUND",
            "ALREADY_FORGOTTEN",
            "FAILED_CLOSED",
        }:
            raise ValueError("未知 lifecycle outcome")
        _require_index(self.schema_version, "schema_version")
        _require_index(self.affected_count, "affected_count")
        _require_index(self.resolution_duration_ms, "resolution_duration_ms")
        _require_index(self.mutation_duration_ms, "mutation_duration_ms")
        for name in (
            "candidate_outcome",
            "winner_memory_id",
            "new_memory_id",
            "safe_reason",
            "safe_error_code",
        ):
            value = getattr(self, name)
            if value is not None and value != "":
                _require_text(value, name)
        _require_text(self.affected_transitions, "affected_transitions")
        if len(self.affected_transitions) > 2048:
            raise ValueError("affected_transitions 超过 bounded 长度")
        if not isinstance(self.ids_truncated, bool):
            raise TypeError("ids_truncated 必须是 bool")
        _require_index(self.omitted_count, "omitted_count")


@dataclass(frozen=True, slots=True)
class TimeoutPayload:
    component: str
    safe_error_code: str = "DEADLINE_EXCEEDED"

    def __post_init__(self) -> None:
        _require_text(self.component, "component")
        _require_text(self.safe_error_code, "safe_error_code")


@dataclass(frozen=True, slots=True)
class ErrorPayload:
    safe_error_code: str
    safe_message: str
    component: str
    fatal: bool
    # WP5 layered delivery/Memory facts. Legacy records may omit them.
    delivery_status: str | None = None
    final_step_status: str | None = None
    memory_commit_status: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.safe_error_code, "safe_error_code")
        _require_text(self.safe_message, "safe_message")
        _require_text(self.component, "component")
        if not isinstance(self.fatal, bool):
            raise TypeError("fatal 必须是 bool")
        for value, name in (
            (self.delivery_status, "delivery_status"),
            (self.final_step_status, "final_step_status"),
            (self.memory_commit_status, "memory_commit_status"),
        ):
            if value is not None:
                _require_text(value, name)


@dataclass(frozen=True, slots=True)
class CancellationPayload:
    reason: str
    component: str

    def __post_init__(self) -> None:
        _require_text(self.reason, "reason")
        _require_text(self.component, "component")


@dataclass(frozen=True, slots=True)
class RunCompletedPayload:
    status: str
    stop_reason: str
    duration_ms: int = 0
    # WP5 layered terminal facts. Legacy records may omit them.
    safe_error_code: str | None = None
    delivery_status: str | None = None
    final_step_status: str | None = None
    memory_commit_status: str | None = None
    memory_duration_ms: int = 0
    shape: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.status, "status")
        _require_text(self.stop_reason, "stop_reason")
        _require_index(self.duration_ms, "duration_ms")
        _require_index(self.memory_duration_ms, "memory_duration_ms")
        for value, name in (
            (self.safe_error_code, "safe_error_code"),
            (self.delivery_status, "delivery_status"),
            (self.final_step_status, "final_step_status"),
            (self.memory_commit_status, "memory_commit_status"),
            (self.shape, "shape"),
        ):
            if value is not None:
                _require_text(value, name)
        if self.shape is not None and self.shape not in {"0", "1", "2", "3"}:
            raise ValueError("shape 必须是 0/1/2/3")


RuntimeEventPayload: TypeAlias = (
    RunStartedPayload
    | PlanningStartedPayload
    | PlanCreatedPayload
    | StepStartedPayload
    | ModelStartedPayload
    | ModelCompletedPayload
    | ToolStartedPayload
    | ToolCompletedPayload
    | RetrievalStartedPayload
    | RetrievalStageCompletedPayload
    | RetrievalCompletedPayload
    | OutputDeltaPayload
    | StepCompletedPayload
    | MemoryFormationCompletedPayload
    | EpisodicMemoryFormationCompletedPayload
    | MemoryLifecycleResolvedPayload
    | MemoryRetrievalCompletedPayload
    | BudgetExhaustedPayload
    | TimeoutPayload
    | ErrorPayload
    | CancellationPayload
    | RunCompletedPayload
)


_PAYLOAD_TYPES: dict[RuntimeEventType, type[RuntimeEventPayload]] = {
    RuntimeEventType.RUN_STARTED: RunStartedPayload,
    RuntimeEventType.PLANNING_STARTED: PlanningStartedPayload,
    RuntimeEventType.PLAN_CREATED: PlanCreatedPayload,
    RuntimeEventType.STEP_STARTED: StepStartedPayload,
    RuntimeEventType.MODEL_STARTED: ModelStartedPayload,
    RuntimeEventType.MODEL_COMPLETED: ModelCompletedPayload,
    RuntimeEventType.TOOL_STARTED: ToolStartedPayload,
    RuntimeEventType.TOOL_COMPLETED: ToolCompletedPayload,
    RuntimeEventType.RETRIEVAL_STARTED: RetrievalStartedPayload,
    RuntimeEventType.RETRIEVAL_STAGE_COMPLETED: RetrievalStageCompletedPayload,
    RuntimeEventType.RETRIEVAL_COMPLETED: RetrievalCompletedPayload,
    RuntimeEventType.OUTPUT_DELTA: OutputDeltaPayload,
    RuntimeEventType.STEP_COMPLETED: StepCompletedPayload,
    RuntimeEventType.MEMORY_FORMATION_COMPLETED: MemoryFormationCompletedPayload,
    RuntimeEventType.EPISODIC_MEMORY_FORMATION_COMPLETED: EpisodicMemoryFormationCompletedPayload,
    RuntimeEventType.MEMORY_LIFECYCLE_RESOLVED: MemoryLifecycleResolvedPayload,
    RuntimeEventType.MEMORY_RETRIEVAL_COMPLETED: MemoryRetrievalCompletedPayload,
    RuntimeEventType.BUDGET_EXHAUSTED: BudgetExhaustedPayload,
    RuntimeEventType.TIMEOUT: TimeoutPayload,
    RuntimeEventType.ERROR: ErrorPayload,
    RuntimeEventType.CANCELLATION: CancellationPayload,
    RuntimeEventType.RUN_COMPLETED: RunCompletedPayload,
}


_JOURNAL_PAYLOAD_FIELDS: dict[type[RuntimeEventPayload], tuple[str, ...]] = {
    RunStartedPayload: ("status",),
    PlanningStartedPayload: (
        "planner_schema_version",
        "configured_timeout_ms",
    ),
    PlanCreatedPayload: (
        "plan_id",
        "plan_version",
        "fingerprint",
        "step_count",
        "planning_source",
        "shape",
    ),
    StepStartedPayload: (
        "status",
        "agent_id",
        "execution_kind",
        "output_policy",
        "dependency_count",
    ),
    ModelStartedPayload: (
        "profile_id",
        "candidate_index",
        "retry_index",
        "routing_adjustment",
        "breaker_key",
    ),
    ModelCompletedPayload: (
        "profile_id",
        "candidate_index",
        "retry_index",
        "succeeded",
        "safe_error_code",
        "duration_ms",
    ),
    ToolStartedPayload: (
        "tool_name",
        "invocation_id",
        "attempt_id",
        "retry_index",
        "resource_key_digest",
    ),
    ToolCompletedPayload: (
        "tool_name",
        "succeeded",
        "safe_error_code",
        "invocation_id",
        "attempt_id",
        "retry_index",
        "side_effect_state",
        "retry_disposition",
        "resource_key_digest",
        "worker_terminated",
        "execution_detached",
        "resource_release_pending",
        "duration_ms",
        "status",
    ),
    RetrievalStartedPayload: (
        "retrieval_id",
        "query_digest",
        "collection_count",
        "top_k",
    ),
    RetrievalStageCompletedPayload: (
        "stage",
        "status",
        "duration_ms",
        "input_count",
        "output_count",
        "degraded",
        "budget_usage",
        "safe_error_code",
        "worker_terminated",
        "execution_detached",
        "background_work_pending",
    ),
    RetrievalCompletedPayload: (
        "retrieval_id",
        "status",
        "duration_ms",
        "chunk_count",
        "citation_count",
        "degraded",
        "budget_usage",
        "safe_error_code",
        "worker_terminated",
        "execution_detached",
        "background_work_pending",
    ),
    StepCompletedPayload: (
        "status",
        "safe_error_code",
        "duration_ms",
        "result_char_count",
        "delivery_status",
        "delivery_duration_ms",
        "execution_kind",
        "output_policy",
    ),
    MemoryFormationCompletedPayload: (
        "exchange_id",
        "agent_id",
        "memory_scope",
        "formation_method",
        "status",
        "safe_error_code",
        "schema_version",
        "proposed_count",
        "accepted_count",
        "ignored_count",
        "persisted_count",
        "reused_count",
        "failed_count",
        "formation_total_duration_ms",
        "model_extraction_duration_ms",
        "persistence_duration_ms",
        "candidate_outcomes",
    ),
    EpisodicMemoryFormationCompletedPayload: (
        "origin_run_id", "outcome", "memory_id", "lesson_status", "safe_reason",
        "episode_kind", "origin_step_id", "verified_performer_agent_id", "owner_match",
    ),
    MemoryLifecycleResolvedPayload: (
        "exchange_id",
        "agent_id",
        "memory_scope",
        "memory_type",
        "operation",
        "outcome",
        "candidate_outcome",
        "winner_memory_id",
        "new_memory_id",
        "affected_count",
        "affected_transitions",
        "ids_truncated",
        "omitted_count",
        "safe_reason",
        "safe_error_code",
        "resolution_duration_ms",
        "mutation_duration_ms",
        "schema_version",
    ),
    MemoryRetrievalCompletedPayload: (
        "retrieval_method",
        "ranking_method",
        "status",
        "schema_version",
        "candidate_count",
        "eligible_count",
        "selected_count",
        "context_record_count",
        "malformed_count",
        "omitted_count",
        "budget_used_chars",
        "registered_selected_count",
        "open_selected_count",
        "duration_ms",
        "planning_injected",
        "direct_entry_supplied",
        "safe_error_code",
        "episodic_candidate_count",
        "episodic_selected_count",
        "episodic_context_record_count",
    ),
    BudgetExhaustedPayload: ("component", "dimension", "safe_error_code"),
    TimeoutPayload: ("component", "safe_error_code"),
    ErrorPayload: (
        "safe_error_code",
        "safe_message",
        "component",
        "fatal",
        "delivery_status",
        "final_step_status",
        "memory_commit_status",
    ),
    CancellationPayload: ("reason", "component"),
    RunCompletedPayload: (
        "status",
        "stop_reason",
        "duration_ms",
        "safe_error_code",
        "delivery_status",
        "final_step_status",
        "memory_commit_status",
        "memory_duration_ms",
        "shape",
    ),
}

# 仅用于读取第 20 天收尾前已写入的 schema v1 记录。新事件始终写出这些字段；
# 旧记录缺少权威 duration 时可继续校验和消费，但无法补造 Histogram。
_LEGACY_TOOL_STARTED_JOURNAL_FIELDS = _JOURNAL_PAYLOAD_FIELDS[
    ToolStartedPayload
]
_LEGACY_TOOL_COMPLETED_JOURNAL_FIELDS = _JOURNAL_PAYLOAD_FIELDS[
    ToolCompletedPayload
]
_TOOL_EVIDENCE_FIELDS = (
    "tool_evidence_schema_version",
    "invocation_identity_digest",
    "attempt_identity_digest",
    "side_effect_kind",
    "idempotency_kind",
    "idempotency_key_digest",
    "replay_supported",
    "side_effect_state",
    "compensation_state",
    "retry_disposition",
    "outcome_classification",
    "execution_detached",
    "worker_terminated",
    "provider_started",
    "safe_error_code",
)
_TOOL_STARTED_EVIDENCE_JOURNAL_FIELDS = (
    "tool_name",
    "retry_index",
    *_TOOL_EVIDENCE_FIELDS,
)
_TOOL_COMPLETED_EVIDENCE_JOURNAL_FIELDS = (
    "tool_name",
    "succeeded",
    "retry_index",
    "duration_ms",
    "status",
    *_TOOL_EVIDENCE_FIELDS,
    "result_present",
    "result_digest",
)


_LEGACY_OPTIONAL_JOURNAL_FIELDS: dict[
    RuntimeEventType, frozenset[str]
] = {
    RuntimeEventType.MODEL_COMPLETED: frozenset({"duration_ms"}),
    RuntimeEventType.TOOL_COMPLETED: frozenset({"duration_ms", "status"}),
    RuntimeEventType.PLAN_CREATED: frozenset({"shape"}),
    RuntimeEventType.STEP_STARTED: frozenset(
        {"agent_id", "execution_kind", "output_policy", "dependency_count"}
    ),
    RuntimeEventType.STEP_COMPLETED: frozenset(
        {
            "duration_ms",
            "result_char_count",
            "delivery_status",
            "delivery_duration_ms",
            "execution_kind",
            "output_policy",
        }
    ),
    RuntimeEventType.ERROR: frozenset(
        {"delivery_status", "final_step_status", "memory_commit_status"}
    ),
    RuntimeEventType.RUN_COMPLETED: frozenset(
        {
            "duration_ms",
            "safe_error_code",
            "delivery_status",
            "final_step_status",
            "memory_commit_status",
            "memory_duration_ms",
            "shape",
        }
    ),
    RuntimeEventType.BUDGET_EXHAUSTED: frozenset({"dimension"}),
}


@dataclass(frozen=True, slots=True)
class RuntimeEventDraft:
    """尚未分配全局序号的内部事件事实。"""

    run_id: str
    trace_id: str
    event_type: RuntimeEventType
    component: str
    payload: RuntimeEventPayload
    step_id: str | None = None
    step_sequence: int | None = None
    span_id: str | None = None
    parent_span_id: str | None = None

    def __post_init__(self) -> None:
        _validate_common(
            self.run_id,
            self.trace_id,
            self.event_type,
            self.component,
            self.payload,
            self.step_id,
            self.step_sequence,
        )


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    """已由单 Run Channel 排序的不可变事件信封。"""

    schema_version: int
    event_id: str
    run_id: str
    trace_id: str
    sequence: int
    event_type: RuntimeEventType
    emitted_at: datetime
    component: str
    payload: RuntimeEventPayload
    step_id: str | None = None
    step_sequence: int | None = None
    span_id: str | None = None
    parent_span_id: str | None = None

    CURRENT_SCHEMA_VERSION: ClassVar[int] = RUNTIME_EVENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version not in {1, self.CURRENT_SCHEMA_VERSION}:
            raise ValueError("不支持的 Runtime Event schema_version")
        _require_text(self.event_id, "event_id")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence <= 0:
            raise ValueError("sequence 必须是正整数")
        if self.emitted_at.tzinfo is None or self.emitted_at.utcoffset() is None:
            raise ValueError("emitted_at 必须是 UTC 时间")
        if self.emitted_at.utcoffset().total_seconds() != 0:
            raise ValueError("emitted_at 必须是 UTC 时间")
        _validate_common(
            self.run_id,
            self.trace_id,
            self.event_type,
            self.component,
            self.payload,
            self.step_id,
            self.step_sequence,
        )

    @classmethod
    def from_draft(cls, draft: RuntimeEventDraft, sequence: int) -> "RuntimeEvent":
        """仅供单 Run sequence owner 在入队时创建信封。"""
        return cls(
            schema_version=RUNTIME_EVENT_SCHEMA_VERSION,
            event_id=uuid4().hex,
            run_id=draft.run_id,
            trace_id=draft.trace_id,
            sequence=sequence,
            event_type=draft.event_type,
            emitted_at=datetime.now(UTC),
            component=draft.component,
            payload=draft.payload,
            step_id=draft.step_id,
            step_sequence=draft.step_sequence,
            span_id=draft.span_id,
            parent_span_id=draft.parent_span_id,
        )

    def to_safe_dict(self, *, include_output: bool = False) -> dict[str, object]:
        """返回日志、诊断和 Transport 可显式选择的安全字典。"""
        if isinstance(self.payload, OutputDeltaPayload) and not include_output:
            return {
                "run_id": self.run_id,
                "sequence": self.sequence,
                "event_type": self.event_type.value,
                "step_id": self.step_id,
                "payload": {"text_length": len(self.payload.text)},
            }
        result: dict[str, object] = {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "sequence": self.sequence,
            "event_type": self.event_type.value,
            "emitted_at": self.emitted_at.isoformat(),
            "step_id": self.step_id,
            "step_sequence": self.step_sequence,
            "component": self.component,
        }
        if isinstance(self.payload, OutputDeltaPayload):
            result["payload"] = {
                "text_length": len(self.payload.text),
                "text": self.payload.text,
            }
        elif isinstance(
            self.payload, (ToolStartedPayload, ToolCompletedPayload)
        ) and self.payload.tool_evidence_schema_version is not None:
            fields = _journal_payload_fields(self.payload)
            assert fields is not None
            result["payload"] = {
                name: _to_journal_value(getattr(self.payload, name))
                for name in fields
            }
        else:
            result["payload"] = asdict(self.payload)
        return result

    def to_journal_dict(self) -> dict[str, object]:
        """返回 Journal 专用安全投影，不包含任何业务正文。

        该投影按强类型 Payload 的字段 allowlist 构造。OutputDelta 只记录
        字符长度与摘要，调用方不得用 ``asdict(event)`` 替代此方法。
        """
        if isinstance(self.payload, OutputDeltaPayload):
            safe_payload: dict[str, object] = {
                "text_length": len(self.payload.text),
                "text_digest": hashlib.sha256(
                    self.payload.text.encode("utf-8")
                ).hexdigest(),
            }
        else:
            fields = _journal_payload_fields(self.payload)
            if fields is None:  # pragma: no cover - _PAYLOAD_TYPES 的封闭性保护
                raise TypeError("Runtime Event Payload 没有 Journal allowlist")
            safe_payload = {
                name: _to_journal_value(getattr(self.payload, name))
                for name in fields
            }
        validate_journal_payload(self.event_type, safe_payload)
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "sequence": self.sequence,
            "event_type": self.event_type.value,
            "emitted_at": self.emitted_at.isoformat(),
            "component": self.component,
            "step_id": self.step_id,
            "step_sequence": self.step_sequence,
            "safe_payload": safe_payload,
        }


def _to_journal_value(value: object) -> object:
    """只接受 Journal schema 明确允许的原子值与 Retrieval 计数。"""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, RetrievalBudgetPayload):
        return {
            name: getattr(value, name)
            for name in (
                "retrieval_calls",
                "embedding_calls",
                "vector_queries",
                "keyword_queries",
                "document_reads",
                "context_chars",
            )
        }
    raise TypeError("Journal Payload 包含未允许的值类型")


def validate_journal_payload(
    event_type: RuntimeEventType, safe_payload: dict[str, object]
) -> None:
    """读取与写入两端共用的 event-type allowlist 严格校验。"""
    if not isinstance(event_type, RuntimeEventType):
        raise TypeError("event_type 必须是 RuntimeEventType")
    if not isinstance(safe_payload, dict):
        raise TypeError("safe_payload 必须是 dict")
    if event_type is RuntimeEventType.OUTPUT_DELTA:
        if set(safe_payload) != {"text_length", "text_digest"}:
            raise ValueError("OutputDelta Journal Payload 字段不匹配")
        text_length = safe_payload["text_length"]
        text_digest = safe_payload["text_digest"]
        if (
            isinstance(text_length, bool)
            or not isinstance(text_length, int)
            or text_length < 0
        ):
            raise ValueError("text_length 必须是非负整数")
        if (
            not isinstance(text_digest, str)
            or len(text_digest) != 64
            or any(c not in "0123456789abcdef" for c in text_digest)
        ):
            raise ValueError("text_digest 必须是小写 SHA-256")
        return
    evidence_schema = safe_payload.get("tool_evidence_schema_version")
    if (
        event_type is RuntimeEventType.TOOL_STARTED
        and evidence_schema is not None
    ):
        expected_fields = _TOOL_STARTED_EVIDENCE_JOURNAL_FIELDS
        optional_fields = frozenset()
        _validate_persisted_tool_evidence(safe_payload)
    elif (
        event_type is RuntimeEventType.TOOL_COMPLETED
        and evidence_schema is not None
    ):
        expected_fields = _TOOL_COMPLETED_EVIDENCE_JOURNAL_FIELDS
        # Day 24 added result evidence without changing RuntimeEvent v1/v2.
        # Historical v1/v2 records must retain their original field set and
        # digest, so readers accept absence as explicit compatibility Unknown.
        optional_fields = frozenset({"result_present", "result_digest"})
        _validate_persisted_tool_evidence(safe_payload)
    else:
        payload_type = _PAYLOAD_TYPES[event_type]
        expected_fields = _JOURNAL_PAYLOAD_FIELDS.get(payload_type)
        optional_fields = _LEGACY_OPTIONAL_JOURNAL_FIELDS.get(
            event_type, frozenset()
        )
    if expected_fields is None:  # pragma: no cover - 封闭类型映射保护
        raise ValueError("Event Type 没有 Journal Payload allowlist")
    actual_fields = set(safe_payload)
    expected_field_set = set(expected_fields)
    optional_legacy_fields = optional_fields
    if (
        not actual_fields <= expected_field_set
        or not expected_field_set - optional_legacy_fields <= actual_fields
    ):
        raise ValueError("Journal Payload 字段与 Event Type 不匹配")
    budget_fields = {
        "retrieval_calls",
        "embedding_calls",
        "vector_queries",
        "keyword_queries",
        "document_reads",
        "context_chars",
    }
    for name, value in safe_payload.items():
        if name == "budget_usage":
            if not isinstance(value, dict) or set(value) != budget_fields:
                raise ValueError("budget_usage 字段不匹配")
            for count in value.values():
                if (
                    isinstance(count, bool)
                    or not isinstance(count, int)
                    or count < 0
                ):
                    raise ValueError("budget_usage 计数必须是非负整数")
            continue
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, int) and not isinstance(value, bool):
            if value < 0:
                raise ValueError(f"{name} 必须是非负整数")
            continue
        if isinstance(value, str) and value.strip():
            continue
        raise ValueError(f"{name} 包含不允许的 Journal 值")


def _journal_payload_fields(
    payload: RuntimeEventPayload,
) -> tuple[str, ...] | None:
    if isinstance(payload, ToolStartedPayload):
        if payload.tool_evidence_schema_version is not None:
            return _TOOL_STARTED_EVIDENCE_JOURNAL_FIELDS
        return _LEGACY_TOOL_STARTED_JOURNAL_FIELDS
    if isinstance(payload, ToolCompletedPayload):
        if payload.tool_evidence_schema_version is not None:
            return _TOOL_COMPLETED_EVIDENCE_JOURNAL_FIELDS
        return _LEGACY_TOOL_COMPLETED_JOURNAL_FIELDS
    return _JOURNAL_PAYLOAD_FIELDS.get(type(payload))


def _validate_tool_evidence_fields(payload: object) -> None:
    version = getattr(payload, "tool_evidence_schema_version")
    if version is None:
        return
    if version != TOOL_EVIDENCE_SCHEMA_VERSION:
        raise ValueError("unsupported tool_evidence_schema_version")
    for name in ("invocation_identity_digest", "attempt_identity_digest"):
        value = getattr(payload, name)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)
        ):
            raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    for name in (
        "side_effect_kind",
        "idempotency_kind",
        "side_effect_state",
        "compensation_state",
        "retry_disposition",
        "outcome_classification",
    ):
        _require_text(getattr(payload, name), name)
    key_digest = getattr(payload, "idempotency_key_digest")
    if key_digest is not None and (
        not isinstance(key_digest, str)
        or len(key_digest) != 64
        or any(char not in "0123456789abcdef" for char in key_digest)
    ):
        raise ValueError(
            "idempotency_key_digest must be a lowercase SHA-256 digest"
        )
    for name in (
        "replay_supported",
        "execution_detached",
        "worker_terminated",
        "provider_started",
    ):
        if type(getattr(payload, name)) is not bool:
            raise TypeError(f"{name} must be bool")
    result_present = getattr(payload, "result_present", None)
    if result_present is not None and type(result_present) is not bool:
        raise TypeError("result_present must be bool")
    result_digest = getattr(payload, "result_digest", None)
    if result_digest is not None and (
        not isinstance(result_digest, str)
        or len(result_digest) != 64
        or any(char not in "0123456789abcdef" for char in result_digest)
    ):
        raise ValueError("result_digest must be a lowercase SHA-256 digest")
    if result_present is False and result_digest is not None:
        raise ValueError("result_digest requires result_present=true")
    safe_error_code = getattr(payload, "safe_error_code")
    if safe_error_code is not None:
        _require_text(safe_error_code, "safe_error_code")


def _validate_persisted_tool_evidence(
    safe_payload: dict[str, object],
) -> None:
    version = safe_payload.get("tool_evidence_schema_version")
    if (
        isinstance(version, bool)
        or version != TOOL_EVIDENCE_SCHEMA_VERSION
    ):
        raise ValueError("unsupported tool evidence payload schema")
    for name in ("invocation_identity_digest", "attempt_identity_digest"):
        value = safe_payload.get(name)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)
        ):
            raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    key_digest = safe_payload.get("idempotency_key_digest")
    if key_digest is not None and (
        not isinstance(key_digest, str)
        or len(key_digest) != 64
        or any(char not in "0123456789abcdef" for char in key_digest)
    ):
        raise ValueError(
            "idempotency_key_digest must be a lowercase SHA-256 digest"
        )
    result_present = safe_payload.get("result_present")
    if result_present is not None and type(result_present) is not bool:
        raise TypeError("result_present must be bool")
    result_digest = safe_payload.get("result_digest")
    if result_digest is not None and (
        not isinstance(result_digest, str)
        or len(result_digest) != 64
        or any(char not in "0123456789abcdef" for char in result_digest)
    ):
        raise ValueError("result_digest must be a lowercase SHA-256 digest")
    if result_present is False and result_digest is not None:
        raise ValueError("result_digest requires result_present=true")
    for name in (
        "side_effect_kind",
        "idempotency_kind",
        "side_effect_state",
        "compensation_state",
        "retry_disposition",
        "outcome_classification",
    ):
        value = safe_payload.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
    for name in (
        "replay_supported",
        "execution_detached",
        "worker_terminated",
        "provider_started",
    ):
        if type(safe_payload.get(name)) is not bool:
            raise ValueError(f"{name} must be bool")


def _validate_common(
    run_id: str,
    trace_id: str,
    event_type: RuntimeEventType,
    component: str,
    payload: RuntimeEventPayload,
    step_id: str | None,
    step_sequence: int | None,
) -> None:
    _require_text(run_id, "run_id")
    _require_text(trace_id, "trace_id")
    _require_text(component, "component")
    if not isinstance(event_type, RuntimeEventType):
        raise TypeError("event_type 必须是 RuntimeEventType")
    expected = _PAYLOAD_TYPES[event_type]
    if not isinstance(payload, expected):
        raise TypeError(
            f"{event_type.value} 必须使用 {expected.__name__}，"
            f"不能使用 {type(payload).__name__}"
        )
    if step_id is None:
        if step_sequence is not None:
            raise ValueError("没有 step_id 时不能设置 step_sequence")
    else:
        _require_text(step_id, "step_id")
        if (
            isinstance(step_sequence, bool)
            or not isinstance(step_sequence, int)
            or step_sequence <= 0
        ):
            raise ValueError("Step Event 必须使用正整数 step_sequence")
