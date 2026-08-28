#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WP3-B explicit forget source / targeting component。

Forget 与 remember Formation 对同一 exchange 互斥：只有 original-user
deterministic forget cue 命中后才进入 destructive branch，且该 branch 不再
形成新的 Semantic Memory。

职责边界：
- 本模块只负责 forget intent detection、Model target proposal、strict
  parser、exact membership 校验与 typed outcome；
- 最终 mutation 由 ``AdvancedMemoryStore.forget_semantic_partition`` 在
  单个 ``BEGIN IMMEDIATE`` 事务内完成；本模块不拥有 SQL / connection；
- Model 只能提议一个 bounded existing logical-key，绝不能输出 memory_id、
  status、agent、scope、SQL、operation 或 supersede。

输入 allowlist：original user query（唯一事实 authority）+ 当前同
agent/scope/type partition 的 bounded existing-key allowlist。禁止 canonical
text、payload value、RAG、Tool、final answer、specialist trace、CoT 进入
Model 输入；logical_key 永不进入 event/metrics。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence

#: deterministic explicit-forget cue（WP3-B 冻结；未命中绝不调用 destructive parser）。
_EXPLICIT_FORGET_CUE_PATTERN = re.compile(
    r"^\s*(?:(?:请|请你|帮我|请帮我|麻烦)\s*)?(?:"
    r"(?:忘记|忘掉)\s*.+|"
    r"不要再?记住\s*.+|"
    r"删除(?:这项|该|这个)?记忆\s*.*|"
    r"把\s*.+(?:记忆|这件事)\s*(?:删掉|删除|忘掉)|"
    r"别记了\s*.*)[。.!！]?\s*$"
)

_LOGICAL_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
_SAFE_REASON_PATTERN = re.compile(r"^[A-Z0-9_]{1,40}$")

FORGET_SCHEMA_VERSION = 1
FORGET_MAX_RAW_OUTPUT_CHARS = 8_192
FORGET_MAX_SOURCE_EXCERPT_CHARS = 400
FORGET_MAX_REASON_CHARS = 80
FORGET_MAX_ALLOWLIST_KEYS = 64

_FORBIDDEN_PROPOSAL_FIELDS = frozenset(
    {
        "memory_id",
        "status",
        "memory_status",
        "agent_id",
        "memory_scope",
        "origin",
        "origin_type",
        "sql",
        "operation",
        "supersede",
        "superseded_by",
        "superseded_by_memory_id",
        "forget",
        "memory_type",
        "created_at",
        "updated_at",
    }
)


def has_explicit_forget_cue(user_query: str) -> bool:
    """deterministic code gate：只有命中明确 forget cue 才进入 forget branch。"""
    if not isinstance(user_query, str) or not user_query.strip():
        return False
    normalized = user_query.strip()
    # 问句/回忆确认不是 destructive instruction；不确定时 fail closed。
    if re.search(r"(?:吗|么|呢|[?？])\s*$", normalized):
        return False
    return bool(_EXPLICIT_FORGET_CUE_PATTERN.search(normalized))


class ForgetProposalErrorCode:
    OUTPUT_INVALID = "FORGET_OUTPUT_INVALID"
    OUTPUT_UNKNOWN_FIELD = "FORGET_OUTPUT_UNKNOWN_FIELD"
    OUTPUT_FORBIDDEN_FIELD = "FORGET_OUTPUT_FORBIDDEN_FIELD"
    TARGET_MISSING = "FORGET_TARGET_MISSING"
    TARGET_INVALID_SYNTAX = "FORGET_TARGET_INVALID_SYNTAX"
    TARGET_NOT_MEMBER = "FORGET_TARGET_NOT_MEMBER"
    TARGET_AMBIGUOUS = "FORGET_TARGET_AMBIGUOUS"
    ALLOWLIST_OVERFLOW = "FORGET_ALLOWLIST_OVERFLOW"


class ForgetProposalError(RuntimeError):
    """typed forget proposal 失败；只携带安全 error code。"""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(f"explicit forget proposal failed ({error_code})")


@dataclass(frozen=True, slots=True)
class ForgetProposal:
    """strict parser 产出的一条 target proposal（未经验证）。"""

    logical_key: Any
    source_excerpt: Any = None
    safe_reason: Any = None


class ExplicitForgetIntentParser:
    """strict forget proposal parser：只接受固定 schema/version、单个 bounded
    logical key、source excerpt 与 safe reason。unknown/forbidden 字段整体
    fail closed。
    """

    @classmethod
    def parse(cls, raw_output: str) -> ForgetProposal:
        if not isinstance(raw_output, str):
            raise ForgetProposalError(ForgetProposalErrorCode.OUTPUT_INVALID)
        if len(raw_output) > FORGET_MAX_RAW_OUTPUT_CHARS:
            raise ForgetProposalError(ForgetProposalErrorCode.OUTPUT_INVALID)
        try:
            payload = json.loads(raw_output)
        except (ValueError, RecursionError):
            raise ForgetProposalError(ForgetProposalErrorCode.OUTPUT_INVALID) from None
        if not isinstance(payload, dict):
            raise ForgetProposalError(ForgetProposalErrorCode.OUTPUT_INVALID)
        if set(payload) != {
            "schema_version",
            "logical_key",
            "source_excerpt",
            "safe_reason",
        }:
            raise ForgetProposalError(ForgetProposalErrorCode.OUTPUT_INVALID)
        if payload["schema_version"] != FORGET_SCHEMA_VERSION:
            raise ForgetProposalError(ForgetProposalErrorCode.OUTPUT_INVALID)
        return ForgetProposal(
            logical_key=payload["logical_key"],
            source_excerpt=payload["source_excerpt"],
            safe_reason=payload["safe_reason"],
        )

    @classmethod
    def validate(
        cls,
        proposal: ForgetProposal,
        allowlist: Sequence[str],
        *,
        user_query: str,
    ) -> Optional[str]:
        """exact membership 校验：返回经过验证的 exact existing logical_key。

        - 语法不合法 / 不在 allowlist / allowlist 为空（无精确成员）→ None
          （fail closed，zero mutation）。不允许 fuzzy、canonical text、vector
          或 Model 发明 key。
        """
        if not isinstance(user_query, str):
            return None
        if not isinstance(proposal.source_excerpt, str):
            return None
        excerpt = proposal.source_excerpt.strip()
        if (
            not excerpt
            or len(excerpt) > FORGET_MAX_SOURCE_EXCERPT_CHARS
            or excerpt not in user_query
        ):
            return None
        if not isinstance(proposal.safe_reason, str):
            return None
        reason = proposal.safe_reason.strip()
        if (
            not reason
            or len(reason) > FORGET_MAX_REASON_CHARS
            or not _SAFE_REASON_PATTERN.fullmatch(reason)
        ):
            return None
        if not isinstance(proposal.logical_key, str):
            return None
        key = proposal.logical_key.strip()
        if not _LOGICAL_KEY_PATTERN.fullmatch(key):
            return None
        if key not in set(allowlist):
            return None
        return key


class ForgetProposalModel:
    """同步窄 seam：真实实现经 AgentRouter 复用统一 Model Invocation。

    Model 输入只允许 original user query + bounded existing-key allowlist。
    """

    def propose_key(self, user_query: str, allowlist: Sequence[str]) -> str:
        raise NotImplementedError


class UnifiedForgetProposalAdapter(ForgetProposalModel):
    """把 ``AgentRouter.complete_forget_proposal`` 适配为 forget proposal seam。"""

    def __init__(self, router, *, run_context, event_emitter=None, fault_controller=None) -> None:
        self._router = router
        self._run_context = run_context
        self._event_emitter = event_emitter
        self._fault_controller = fault_controller

    def propose_key(self, user_query: str, allowlist: Sequence[str]) -> str:
        return self._router.complete_forget_proposal(
            user_query,
            list(allowlist),
            run_context=self._run_context,
            event_emitter=self._event_emitter,
            fault_controller=self._fault_controller,
        )


__all__ = [
    "ExplicitForgetIntentParser",
    "FORGET_MAX_ALLOWLIST_KEYS",
    "FORGET_SCHEMA_VERSION",
    "ForgetProposal",
    "ForgetProposalError",
    "ForgetProposalErrorCode",
    "ForgetProposalModel",
    "UnifiedForgetProposalAdapter",
    "has_explicit_forget_cue",
]
