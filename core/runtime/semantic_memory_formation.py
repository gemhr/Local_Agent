#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WP2-B canonical post-delivery Semantic Memory Formation component。

固定 ordering（WP2-A Decision A）：

    OutputGate DELIVERED
    -> RunFinalMemoryWriter.write_delivered（canonical conversation exchange）
    -> committed exchange receipt
    -> 本 component（independent, bounded, awaited）
    -> safe typed Formation result
    -> 既有 Step / Run completion

职责边界：
- 本 component 只拥有 eligibility、LLM candidate extraction、strict parsing、
  Should-Remember code validation、normalization、authoritative record
  preparation、经 ``AdvancedMemoryStore.create`` 的持久化与 typed outcome；
- 不承担 Conflict Resolution、Supersede、Forget、Retrieval、Context Injection
  （WP3/WP4 范围）；
- Formation failure 永远不改变 delivered output、final Step status 或 Run
  terminal；结果只通过安全 typed result / observation 表达。

输入 allowlist：original user query（唯一事实 authority）、delivered final
answer（仅辅助 normalization）、committed exchange receipt 与真实 run/entry
agent/direct scope identity。禁止 CoT、provider data、tool/RAG 正文、
system/developer instruction 进入 candidate source。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
import math
import re
import threading
import time
from typing import Any, List, Mapping, Optional, Sequence, Tuple
from uuid import uuid4

from core.advanced_memory import (
    AdvancedMemoryStore,
    MemoryDomainError,
    MemoryErrorCode,
    MemoryOrigin,
    MemoryStatus,
    MemoryType,
    SemanticMemoryRecord,
)
from core.runtime.cancellation import RunCancelledError
from core.runtime.context import RunDeadlineExceededError
from core.runtime.events import (
    MemoryFormationCompletedPayload,
    RuntimeEventType,
)
from core.runtime.final_memory_writer import CommittedExchangeReceipt
from core.runtime.trace_contract import set_span_attributes
from core.runtime.tracing import current_trace_context, start_span_safely

# ---------------------------------------------------------------------------
# 冻结 vocabulary 与实现常量（非公共配置系统）
# ---------------------------------------------------------------------------

FORMATION_SCHEMA_VERSION = 1
FORMATION_METHOD_HYBRID = "HYBRID"
FORMATION_ORIGIN_TYPE = "DELIVERED_EXCHANGE"
FORMATION_MEMORY_SCOPE = "direct"

#: bounded candidate batch 上限（代码常量，不是配置系统）。
FORMATION_MAX_CANDIDATES = 8
FORMATION_MAX_CANONICAL_TEXT_CHARS = 400
FORMATION_MAX_LOGICAL_KEY_CHARS = 100
FORMATION_MAX_SOURCE_EXCERPT_CHARS = 400
FORMATION_MAX_REASON_CODE_CHARS = 80
FORMATION_MAX_RAW_OUTPUT_CHARS = 65_536
FORMATION_MAX_CANDIDATE_OUTCOMES_CHARS = 2_048

#: 整个 Formation operation 的 bounded timeout（秒）。
FORMATION_TIMEOUT_SECONDS = 30.0

#: 同一 prepared record 的持久化额外重试次数（same-execution idempotency）。
FORMATION_PERSISTENCE_RETRY_LIMIT = 2

# Internal RC extension operation。Trace Contract v1 仍只冻结原有六个公共
# operation；该 Formation span 不进入 consumer-neutral trace export contract。
_SEMANTIC_FORMATION_SPAN_OPERATION = "memory.formation"

_LOGICAL_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
_SAFE_REASON_PATTERN = re.compile(r"^[A-Z0-9_]{1,40}$")
_OBVIOUS_SMALL_TALK_PATTERN = re.compile(
    r"^(?:你好|您好|嗨|hi|hello|谢谢|感谢|多谢|再见)[!！,.，。\s]*$",
    re.IGNORECASE,
)
_LONG_TERM_CUE_PATTERN = re.compile(
    r"(?:以后|今后|长期|一直|统一|默认|从现在起|始终|永久|已经改成|改成|换成)"
)
_TRANSIENT_CUE_PATTERN = re.compile(
    r"(?:今天|这次|本次|本轮|这一轮|先临时|暂时|临时|一次性)"
)
_UNCERTAIN_CUE_PATTERN = re.compile(r"(?:可能|也许|大概|或许|猜测|不确定)")
_EXTERNAL_SOURCE_CUE_PATTERN = re.compile(
    r"(?:工具(?:返回|结果|显示)|RAG|检索(?:结果|显示)|知识库|"
    r"文档(?:说|显示|指出)|第三方|别人说|有人说|说过)"
)


class SemanticFormationStatus(str, Enum):
    """Formation 执行的整体 typed status（零 accepted 也是正常 SUCCEEDED）。"""

    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


class SemanticFormationErrorCode(str, Enum):
    """安全错误码；不含 provider/SQL/路径/正文。"""

    MODEL_FAILED = "FORMATION_MODEL_FAILED"
    OUTPUT_INVALID = "FORMATION_OUTPUT_INVALID"
    OUTPUT_UNKNOWN_FIELD = "FORMATION_OUTPUT_UNKNOWN_FIELD"
    OUTPUT_FORBIDDEN_FIELD = "FORMATION_OUTPUT_FORBIDDEN_FIELD"
    BATCH_TOO_LARGE = "FORMATION_BATCH_TOO_LARGE"
    DUPLICATE_EXECUTION = "FORMATION_DUPLICATE_EXECUTION"
    IDENTITY_INVALID = "FORMATION_IDENTITY_INVALID"
    PERSISTENCE_FAILED = "FORMATION_PERSISTENCE_FAILED"
    TIMED_OUT = "FORMATION_TIMED_OUT"
    CANCELLED = "FORMATION_CANCELLED"
    INTERNAL_ERROR = "FORMATION_INTERNAL_ERROR"


class FormationCandidateOutcomeCode(str, Enum):
    """per-candidate 安全 outcome（batch-local ordinal 关联）。"""

    PERSISTED = "PERSISTED"
    REUSED = "REUSED"
    IGNORED_POLICY = "IGNORED_POLICY"
    IGNORED_INVALID = "IGNORED_INVALID"
    PERSIST_FAILED = "PERSIST_FAILED"


class FormationCandidateCategory(str, Enum):
    """WP2 冻结的小型 category allowlist；不建 ontology/taxonomy。"""

    STABLE_USER_PREFERENCE = "STABLE_USER_PREFERENCE"
    PROJECT_STABLE_FACT = "PROJECT_STABLE_FACT"
    ENGINEERING_CONSTRAINT = "ENGINEERING_CONSTRAINT"
    LONG_TERM_DECISION = "LONG_TERM_DECISION"


@dataclass(slots=True)
class _FormationTimings:
    extraction_ms: int = 0
    persistence_ms: int = 0


class _PersistenceInterrupted(asyncio.CancelledError):
    """单条持久化已到安全边界后传播 cancellation/timeout。"""

    def __init__(self, outcome: "FormationCandidateOutcome") -> None:
        self.outcome = outcome
        super().__init__()


class SemanticFormationError(RuntimeError):
    """typed Formation 失败；只携带安全 error code。"""

    def __init__(self, error_code: SemanticFormationErrorCode) -> None:
        self.error_code = error_code
        super().__init__(f"semantic memory formation failed ({error_code.value})")


# ---------------------------------------------------------------------------
# Candidate proposal contract（Model 只能提议以下字段）
# ---------------------------------------------------------------------------

_CANDIDATE_REQUIRED_FIELDS = frozenset(
    {"disposition", "category", "canonical_text", "value", "source_excerpt"}
)
_CANDIDATE_OPTIONAL_FIELDS = frozenset({"logical_key", "reason_code"})
_CANDIDATE_ALLOWED_FIELDS = _CANDIDATE_REQUIRED_FIELDS | _CANDIDATE_OPTIONAL_FIELDS

#: Model 无权声明的 authoritative 字段；出现即整体 fail closed。
_FORBIDDEN_CANDIDATE_FIELDS = frozenset(
    {
        "memory_id",
        "memory_type",
        "status",
        "memory_status",
        "agent_id",
        "memory_scope",
        "origin",
        "origin_type",
        "origin_run_id",
        "origin_exchange_id",
        "origin_agent_id",
        "origin_memory_scope",
        "created_at",
        "updated_at",
        "timestamps",
        "formation_method",
        "supersede",
        "superseded_by",
        "superseded_by_memory_id",
        "forget",
        "sql",
        "query",
    }
)


@dataclass(frozen=True, slots=True)
class FormationProposal:
    """strict parser 产出的一条 model proposal（未经验证）。"""

    ordinal: int
    disposition: str
    category: str
    canonical_text: Any
    value: Any
    source_excerpt: Any
    logical_key: Any = None
    reason_code: Any = None


# ---------------------------------------------------------------------------
# Safe typed result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FormationCandidateOutcome:
    """per-candidate 安全 outcome；memory_id 仅 accepted/reused 时出现。"""

    ordinal: int
    outcome: FormationCandidateOutcomeCode
    safe_reason_code: str
    memory_id: Optional[str] = None

    def encode(self) -> str:
        memory_id = self.memory_id or ""
        return f"{self.ordinal}|{self.outcome.value}|{self.safe_reason_code}|{memory_id}"


@dataclass(frozen=True, slots=True)
class SemanticFormationResult:
    """content-minimized typed Formation 结果；不携带任何正文/payload/quote。"""

    run_id: str
    exchange_id: str
    agent_id: str
    memory_scope: str
    formation_method: str
    status: SemanticFormationStatus
    schema_version: int
    proposed_count: int
    accepted_count: int
    ignored_count: int
    persisted_count: int
    reused_count: int
    failed_count: int
    candidate_outcomes: Tuple[FormationCandidateOutcome, ...]
    formation_total_duration_ms: int
    model_extraction_duration_ms: int
    persistence_duration_ms: int
    safe_error_code: Optional[str] = None

    def candidate_outcomes_encoded(self) -> str:
        if not self.candidate_outcomes:
            return "NONE"
        return ";".join(outcome.encode() for outcome in self.candidate_outcomes)


# ---------------------------------------------------------------------------
# Strict parser（fail closed on unknown / forbidden / oversized）
# ---------------------------------------------------------------------------


class StrictFormationProposalParser:
    """严格 v1 JSON parser。

    整体 fail closed（typed FAILED、零写入、不重试）：
    - 非法 JSON / 顶层形状错误 / schema_version 不符 / 超长输出；
    - candidate 携带 unknown 字段；
    - candidate 携带 forbidden authoritative 字段；
    - candidate batch 超过 bounded 上限。

    单条 candidate 的字段级问题（类型、枚举值、payload 形状等）不 fail 整个
    batch，由 code-owned validation 标记该 candidate 为 IGNORE_INVALID。
    """

    @classmethod
    def parse(cls, raw_output: str) -> Tuple[FormationProposal, ...]:
        if not isinstance(raw_output, str):
            raise SemanticFormationError(
                SemanticFormationErrorCode.OUTPUT_INVALID
            )
        if len(raw_output) > FORMATION_MAX_RAW_OUTPUT_CHARS:
            raise SemanticFormationError(
                SemanticFormationErrorCode.OUTPUT_INVALID
            )
        try:
            payload = json.loads(raw_output)
        except (ValueError, RecursionError):
            raise SemanticFormationError(
                SemanticFormationErrorCode.OUTPUT_INVALID
            ) from None
        if not isinstance(payload, Mapping):
            raise SemanticFormationError(
                SemanticFormationErrorCode.OUTPUT_INVALID
            )
        if set(payload) != {"schema_version", "candidates"}:
            raise SemanticFormationError(
                SemanticFormationErrorCode.OUTPUT_INVALID
            )
        if payload["schema_version"] != FORMATION_SCHEMA_VERSION:
            raise SemanticFormationError(
                SemanticFormationErrorCode.OUTPUT_INVALID
            )
        candidates = payload["candidates"]
        if not isinstance(candidates, list):
            raise SemanticFormationError(
                SemanticFormationErrorCode.OUTPUT_INVALID
            )
        if len(candidates) > FORMATION_MAX_CANDIDATES:
            raise SemanticFormationError(
                SemanticFormationErrorCode.BATCH_TOO_LARGE
            )
        proposals: List[FormationProposal] = []
        for ordinal, item in enumerate(candidates):
            proposals.append(cls._parse_candidate(ordinal, item))
        return tuple(proposals)

    @classmethod
    def _parse_candidate(
        cls, ordinal: int, item: object
    ) -> FormationProposal:
        if not isinstance(item, Mapping):
            # 单条 candidate 形状错误：按 candidate invalid 处理，不废整个 batch。
            return FormationProposal(
                ordinal=ordinal,
                disposition="",
                category="",
                canonical_text=None,
                value=None,
                source_excerpt=None,
            )
        keys = {key for key in item if isinstance(key, str)}
        if keys & _FORBIDDEN_CANDIDATE_FIELDS:
            raise SemanticFormationError(
                SemanticFormationErrorCode.OUTPUT_FORBIDDEN_FIELD
            )
        if not keys <= _CANDIDATE_ALLOWED_FIELDS:
            raise SemanticFormationError(
                SemanticFormationErrorCode.OUTPUT_UNKNOWN_FIELD
            )
        if not _CANDIDATE_REQUIRED_FIELDS <= keys:
            raise SemanticFormationError(
                SemanticFormationErrorCode.OUTPUT_INVALID
            )
        return FormationProposal(
            ordinal=ordinal,
            disposition=item["disposition"],
            category=item["category"],
            canonical_text=item["canonical_text"],
            value=item["value"],
            source_excerpt=item["source_excerpt"],
            logical_key=item.get("logical_key"),
            reason_code=item.get("reason_code"),
        )


# ---------------------------------------------------------------------------
# Code-owned candidate validation（Should-Remember deterministic gate）
# ---------------------------------------------------------------------------


def _normalize_for_grounding(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold()).strip()


def _sanitize_reason(raw: object) -> str:
    if isinstance(raw, str) and _SAFE_REASON_PATTERN.fullmatch(raw.strip()):
        return raw.strip()
    return "POLICY_IGNORED"


def _is_obviously_non_persistent_source(user_query: str) -> bool:
    """Pre-model deterministic gate for only obvious non-persistent input.

    This intentionally stays narrow: mixed or ambiguous natural language still
    reaches the extractor, while exact small talk and a purely transient request
    cannot spend a model call or be upgraded by a malicious REMEMBER proposal.
    """
    stripped = user_query.strip()
    if _OBVIOUS_SMALL_TALK_PATTERN.fullmatch(stripped):
        return True
    return bool(
        _TRANSIENT_CUE_PATTERN.search(stripped)
        and not _LONG_TERM_CUE_PATTERN.search(stripped)
    )


@dataclass(frozen=True, slots=True)
class CandidateValidation:
    """code-owned validation 结论；Model 只提议，LocalAgent 最终决定。"""

    accepted: bool
    policy_ignored: bool = False
    canonical_text: Optional[str] = None
    value: Any = None
    logical_key: Optional[str] = None
    reason_code: str = "CANDIDATE_INVALID"


def validate_candidate(
    proposal: FormationProposal,
    *,
    user_query: str,
) -> Optional[CandidateValidation]:
    """code-owned validation；返回 None 表示 candidate invalid。

    Model 的 REMEMBER 只是 proposal；category allowlist、payload 形状、
    logical key 规则、字段长度与 source grounding 全部由本函数（LocalAgent
    code）决定 ACCEPT / IGNORE。
    """
    if not isinstance(proposal.disposition, str):
        return None
    if proposal.disposition == "IGNORE":
        # policy ignore：正常业务结果（例如 small talk / transient statement）。
        return CandidateValidation(
            accepted=False,
            policy_ignored=True,
            reason_code=_sanitize_reason(proposal.reason_code),
        )
    if proposal.disposition != "REMEMBER":
        return None
    # category allowlist
    if not isinstance(proposal.category, str):
        return None
    try:
        FormationCandidateCategory(proposal.category)
    except ValueError:
        return None
    # canonical_text：非空、bounded、单一 atomic fact 陈述
    if (
        not isinstance(proposal.canonical_text, str)
        or not proposal.canonical_text.strip()
        or len(proposal.canonical_text) > FORMATION_MAX_CANONICAL_TEXT_CHARS
    ):
        return None
    # value：仅 string / number / boolean；禁止 null、list、嵌套 object
    value = proposal.value
    if isinstance(value, bool):
        pass
    elif isinstance(value, (int, float)):
        if not math.isfinite(value):
            return None
    elif isinstance(value, str):
        if not value.strip() or len(value) > FORMATION_MAX_CANONICAL_TEXT_CHARS:
            return None
    else:
        return None
    # logical_key：optional 小写 dotted token
    logical_key = proposal.logical_key
    if logical_key is not None:
        if (
            not isinstance(logical_key, str)
            or len(logical_key) > FORMATION_MAX_LOGICAL_KEY_CHARS
            or not _LOGICAL_KEY_PATTERN.fullmatch(logical_key)
        ):
            return None
    # source grounding：excerpt 必须能在 original user query 中找到
    if (
        not isinstance(proposal.source_excerpt, str)
        or not proposal.source_excerpt.strip()
        or len(proposal.source_excerpt) > FORMATION_MAX_SOURCE_EXCERPT_CHARS
    ):
        return None
    normalized_query = _normalize_for_grounding(user_query)
    normalized_excerpt = _normalize_for_grounding(proposal.source_excerpt)
    if len(normalized_excerpt) < 2 or normalized_excerpt not in normalized_query:
        return None
    # Grounding proves provenance, not semantic eligibility. LocalAgent code
    # must still reject obvious transient/speculative/external-only evidence even
    # when the Model labels it REMEMBER. A long-term/adoption cue can coexist
    # with transient/external context, but uncertainty always fails closed.
    if _UNCERTAIN_CUE_PATTERN.search(proposal.source_excerpt):
        return None
    if (
        _TRANSIENT_CUE_PATTERN.search(proposal.source_excerpt)
        and not _LONG_TERM_CUE_PATTERN.search(proposal.source_excerpt)
    ):
        return None
    if (
        _EXTERNAL_SOURCE_CUE_PATTERN.search(proposal.source_excerpt)
        and not _LONG_TERM_CUE_PATTERN.search(proposal.source_excerpt)
    ):
        return None
    return CandidateValidation(
        accepted=True,
        canonical_text=proposal.canonical_text.strip(),
        value=value,
        logical_key=logical_key,
        reason_code="ACCEPTED",
    )


# ---------------------------------------------------------------------------
# Extraction model（复用统一 Model Invocation 的窄 adapter）
# ---------------------------------------------------------------------------


class FormationExtractionModel:
    """同步窄 seam：真实实现经 AgentRouter 复用统一 Model Invocation。"""

    def extract(self, user_query: str, final_answer: str) -> str:
        raise NotImplementedError


class UnifiedFormationExtractionAdapter(FormationExtractionModel):
    """把 ``AgentRouter.complete_memory_formation_decision`` 适配为 extraction seam。

    retry / fallback / circuit / budget / cancellation 全部留在既有统一
    Model Invocation 内；本 adapter 不新建 provider client 或第二套 retry。
    """

    def __init__(self, router, *, run_context, event_emitter=None, fault_controller=None) -> None:
        self._router = router
        self._run_context = run_context
        self._event_emitter = event_emitter
        self._fault_controller = fault_controller

    def extract(self, user_query: str, final_answer: str) -> str:
        return self._router.complete_memory_formation_decision(
            user_query,
            final_answer,
            run_context=self._run_context,
            event_emitter=self._event_emitter,
            fault_controller=self._fault_controller,
        )


# ---------------------------------------------------------------------------
# Formation component
# ---------------------------------------------------------------------------


class SemanticMemoryFormation:
    """run-scoped post-delivery Semantic Memory Formation owner。

    - 只在 canonical hook（final Step + DELIVERED + committed exchange
      receipt + persist）被调用后执行；
    - 同一 ``(run_id, exchange_id)`` 只执行一次（run-scoped write-once
      guard，非跨进程 durable dedup）；
    - accepted candidate 一次性生成 immutable ``SemanticMemoryRecord``（含
      LocalAgent 生成的 memory_id 与 timestamps），持久化重试复用同一 record；
    - 每条 Memory 独立 transaction（经 ``AdvancedMemoryStore.create``），
      与 conversation exchange 不共享 transaction；
    - observation 为 journal-first typed event + best-effort span/metrics。
    """

    def __init__(
        self,
        *,
        entry_agent_id: str,
        user_request: str,
        memory_store: AdvancedMemoryStore,
        extraction_model: FormationExtractionModel,
        run_id: Optional[str] = None,
        memory_scope: str = FORMATION_MEMORY_SCOPE,
        span_recorder=None,
        metrics_recorder=None,
        event_emitter=None,
    ) -> None:
        if not isinstance(entry_agent_id, str) or not entry_agent_id.strip():
            raise ValueError("entry_agent_id 不能为空")
        if not isinstance(user_request, str) or not user_request.strip():
            raise ValueError("user_request 不能为空")
        if not callable(getattr(memory_store, "create", None)):
            raise TypeError("memory_store 必须实现 create（AdvancedMemoryStore 窄边界）")
        if not isinstance(extraction_model, FormationExtractionModel):
            raise TypeError("extraction_model 必须实现 extract")
        if run_id is not None and (
            not isinstance(run_id, str) or not run_id.strip()
        ):
            raise ValueError("run_id 必须是非空字符串")
        if not isinstance(memory_scope, str) or not memory_scope.strip():
            raise ValueError("memory_scope 不能为空")
        self._entry_agent_id = entry_agent_id.strip()
        self._user_request = user_request
        self._memory_store = memory_store
        self._extraction_model = extraction_model
        self._run_id = run_id
        self._memory_scope = memory_scope
        self._span_recorder = span_recorder
        self._metrics_recorder = metrics_recorder
        self._event_emitter = event_emitter
        self._guard_lock = threading.Lock()
        self._executed_keys: set[Tuple[str, str]] = set()
        self._memoized_results: dict[Tuple[str, str], SemanticFormationResult] = {}

    @property
    def entry_agent_id(self) -> str:
        return self._entry_agent_id

    async def run_formation(
        self,
        *,
        receipt: CommittedExchangeReceipt,
        final_step_id: str,
        store,
    ) -> SemanticFormationResult:
        """Awaited、bounded 的 Formation 入口；永不向 hook 抛业务异常。"""
        total_started = time.monotonic()
        guarded = self._begin_execution(receipt)
        if guarded is not None:
            await self._observe(receipt, guarded)
            return guarded
        outcomes: List[FormationCandidateOutcome] = []
        timings = _FormationTimings()
        try:
            result = await asyncio.wait_for(
                self._execute(
                    receipt=receipt,
                    final_step_id=final_step_id,
                    store=store,
                    outcomes=outcomes,
                    timings=timings,
                    total_started=total_started,
                ),
                timeout=FORMATION_TIMEOUT_SECONDS,
            )
            await self._observe(receipt, result)
            return result
        except asyncio.CancelledError:
            # Formation 吞掉取消并返回 typed CANCELLED，保证 cancellation
            # 不改变 delivered output / final Step status / Run terminal。
            result = self._stopped_result(
                receipt,
                status=(
                    SemanticFormationStatus.PARTIAL
                    if self._persisted_or_reused(outcomes) > 0
                    else SemanticFormationStatus.CANCELLED
                ),
                error_code=SemanticFormationErrorCode.CANCELLED,
                outcomes=outcomes,
                extraction_ms=timings.extraction_ms,
                persistence_ms=timings.persistence_ms,
                total_started=total_started,
            )
            await self._observe(receipt, result)
            return result
        except TimeoutError:
            result = self._stopped_result(
                receipt,
                status=(
                    SemanticFormationStatus.PARTIAL
                    if self._persisted_or_reused(outcomes) > 0
                    else SemanticFormationStatus.TIMED_OUT
                ),
                error_code=SemanticFormationErrorCode.TIMED_OUT,
                outcomes=outcomes,
                extraction_ms=timings.extraction_ms,
                persistence_ms=timings.persistence_ms,
                total_started=total_started,
            )
            await self._observe(receipt, result)
            return result
        except SemanticFormationError as exc:
            result = self._stopped_result(
                receipt,
                status=SemanticFormationStatus.FAILED,
                error_code=exc.error_code,
                outcomes=outcomes,
                extraction_ms=timings.extraction_ms,
                persistence_ms=timings.persistence_ms,
                total_started=total_started,
            )
            await self._observe(receipt, result)
            return result
        except MemoryDomainError:
            result = self._stopped_result(
                receipt,
                status=SemanticFormationStatus.FAILED,
                error_code=SemanticFormationErrorCode.INTERNAL_ERROR,
                outcomes=outcomes,
                extraction_ms=timings.extraction_ms,
                persistence_ms=timings.persistence_ms,
                total_started=total_started,
            )
            await self._observe(receipt, result)
            return result
        except Exception:
            result = self._stopped_result(
                receipt,
                status=SemanticFormationStatus.FAILED,
                error_code=SemanticFormationErrorCode.INTERNAL_ERROR,
                outcomes=outcomes,
                extraction_ms=timings.extraction_ms,
                persistence_ms=timings.persistence_ms,
                total_started=total_started,
            )
            await self._observe(receipt, result)
            return result

    # -- guard / eligibility ------------------------------------------------

    def _begin_execution(
        self, receipt: CommittedExchangeReceipt
    ) -> Optional[SemanticFormationResult]:
        """run-scoped write-once guard + deterministic eligibility。"""
        if not isinstance(receipt, CommittedExchangeReceipt):
            return self._immediate_failure(
                receipt,
                SemanticFormationErrorCode.IDENTITY_INVALID,
            )
        key = (receipt.run_id or "", receipt.exchange_id)
        with self._guard_lock:
            if key in self._executed_keys:
                memoized = self._memoized_results.get(key)
                if memoized is not None:
                    return memoized
                return self._immediate_failure(
                    receipt,
                    SemanticFormationErrorCode.DUPLICATE_EXECUTION,
                )
            self._executed_keys.add(key)
        # eligibility：真实 committed identity + canonical direct scope
        if not receipt.run_id or not receipt.exchange_id:
            return self._immediate_failure(
                receipt, SemanticFormationErrorCode.IDENTITY_INVALID
            )
        if self._run_id is not None and receipt.run_id != self._run_id:
            return self._immediate_failure(
                receipt, SemanticFormationErrorCode.IDENTITY_INVALID
            )
        if (
            receipt.entry_agent_id != self._entry_agent_id
            or receipt.memory_scope != FORMATION_MEMORY_SCOPE
            or self._memory_scope != FORMATION_MEMORY_SCOPE
        ):
            return self._immediate_failure(
                receipt, SemanticFormationErrorCode.IDENTITY_INVALID
            )
        if _is_obviously_non_persistent_source(self._user_request):
            return self._build_result(
                run_id=receipt.run_id,
                exchange_id=receipt.exchange_id,
                status=SemanticFormationStatus.SUCCEEDED,
                error_code=None,
                outcomes=(),
                proposed=0,
                accepted=0,
                extraction_ms=0,
                persistence_ms=0,
                total_duration_ms=0,
            )
        return None

    def _immediate_failure(
        self,
        receipt: CommittedExchangeReceipt,
        error_code: SemanticFormationErrorCode,
    ) -> SemanticFormationResult:
        return self._build_result(
            run_id=getattr(receipt, "run_id", None) or "unknown",
            exchange_id=getattr(receipt, "exchange_id", "unknown"),
            status=SemanticFormationStatus.FAILED,
            error_code=error_code,
            outcomes=(),
            proposed=0,
            accepted=0,
            extraction_ms=0,
            persistence_ms=0,
            total_duration_ms=0,
        )

    # -- core execution -----------------------------------------------------

    async def _execute(
        self,
        *,
        receipt: CommittedExchangeReceipt,
        final_step_id: str,
        store,
        outcomes: List[FormationCandidateOutcome],
        timings: _FormationTimings,
        total_started: float,
    ) -> SemanticFormationResult:
        final_answer = store.read_final_content(final_step_id)
        # ---- extraction（单一一次；重试只属于统一 Model Invocation） ----
        extraction_started = time.monotonic()
        try:
            raw_output, interrupted = await self._await_blocking_safe_boundary(
                self._extraction_model.extract,
                self._user_request,
                final_answer,
            )
            timings.extraction_ms = max(
                0, int((time.monotonic() - extraction_started) * 1000)
            )
            if interrupted:
                raise asyncio.CancelledError()
            if isinstance(raw_output, BaseException):
                raise raw_output
        except asyncio.CancelledError:
            raise
        except RunDeadlineExceededError:
            # Run deadline 到达：复用外层 TIMED_OUT 收口路径。
            raise TimeoutError("formation extraction deadline exceeded") from None
        except RunCancelledError:
            # 取消发生在 extraction 阶段：typed CANCELLED、零写入。
            raise asyncio.CancelledError() from None
        except SemanticFormationError:
            raise
        except Exception:
            # model extraction final failure：只依赖统一 invocation 自身
            # retry/fallback，外层不做第二次 extraction。
            raise SemanticFormationError(
                SemanticFormationErrorCode.MODEL_FAILED
            ) from None
        timings.extraction_ms = max(
            0, int((time.monotonic() - extraction_started) * 1000)
        )
        # ---- strict parse（整体 fail closed） ----
        proposals = StrictFormationProposalParser.parse(raw_output)
        # ---- validation + normalization + preparation + persistence ----
        accepted = 0
        for proposal in proposals:
            validated = validate_candidate(proposal, user_query=self._user_request)
            if validated is None:
                outcomes.append(
                    FormationCandidateOutcome(
                        proposal.ordinal,
                        FormationCandidateOutcomeCode.IGNORED_INVALID,
                        "CANDIDATE_INVALID",
                        None,
                    )
                )
                continue
            if not validated.accepted:
                outcomes.append(
                    FormationCandidateOutcome(
                        proposal.ordinal,
                        FormationCandidateOutcomeCode.IGNORED_POLICY,
                        validated.reason_code,
                        None,
                    )
                )
                continue
            accepted += 1
            record = self._prepare_record(
                receipt=receipt,
                canonical_text=validated.canonical_text,
                value=validated.value,
                logical_key=validated.logical_key,
            )
            persistence_started = time.monotonic()
            try:
                outcome = await self._persist_record(record, proposal.ordinal)
            except _PersistenceInterrupted as exc:
                # cancellation/timeout 到达时仍等待当前单条 transaction 完成或
                # rollback，并记录真实 outcome；随后停止剩余 candidates。
                timings.persistence_ms += max(
                    0, int((time.monotonic() - persistence_started) * 1000)
                )
                outcomes.append(exc.outcome)
                raise asyncio.CancelledError() from None
            timings.persistence_ms += max(
                0, int((time.monotonic() - persistence_started) * 1000)
            )
            outcomes.append(outcome)
        return self._build_result(
            run_id=receipt.run_id or "unknown",
            exchange_id=receipt.exchange_id,
            status=None,
            error_code=None,
            outcomes=tuple(outcomes),
            proposed=len(proposals),
            accepted=accepted,
            extraction_ms=timings.extraction_ms,
            persistence_ms=timings.persistence_ms,
            total_duration_ms=max(
                0, int((time.monotonic() - total_started) * 1000)
            ),
        )

    def _prepare_record(
        self,
        *,
        receipt: CommittedExchangeReceipt,
        canonical_text: str,
        value: Any,
        logical_key: Optional[str],
    ) -> SemanticMemoryRecord:
        """一次性构造完整 immutable record；重试必须复用同一对象。"""
        now = datetime.now(UTC)
        return SemanticMemoryRecord(
            memory_id="mem-" + uuid4().hex,
            agent_id=self._entry_agent_id,
            memory_scope=self._memory_scope,
            canonical_text=canonical_text,
            payload={"value": value},
            origin=MemoryOrigin(
                origin_type=FORMATION_ORIGIN_TYPE,
                origin_run_id=receipt.run_id or "",
                origin_exchange_id=receipt.exchange_id,
                origin_agent_id=receipt.entry_agent_id,
                origin_memory_scope=receipt.memory_scope,
                formation_method=FORMATION_METHOD_HYBRID,
            ),
            memory_type=MemoryType.SEMANTIC,
            status=MemoryStatus.ACTIVE,
            logical_key=logical_key,
            created_at=now,
            updated_at=now,
        )

    async def _persist_record(
        self, record: SemanticMemoryRecord, ordinal: int
    ) -> FormationCandidateOutcome:
        """bounded retry：仅 retryable persistence failure，且复用同一 record。"""
        attempts_left = 1 + FORMATION_PERSISTENCE_RETRY_LIMIT
        while True:
            completion, interrupted = await self._await_blocking_safe_boundary(
                self._memory_store.create, record
            )
            if isinstance(completion, MemoryDomainError):
                exc = completion
                retryable = exc.error_code == MemoryErrorCode.PERSISTENCE_FAILED
                if retryable and attempts_left > 1 and not interrupted:
                    attempts_left -= 1
                    continue
                outcome = FormationCandidateOutcome(
                    ordinal,
                    FormationCandidateOutcomeCode.PERSIST_FAILED,
                    (
                        "MEMORY_PERSISTENCE_FAILED"
                        if retryable
                        else "MEMORY_DUPLICATE_CONFLICT"
                        if exc.error_code == MemoryErrorCode.DUPLICATE_CONFLICT
                        else "MEMORY_CREATE_REJECTED"
                    ),
                    None,
                )
            elif isinstance(completion, BaseException):
                outcome = FormationCandidateOutcome(
                    ordinal,
                    FormationCandidateOutcomeCode.PERSIST_FAILED,
                    "MEMORY_CREATE_REJECTED",
                    None,
                )
            elif completion is not record:
                # complete-record idempotency：第一次已提交但 caller 未确认。
                outcome = FormationCandidateOutcome(
                    ordinal,
                    FormationCandidateOutcomeCode.REUSED,
                    "IDEMPOTENT_REUSE",
                    completion.memory_id,
                )
            else:
                outcome = FormationCandidateOutcome(
                    ordinal,
                    FormationCandidateOutcomeCode.PERSISTED,
                    "OK",
                    record.memory_id,
                )
            if interrupted:
                raise _PersistenceInterrupted(outcome)
            return outcome

    @staticmethod
    async def _await_blocking_safe_boundary(function, *args):
        """等待同步调用到安全边界，并延迟传播 task cancellation。

        ``asyncio.to_thread`` 本身不能停止已开始的 SQLite/model 调用。这里用
        shield 保留 event-loop-owned worker，并在 cancellation/timeout 后继续
        等到该次有界调用完成，避免 Formation 返回时仍有未知 transaction。
        """

        def invoke():
            try:
                return function(*args)
            except BaseException as exc:  # 只在调用线程内搬运，随后按 typed 边界处理。
                return exc

        worker = asyncio.create_task(asyncio.to_thread(invoke))
        interrupted = False
        while True:
            try:
                return await asyncio.shield(worker), interrupted
            except asyncio.CancelledError:
                interrupted = True

    # -- result assembly ----------------------------------------------------

    @staticmethod
    def _persisted_or_reused(outcomes: Sequence[FormationCandidateOutcome]) -> int:
        return sum(
            1
            for outcome in outcomes
            if outcome.outcome
            in {
                FormationCandidateOutcomeCode.PERSISTED,
                FormationCandidateOutcomeCode.REUSED,
            }
        )

    def _stopped_result(
        self,
        receipt: CommittedExchangeReceipt,
        *,
        status: SemanticFormationStatus,
        error_code: SemanticFormationErrorCode,
        outcomes: Sequence[FormationCandidateOutcome],
        extraction_ms: int,
        persistence_ms: int,
        total_started: float,
    ) -> SemanticFormationResult:
        return self._build_result(
            run_id=receipt.run_id or "unknown",
            exchange_id=receipt.exchange_id,
            status=status,
            error_code=error_code,
            outcomes=tuple(outcomes),
            proposed=len(outcomes),
            accepted=sum(
                1
                for outcome in outcomes
                if outcome.outcome
                not in {
                    FormationCandidateOutcomeCode.IGNORED_POLICY,
                    FormationCandidateOutcomeCode.IGNORED_INVALID,
                }
            ),
            extraction_ms=extraction_ms,
            persistence_ms=persistence_ms,
            total_duration_ms=max(
                0, int((time.monotonic() - total_started) * 1000)
            ),
        )

    def _build_result(
        self,
        *,
        run_id: str,
        exchange_id: str,
        status: Optional[SemanticFormationStatus],
        error_code: Optional[SemanticFormationErrorCode],
        outcomes: Tuple[FormationCandidateOutcome, ...],
        proposed: int,
        accepted: int,
        extraction_ms: int,
        persistence_ms: int,
        total_duration_ms: int,
    ) -> SemanticFormationResult:
        ignored = sum(
            1
            for outcome in outcomes
            if outcome.outcome
            in {
                FormationCandidateOutcomeCode.IGNORED_POLICY,
                FormationCandidateOutcomeCode.IGNORED_INVALID,
            }
        )
        persisted = sum(
            1
            for outcome in outcomes
            if outcome.outcome is FormationCandidateOutcomeCode.PERSISTED
        )
        reused = sum(
            1
            for outcome in outcomes
            if outcome.outcome is FormationCandidateOutcomeCode.REUSED
        )
        failed = sum(
            1
            for outcome in outcomes
            if outcome.outcome is FormationCandidateOutcomeCode.PERSIST_FAILED
        )
        if status is None:
            if failed > 0:
                if persisted + reused > 0:
                    status = SemanticFormationStatus.PARTIAL
                    error_code = SemanticFormationErrorCode.PERSISTENCE_FAILED
                else:
                    status = SemanticFormationStatus.FAILED
                    error_code = SemanticFormationErrorCode.PERSISTENCE_FAILED
            else:
                # 零 accepted（全部 policy ignore）也是正常 SUCCEEDED。
                status = SemanticFormationStatus.SUCCEEDED
        encoded = ";".join(outcome.encode() for outcome in outcomes) or "NONE"
        if len(encoded) > FORMATION_MAX_CANDIDATE_OUTCOMES_CHARS:
            encoded = encoded[:FORMATION_MAX_CANDIDATE_OUTCOMES_CHARS]
        result = SemanticFormationResult(
            run_id=run_id,
            exchange_id=exchange_id,
            agent_id=self._entry_agent_id,
            memory_scope=self._memory_scope,
            formation_method=FORMATION_METHOD_HYBRID,
            status=status,
            schema_version=FORMATION_SCHEMA_VERSION,
            proposed_count=proposed,
            accepted_count=accepted,
            ignored_count=ignored,
            persisted_count=persisted,
            reused_count=reused,
            failed_count=failed,
            candidate_outcomes=outcomes,
            formation_total_duration_ms=total_duration_ms,
            model_extraction_duration_ms=extraction_ms,
            persistence_duration_ms=persistence_ms,
            safe_error_code=error_code.value if error_code else None,
        )
        self._memoize(run_id, exchange_id, result)
        return result

    def _memoize(
        self, run_id: str, exchange_id: str, result: SemanticFormationResult
    ) -> None:
        with self._guard_lock:
            self._memoized_results[(run_id, exchange_id)] = result

    # -- observation（journal-first typed event；best-effort） ----------------

    async def _observe(
        self,
        receipt: CommittedExchangeReceipt,
        result: SemanticFormationResult,
    ) -> None:
        span = None
        if self._span_recorder is not None:
            parent = current_trace_context()
            span = start_span_safely(
                self._span_recorder,
                trace_id=(
                    parent.trace_id if parent is not None else (result.run_id)
                ),
                run_id=result.run_id,
                component="semantic_memory_formation",
                operation=_SEMANTIC_FORMATION_SPAN_OPERATION,
                parent_context=parent,
            )
        if span is not None:
            set_span_attributes(
                span,
                entry_agent_id=result.agent_id,
                memory_scope=result.memory_scope,
                formation_method=result.formation_method,
                formation_status=result.status.value,
                safe_error_code=result.safe_error_code,
                exchange_id=result.exchange_id,
                proposed_count=result.proposed_count,
                accepted_count=result.accepted_count,
                ignored_count=result.ignored_count,
                persisted_count=result.persisted_count,
                reused_count=result.reused_count,
                failed_count=result.failed_count,
                formation_total_duration_ms=result.formation_total_duration_ms,
                model_extraction_duration_ms=result.model_extraction_duration_ms,
                persistence_duration_ms=result.persistence_duration_ms,
            )
            if result.status is SemanticFormationStatus.SUCCEEDED:
                span.end_ok()
            elif result.status is SemanticFormationStatus.CANCELLED:
                span.end_cancelled(result.safe_error_code or "CANCELLED")
            elif result.status is SemanticFormationStatus.TIMED_OUT:
                span.end_timed_out(result.safe_error_code or "TIMED_OUT")
            else:
                span.end_error(result.safe_error_code or "FAILED")
        if self._event_emitter is not None:
            try:
                await self._event_emitter.emit(
                    RuntimeEventType.MEMORY_FORMATION_COMPLETED,
                    MemoryFormationCompletedPayload(
                        exchange_id=result.exchange_id,
                        agent_id=result.agent_id,
                        memory_scope=result.memory_scope,
                        formation_method=result.formation_method,
                        status=result.status.value,
                        safe_error_code=result.safe_error_code,
                        schema_version=result.schema_version,
                        proposed_count=result.proposed_count,
                        accepted_count=result.accepted_count,
                        ignored_count=result.ignored_count,
                        persisted_count=result.persisted_count,
                        reused_count=result.reused_count,
                        failed_count=result.failed_count,
                        formation_total_duration_ms=(
                            result.formation_total_duration_ms
                        ),
                        model_extraction_duration_ms=(
                            result.model_extraction_duration_ms
                        ),
                        persistence_duration_ms=result.persistence_duration_ms,
                        candidate_outcomes=result.candidate_outcomes_encoded(),
                    ),
                    component="semantic_memory_formation",
                    ignore_run_cancellation=True,
                )
            except (asyncio.CancelledError, Exception):
                # Observation failure 永远 best-effort：不 rollback Memory、
                # 不改变 delivery / terminal。
                return

    def __repr__(self) -> str:
        return (
            "SemanticMemoryFormation("
            f"entry_agent_id={self._entry_agent_id!r}, "
            f"run_id={self._run_id!r}, "
            f"memory_scope={self._memory_scope!r})"
        )


__all__ = [
    "FORMATION_MAX_CANDIDATES",
    "FORMATION_MEMORY_SCOPE",
    "FORMATION_METHOD_HYBRID",
    "FORMATION_ORIGIN_TYPE",
    "FORMATION_PERSISTENCE_RETRY_LIMIT",
    "FORMATION_SCHEMA_VERSION",
    "FORMATION_TIMEOUT_SECONDS",
    "CandidateValidation",
    "FormationCandidateCategory",
    "FormationCandidateOutcome",
    "FormationCandidateOutcomeCode",
    "FormationExtractionModel",
    "FormationProposal",
    "SemanticFormationError",
    "SemanticFormationErrorCode",
    "SemanticFormationResult",
    "SemanticFormationStatus",
    "SemanticMemoryFormation",
    "StrictFormationProposalParser",
    "UnifiedFormationExtractionAdapter",
    "validate_candidate",
]
