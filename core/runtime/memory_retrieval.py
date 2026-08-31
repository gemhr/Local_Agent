#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WP4-B Long-term Memory Retrieval, Ranking & Context Projection（v1）。

RETRIEVAL_OWNER / RANKING_OWNER = MemoryRetrievalService；PERSISTENCE_OWNER
仍是 ``AdvancedMemoryStore``（SQLite ``long_term_memory`` 是唯一 Source of
Truth）。本组件只拥有：原始 query 接收、scope-bound candidate collection、
eligibility filtering、bounded deterministic lexical matching、deterministic
ranking、top-K / char-budget selection，以及把 selected rows 投影为
``MemoryContextRecord``。

v1 策略：``SQLITE_BOUNDED_LEXICAL_NO_DERIVED_INDEX``。没有 Memory vector
index、没有 FTS business authority、没有 Knowledge RAG collection 复用、
没有 dual-write、没有新依赖。失败策略：
``BEST_EFFORT_EMPTY_BUNDLE_NO_STALE_FALLBACK``（由调用方 RunCoordinator 执行；
本组件抛 typed ``MemoryRetrievalError``）。

禁止：写 Memory、lifecycle mutation、prompt string assembly、Model ranking、
Embedding、Vector search。model-visible content 只来自 ``canonical_text``；
payload / logical_key 仅参与 lexical matching。
"""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Optional, Tuple

from core.advanced_memory import (
    AdvancedMemoryStore,
    MemoryDomainError,
    MemoryErrorCode,
    MemoryStatus,
    MemoryType,
    EpisodicMemoryRecord,
    SemanticMemoryRecord,
)
from core.runtime.model_context import (
    ContextSourceType,
    ContextTrustLevel,
    EpisodicMemoryContextRecord,
    MemoryContextRecord,
    MemoryProvenance,
)
from core.runtime.memory_authorization import (
    MemoryAccessAuthorizer,
    MemoryAccessPrincipal,
    MemoryAuthorizationResult,
)

#: 与 AgentRouter.DIRECT_MEMORY_SCOPE 一致的既有 scope 常量（不新建 identity）。
MEMORY_DIRECT_SCOPE = "direct"

RETRIEVAL_METHOD = "SQLITE_BOUNDED_LEXICAL_V1"
RANKING_METHOD = "DETERMINISTIC_LEXICAL_V1"
MEMORY_RETRIEVAL_SCHEMA_VERSION = 1

# 保守 bounded defaults（WP4-B configuration decision；正整数、无无限 context）。
DEFAULT_CANDIDATE_LIMIT = 64
DEFAULT_TOP_K = 5
DEFAULT_MAX_MEMORY_CONTEXT_CHARS = 2000
DEFAULT_MAX_MEMORY_RECORD_CHARS = 600
EPISODIC_MAX_SELECTED = 3
EPISODIC_MAX_CONTEXT_CHARS = 1200


class MemoryRetrievalErrorCode:
    FAILED = "MEMORY_RETRIEVAL_FAILED"
    UNAVAILABLE = "MEMORY_RETRIEVAL_UNAVAILABLE"


class MemoryRetrievalError(RuntimeError):
    """Typed retrieval failure；不暴露 SQL、路径、正文或原始异常。"""

    def __init__(self, error_code: str, safe_message: str) -> None:
        self.error_code = error_code
        self.safe_message = safe_message
        super().__init__(f"{safe_message} (error_code={error_code})")


def _require_positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MemoryRetrievalError(
            MemoryRetrievalErrorCode.FAILED, f"{name} 必须是正整数"
        )
    return value


_LATIN_RUN = re.compile(r"[0-9a-z_]+")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_TOKEN_RUN = re.compile(r"[0-9a-z_]+|[\u4e00-\u9fff]+")


def _normalize_text(text: str) -> str:
    """deterministic normalization：NFKC + casefold（不改变语义来源）。"""
    return unicodedata.normalize("NFKC", text).casefold()


def _tokenize(text: str) -> Tuple[str, ...]:
    """deterministic tokens：Latin/digit run 为整 token；CJK run 用 bigram
    （单字 run 保留单字），降低单字误命中噪声。"""
    normalized = _normalize_text(text)
    tokens: list[str] = []
    for run in _TOKEN_RUN.findall(normalized):
        if _CJK_RUN.fullmatch(run):
            if len(run) == 1:
                tokens.append(run)
            else:
                tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
        else:
            tokens.append(run)
    return tuple(tokens)


def _payload_match_text(payload: object) -> str:
    """payload 的 safe scalar 匹配表示（matching only，不进 prompt）。"""
    if not isinstance(payload, dict):
        return ""
    parts: list[str] = []
    for key in sorted(payload.keys()):
        value = payload[key]
        if isinstance(value, bool):
            parts.append("true" if value else "false")
        elif isinstance(value, int):
            parts.append(str(value))
        elif isinstance(value, float):
            parts.append(repr(value))
        elif isinstance(value, str):
            parts.append(value)
    return " ".join(parts)


@dataclass(frozen=True)
class MemoryRetrievalEvidence:
    """internal / evaluation-only 安全投影；永远不进入 model prompt。

    只保存 opaque memory_id、match component scores、rank、selected flag、
    budget/drop reason。禁止携带 canonical text、payload、logical_key、
    origin IDs 或 retrieval diagnostics 正文。
    """

    memory_id: str
    registered: bool
    lexical_match_score: int
    registered_exact_logical_key_match: bool
    canonical_text_exact_match: bool
    rank: int
    selected: bool
    drop_reason: Optional[str] = None


@dataclass(frozen=True)
class EpisodicMemorySelection:
    """内部 selection evidence；不得写入 prompt 或 runtime event。"""
    memory_id: str
    lexical_match_score: int
    rank: int
    selected: bool
    canonical_text: str
    drop_reason: Optional[str] = None


@dataclass(frozen=True)
class MemoryContextBundle:
    """run-scoped immutable retrieval projection（不是 string，不是可变
    message list，不持有 DB connection 或 callable）。

    ``records`` 是按 deterministic ranking 排序、经 top-K + char budget
    选择的 ``MemoryContextRecord``；ContextBuilder 是唯一注入 Owner。
    """

    records: Tuple[MemoryContextRecord, ...]
    evidence: Tuple[MemoryRetrievalEvidence, ...]
    entry_agent_id: str
    memory_scope: str
    retrieval_method: str
    ranking_method: str
    candidate_count: int
    eligible_count: int
    malformed_count: int
    selected_count: int
    omitted_count: int
    budget_used_chars: int
    registered_selected_count: int
    open_selected_count: int
    schema_version: int = MEMORY_RETRIEVAL_SCHEMA_VERSION
    episodic_records: Tuple[EpisodicMemoryContextRecord, ...] = ()
    episodic_evidence: Tuple[EpisodicMemorySelection, ...] = ()
    episodic_candidate_count: int = 0
    episodic_selected_count: int = 0
    episodic_budget_used_chars: int = 0
    project_records: Tuple[MemoryContextRecord, ...] = ()
    project_candidate_count: int = 0
    project_selected_count: int = 0
    authorization: MemoryAuthorizationResult | None = None

    @property
    def record_count(self) -> int:
        return len(self.records) + len(self.project_records) + len(self.episodic_records)

    @property
    def semantic_records(self) -> Tuple[MemoryContextRecord, ...]:
        return self.records

    @property
    def all_records(self) -> Tuple[MemoryContextRecord | EpisodicMemoryContextRecord, ...]:
        """固定注入顺序：PRIVATE Semantic → PROJECT Semantic → Episodic。"""
        return self.records + self.project_records + self.episodic_records

    @classmethod
    def empty(
        cls,
        entry_agent_id: str,
        memory_scope: str,
        authorization: MemoryAuthorizationResult | None = None,
    ) -> "MemoryContextBundle":
        return cls(
            records=(),
            evidence=(),
            entry_agent_id=entry_agent_id,
            memory_scope=memory_scope,
            retrieval_method=RETRIEVAL_METHOD,
            ranking_method=RANKING_METHOD,
            candidate_count=0,
            eligible_count=0,
            malformed_count=0,
            selected_count=0,
            omitted_count=0,
            budget_used_chars=0,
            registered_selected_count=0,
            open_selected_count=0,
            authorization=authorization,
        )


@dataclass(frozen=True)
class MemoryInjectionReport:
    """Builder-acceptance evidence：selection != injection。

    ``supplied_count`` 是本 bundle 交给 ContextBuilder 的 record 数；
    ``accepted_count`` 是 Builder 最终接纳进 model context 的
    MEMORY_RETRIEVAL item 数。Builder 预算丢弃时 accepted < supplied，
    不得宣称 supplied 条全部 injected。
    """

    target: str
    supplied_count: int
    accepted_count: int
    dropped_count: int


@dataclass(frozen=True)
class _ScoredCandidate:
    record: SemanticMemoryRecord
    score: int
    registered_exact: bool
    text_exact: bool


class MemoryRetrievalService:
    """RUN_SCOPE retrieval policy component（每 Run 一个实例、一次 retrieval）。

    不拥有 connection；``AdvancedMemoryStore`` 窄读 primitive 负责读取
    authority rows。本服务不写 Memory、不改 lifecycle、不拼 prompt。
    """

    def __init__(
        self,
        memory_store: AdvancedMemoryStore,
        *,
        candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
        top_k: int = DEFAULT_TOP_K,
        max_memory_context_chars: int = DEFAULT_MAX_MEMORY_CONTEXT_CHARS,
        max_memory_record_chars: int = DEFAULT_MAX_MEMORY_RECORD_CHARS,
        authorizer: MemoryAccessAuthorizer | None = None,
    ) -> None:
        if not isinstance(memory_store, AdvancedMemoryStore):
            raise MemoryRetrievalError(
                MemoryRetrievalErrorCode.FAILED,
                "MemoryRetrievalService 需要 AdvancedMemoryStore",
            )
        self._memory_store = memory_store
        self._candidate_limit = _require_positive_int(
            candidate_limit, "candidate_limit"
        )
        self._top_k = _require_positive_int(top_k, "top_k")
        self._max_memory_context_chars = _require_positive_int(
            max_memory_context_chars, "max_memory_context_chars"
        )
        self._max_memory_record_chars = _require_positive_int(
            max_memory_record_chars, "max_memory_record_chars"
        )
        if authorizer is not None and not isinstance(authorizer, MemoryAccessAuthorizer):
            raise TypeError("authorizer 必须是 MemoryAccessAuthorizer 或 None")
        self._authorizer = authorizer or MemoryAccessAuthorizer()

    # ------------------------------------------------------------------
    # Public entry（每 Run 最多调用一次；禁止第二次 retrieval）
    def _retrieve_episodic(
        self, *, owner_agent_id: str, memory_scope: str, query: str,
    ) -> tuple[Tuple[EpisodicMemoryContextRecord, ...], Tuple[EpisodicMemorySelection, ...], int, int, int]:
        """只读 EPISODIC narrow read；canonical_text 是唯一匹配来源。"""
        try:
            read = self._memory_store.list_active_episodic_for_scope(
                owner_agent_id, memory_scope, candidate_limit=self._candidate_limit
            )
        except MemoryDomainError as exc:
            code = MemoryRetrievalErrorCode.UNAVAILABLE if exc.error_code == MemoryErrorCode.PERSISTENCE_FAILED else MemoryRetrievalErrorCode.FAILED
            raise MemoryRetrievalError(code, "Episodic Memory authority read failed") from None
        query_tokens = tuple(dict.fromkeys(_tokenize(query)))
        candidates: list[tuple[EpisodicMemoryRecord, int]] = []
        for record in read.records:
            if (record.memory_type is not MemoryType.EPISODIC or record.status is not MemoryStatus.ACTIVE
                    or record.agent_id != owner_agent_id or record.memory_scope != memory_scope):
                continue
            candidates.append((record, sum(token in set(_tokenize(record.canonical_text)) for token in query_tokens)))
        ranked = sorted(candidates, key=lambda item: (-item[1], -item[0].created_at.timestamp(), item[0].memory_id))
        records: list[EpisodicMemoryContextRecord] = []
        evidence: list[EpisodicMemorySelection] = []
        used = 0
        for rank, (record, score) in enumerate(ranked, start=1):
            reason: str | None = None
            if score <= 0:
                reason = "NO_LEXICAL_MATCH"
            elif len(records) >= EPISODIC_MAX_SELECTED:
                reason = "TOP_K_EXCEEDED"
            elif len(record.canonical_text) > self._max_memory_record_chars:
                reason = "RECORD_CHAR_BUDGET_EXCEEDED"
            elif used + len(record.canonical_text) > EPISODIC_MAX_CONTEXT_CHARS:
                reason = "CONTEXT_CHAR_BUDGET_EXCEEDED"
            selected = reason is None
            evidence.append(EpisodicMemorySelection(record.memory_id, score, rank, selected, record.canonical_text, reason))
            if not selected:
                continue
            used += len(record.canonical_text)
            records.append(EpisodicMemoryContextRecord(
                provenance=MemoryProvenance(record.memory_id, record.memory_type.value, record.memory_id),
                content=record.canonical_text, created_at=record.created_at,
            ))
        return tuple(records), tuple(evidence), len(read.records) + read.malformed_count, len(records), used

    # ------------------------------------------------------------------

    def retrieve(
        self,
        *,
        memory_scope: str,
        query: str,
        requester: MemoryAccessPrincipal | None = None,
        target_owner_agent_id: str | None = None,
        agent_id: str | None = None,
    ) -> MemoryContextBundle:
        """授权后执行两个独立 typed pipeline；拒绝时绝不触达 Store。

        ``agent_id`` 仅作为历史 target 参数别名保留；它永远不会被转换成
        requester authorization proof。canonical Coordinated Runtime 必须传入
        typed ``requester`` 和明确的 ``target_owner_agent_id``；requester 缺失
        时 fail closed。
        """
        owner_agent_id = target_owner_agent_id if target_owner_agent_id is not None else agent_id
        decision = self._authorizer.authorize_private_read(
            requester,
            owner_agent_id,
            memory_scope,
            requested_memory_scope=memory_scope,
        )
        if not decision.allowed:
            return MemoryContextBundle.empty(owner_agent_id or "", memory_scope, decision)
        semantic_error: MemoryRetrievalError | None = None
        try:
            semantic = self._retrieve_semantic(agent_id=owner_agent_id, memory_scope=memory_scope, query=query)
        except MemoryRetrievalError as exc:
            semantic_error = exc
            semantic = MemoryContextBundle.empty(owner_agent_id or "", memory_scope, decision)
        except Exception:
            semantic_error = MemoryRetrievalError(MemoryRetrievalErrorCode.FAILED, "Long-term Memory retrieval failed")
            semantic = MemoryContextBundle.empty(owner_agent_id or "", memory_scope, decision)
        try:
            episodic = self._retrieve_episodic(owner_agent_id=owner_agent_id, memory_scope=memory_scope, query=query)
        except Exception:
            episodic = ((), (), 0, 0, 0)
        if semantic_error is not None and not episodic[0]:
            raise semantic_error
        return MemoryContextBundle(
            **{**semantic.__dict__, "episodic_records": episodic[0], "episodic_evidence": episodic[1],
               "episodic_candidate_count": episodic[2], "episodic_selected_count": episodic[3],
               "episodic_budget_used_chars": episodic[4], "authorization": decision}
        )

    def _retrieve_semantic(
        self,
        *,
        agent_id: str,
        memory_scope: str,
        query: str,
    ) -> MemoryContextBundle:
        """scope-bound deterministic lexical retrieval → immutable bundle。"""
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise MemoryRetrievalError(
                MemoryRetrievalErrorCode.FAILED, "agent_id 必须是非空字符串"
            )
        if not isinstance(memory_scope, str) or not memory_scope.strip():
            raise MemoryRetrievalError(
                MemoryRetrievalErrorCode.FAILED, "memory_scope 必须是非空字符串"
            )
        if not isinstance(query, str) or not query.strip():
            raise MemoryRetrievalError(
                MemoryRetrievalErrorCode.FAILED, "query 必须是非空字符串"
            )
        started = time.monotonic()
        try:
            read = self._memory_store.list_active_semantic_for_scope(
                agent_id,
                memory_scope,
                candidate_limit=self._candidate_limit,
            )
        except MemoryDomainError as exc:
            code = (
                MemoryRetrievalErrorCode.UNAVAILABLE
                if exc.error_code == MemoryErrorCode.PERSISTENCE_FAILED
                else MemoryRetrievalErrorCode.FAILED
            )
            raise MemoryRetrievalError(
                code, "Long-term Memory authority read failed"
            ) from None
        query_tokens = dict.fromkeys(_tokenize(query))
        query_token_tuple = tuple(query_tokens)

        eligible: list[_ScoredCandidate] = []
        malformed = read.malformed_count
        for record in read.records:
            scored = self._score_candidate(record, agent_id, memory_scope, query_token_tuple)
            if scored is None:
                malformed += 1
                continue
            eligible.append(scored)

        ranked = sorted(
            eligible,
            key=lambda c: (
                -c.score,
                -int(c.registered_exact),
                -int(c.text_exact),
                -c.record.created_at.timestamp(),
                c.record.memory_id,
            ),
        )

        records: list[MemoryContextRecord] = []
        evidence: list[MemoryRetrievalEvidence] = []
        selected_registered = 0
        selected_open = 0
        budget_used = 0
        selected_count = 0
        for rank, candidate in enumerate(ranked, start=1):
            base_evidence = MemoryRetrievalEvidence(
                memory_id=candidate.record.memory_id,
                registered=candidate.record.logical_key is not None,
                lexical_match_score=candidate.score,
                registered_exact_logical_key_match=candidate.registered_exact,
                canonical_text_exact_match=candidate.text_exact,
                rank=rank,
                selected=False,
            )
            if candidate.score <= 0:
                evidence.append(
                    MemoryRetrievalEvidence(
                        **{**base_evidence.__dict__, "drop_reason": "NO_LEXICAL_MATCH"}
                    )
                )
                continue
            if selected_count >= self._top_k:
                evidence.append(
                    MemoryRetrievalEvidence(
                        **{**base_evidence.__dict__, "drop_reason": "TOP_K_EXCEEDED"}
                    )
                )
                continue
            text_len = len(candidate.record.canonical_text)
            if text_len > self._max_memory_record_chars:
                evidence.append(
                    MemoryRetrievalEvidence(
                        **{
                            **base_evidence.__dict__,
                            "drop_reason": "RECORD_CHAR_BUDGET_EXCEEDED",
                        }
                    )
                )
                continue
            if budget_used + text_len > self._max_memory_context_chars:
                evidence.append(
                    MemoryRetrievalEvidence(
                        **{
                            **base_evidence.__dict__,
                            "drop_reason": "CONTEXT_CHAR_BUDGET_EXCEEDED",
                        }
                    )
                )
                continue
            selected_count += 1
            budget_used += text_len
            if candidate.record.logical_key is not None:
                selected_registered += 1
            else:
                selected_open += 1
            evidence.append(
                MemoryRetrievalEvidence(
                    **{**base_evidence.__dict__, "selected": True}
                )
            )
            records.append(
                MemoryContextRecord(
                    provenance=MemoryProvenance(
                        memory_id=candidate.record.memory_id,
                        memory_type=candidate.record.memory_type.value,
                        record_id=candidate.record.memory_id,
                    ),
                    source_type=ContextSourceType.MEMORY_RETRIEVAL,
                    content=candidate.record.canonical_text,
                    created_at=candidate.record.created_at,
                    priority=700,
                    trust_level=ContextTrustLevel.USER_CONTENT,
                )
            )

        return MemoryContextBundle(
            records=tuple(records),
            evidence=tuple(evidence),
            entry_agent_id=agent_id,
            memory_scope=memory_scope,
            retrieval_method=RETRIEVAL_METHOD,
            ranking_method=RANKING_METHOD,
            candidate_count=len(read.records) + malformed,
            eligible_count=len(eligible),
            malformed_count=malformed,
            selected_count=selected_count,
            omitted_count=len(eligible) - selected_count,
            budget_used_chars=budget_used,
            registered_selected_count=selected_registered,
            open_selected_count=selected_open,
        )

    # ------------------------------------------------------------------

    @staticmethod
    def _is_contiguous_subsequence(
        needle: Tuple[str, ...], haystack: Tuple[str, ...]
    ) -> bool:
        if not needle or len(needle) > len(haystack):
            return False
        for start in range(len(haystack) - len(needle) + 1):
            if haystack[start : start + len(needle)] == needle:
                return True
        return False

    def _score_candidate(
        self,
        record: SemanticMemoryRecord,
        agent_id: str,
        memory_scope: str,
        query_tokens: Tuple[str, ...],
    ) -> Optional[_ScoredCandidate]:
        """fail-closed eligibility + deterministic lexical scoring。

        eligibility：SEMANTIC / ACTIVE / agent exact / scope exact；任何
        不满足（含未知 enum 等畸形历史数据）返回 None → drop candidate。
        """
        if record.memory_type is not MemoryType.SEMANTIC:
            return None
        if record.status is not MemoryStatus.ACTIVE:
            return None
        if record.agent_id != agent_id or record.memory_scope != memory_scope:
            return None
        candidate_tokens = set(
            _tokenize(record.canonical_text)
        )
        key_tokens: Tuple[str, ...] = ()
        if record.logical_key is not None:
            key_tokens = _tokenize(record.logical_key)
            candidate_tokens.update(key_tokens)
        payload_text = _payload_match_text(record.payload)
        if payload_text:
            candidate_tokens.update(_tokenize(payload_text))
        score = sum(1 for token in query_tokens if token in candidate_tokens)
        registered_exact = bool(key_tokens) and self._is_contiguous_subsequence(
            key_tokens, query_tokens
        )
        text_exact = self._is_contiguous_subsequence(
            _tokenize(record.canonical_text), query_tokens
        )
        return _ScoredCandidate(
            record=record,
            score=score,
            registered_exact=registered_exact,
            text_exact=text_exact,
        )


__all__ = [
    "DEFAULT_CANDIDATE_LIMIT",
    "DEFAULT_MAX_MEMORY_CONTEXT_CHARS",
    "DEFAULT_MAX_MEMORY_RECORD_CHARS",
    "DEFAULT_TOP_K",
    "EPISODIC_MAX_CONTEXT_CHARS",
    "EPISODIC_MAX_SELECTED",
    "MEMORY_DIRECT_SCOPE",
    "MEMORY_RETRIEVAL_SCHEMA_VERSION",
    "MemoryContextBundle",
    "MemoryInjectionReport",
    "MemoryRetrievalError",
    "MemoryRetrievalErrorCode",
    "MemoryRetrievalEvidence",
    "EpisodicMemorySelection",
    "MemoryRetrievalService",
    "RANKING_METHOD",
    "RETRIEVAL_METHOD",
]
