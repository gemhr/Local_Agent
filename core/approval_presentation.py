#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tool Approval 的桌面端 Presentation State（非 Runtime truth）。

本模块只描述卡片展示态与幂等更新规则，不持有 ApprovalStatus / StepStatus /
RunStatus，也不向 Runtime 写回任何状态。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import re
from typing import Any, Mapping

_BINDING_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")

_REQUESTED_EVENT = "TOOL_APPROVAL_REQUESTED"
_DECIDED_EVENT = "TOOL_APPROVAL_DECIDED"

_DECIDED_APPROVED = "APPROVED"
_DECIDED_REJECTED = "REJECTED"
_DECIDED_INVALIDATED_CANCELLED = "INVALIDATED_CANCELLED"
_DECIDED_INVALIDATED_TIMEOUT = "INVALIDATED_TIMEOUT"

_HTTP_APPROVED_STATUSES = frozenset({"APPROVED", "EXECUTION_CLAIMED"})
_HTTP_REJECTED_STATUSES = frozenset({"REJECTED"})
_HTTP_EXPIRED_STATUSES = frozenset(
    {"INVALIDATED_CANCELLED", "INVALIDATED_TIMEOUT", "PENDING"}
)

_EXPIRE_EVENT_TYPES = frozenset(
    {
        "CANCELLATION",
        "TIMEOUT",
        "BUDGET_EXHAUSTED",
        "RUN_COMPLETED",
    }
)


class ApprovalPresentationState(str, Enum):
    """卡片展示态。不是 Runtime ApprovalState。"""

    PENDING = "PENDING"
    SUBMITTING = "SUBMITTING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    ERROR = "ERROR"


_TERMINAL_STATES = frozenset(
    {
        ApprovalPresentationState.APPROVED,
        ApprovalPresentationState.REJECTED,
        ApprovalPresentationState.EXPIRED,
    }
)

_TITLE_BY_STATE = {
    ApprovalPresentationState.PENDING: "需要人工审批",
    ApprovalPresentationState.SUBMITTING: "正在提交审批…",
    ApprovalPresentationState.APPROVED: "已批准",
    ApprovalPresentationState.REJECTED: "已拒绝",
    ApprovalPresentationState.EXPIRED: "审批已失效",
    ApprovalPresentationState.ERROR: "提交审批失败",
}

CONFLICT_MESSAGE = "审批状态已发生变化，请以当前运行状态为准。"
EXPIRED_MESSAGE = "该审批已失效。"
SUBMIT_FAILED_MESSAGE = "提交审批失败"
APPROVED_CONTINUING_MESSAGE = "正在继续执行"


@dataclass(frozen=True, slots=True)
class ApprovalCardModel:
    """单张 Approval Card 的展示模型。"""

    run_id: str
    approval_id: str
    invocation_binding_digest: str
    tool_name: str
    risk_level: str
    risk_facts: tuple[str, ...]
    state: ApprovalPresentationState = ApprovalPresentationState.PENDING
    message: str = ""
    buttons_enabled: bool = True
    command_in_flight: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return (self.run_id, self.approval_id)

    @property
    def title(self) -> str:
        return _TITLE_BY_STATE[self.state]

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL_STATES


@dataclass(frozen=True, slots=True)
class ApprovalCommandHttpResult:
    """approve/reject HTTP 的安全投影，不含 exception 正文。"""

    network_error: bool
    status_code: int | None = None
    effective_status: str | None = None
    error_code: str | None = None
    idempotent: bool | None = None
    decided_at: str | None = None


def parse_requested_event(
    event: Mapping[str, Any] | object,
    fallback_run_id: str = "",
) -> ApprovalCardModel | None:
    """从 public TOOL_APPROVAL_REQUESTED 投影构造展示模型。"""
    if not isinstance(event, Mapping):
        return None
    if str(event.get("event_type") or "") != _REQUESTED_EVENT:
        return None
    payload = event.get("payload") or {}
    if not isinstance(payload, Mapping):
        return None
    run_id = str(event.get("run_id") or fallback_run_id or "").strip()
    approval_id = str(payload.get("approval_id") or "").strip()
    digest = str(payload.get("invocation_binding_digest") or "").strip()
    tool_name = str(payload.get("tool_name") or "").strip()
    if not run_id or not approval_id or not tool_name:
        return None
    if _BINDING_DIGEST_PATTERN.fullmatch(digest) is None:
        return None
    risk_level = str(payload.get("risk_level") or "").strip()
    return ApprovalCardModel(
        run_id=run_id,
        approval_id=approval_id,
        invocation_binding_digest=digest,
        tool_name=tool_name,
        risk_level=risk_level,
        risk_facts=_parse_risk_facts(payload.get("risk_facts")),
    )


def parse_decided_event(
    event: Mapping[str, Any] | object,
    fallback_run_id: str = "",
) -> tuple[str, str, str] | None:
    """解析 public TOOL_APPROVAL_DECIDED：``(run_id, approval_id, decision_status)``。"""
    if not isinstance(event, Mapping):
        return None
    if str(event.get("event_type") or "") != _DECIDED_EVENT:
        return None
    payload = event.get("payload") or {}
    if not isinstance(payload, Mapping):
        return None
    run_id = str(event.get("run_id") or fallback_run_id or "").strip()
    approval_id = str(payload.get("approval_id") or "").strip()
    decision_status = str(payload.get("decision_status") or "").strip()
    if not run_id or not approval_id or not decision_status:
        return None
    return (run_id, approval_id, decision_status)


def event_expires_pending_approvals(event: Mapping[str, Any] | object) -> bool:
    """当前 control event 是否表示 Run 进入 terminal / cancel / timeout。"""
    if not isinstance(event, Mapping):
        return False
    return str(event.get("event_type") or "") in _EXPIRE_EVENT_TYPES


def remember_requested(
    models: dict[tuple[str, str], ApprovalCardModel],
    event: Mapping[str, Any] | object,
    fallback_run_id: str = "",
) -> ApprovalCardModel | None:
    """首次 REQUESTED 登记模型；重复 ``(run_id, approval_id)`` 忽略。"""
    parsed = parse_requested_event(event, fallback_run_id=fallback_run_id)
    if parsed is None:
        return None
    if parsed.key in models:
        return None
    models[parsed.key] = parsed
    return parsed


def begin_submit(
    model: ApprovalCardModel,
) -> tuple[ApprovalCardModel, bool]:
    """用户点击 Approve/Reject：进入 SUBMITTING 并禁止连点。

    Returns:
        (updated_model, should_send_http)
    """
    if model.is_terminal or model.command_in_flight:
        return model, False
    if model.state not in {
        ApprovalPresentationState.PENDING,
        ApprovalPresentationState.ERROR,
    }:
        return model, False
    updated = replace(
        model,
        state=ApprovalPresentationState.SUBMITTING,
        message="",
        buttons_enabled=False,
        command_in_flight=True,
    )
    return updated, True


def apply_http_result(
    model: ApprovalCardModel,
    result: ApprovalCommandHttpResult,
) -> ApprovalCardModel:
    """用 HTTP 响应做 quick command feedback；不得覆盖已有 terminal 展示态。"""
    if model.is_terminal:
        return replace(model, command_in_flight=False, buttons_enabled=False)
    if result.network_error:
        return replace(
            model,
            state=ApprovalPresentationState.ERROR,
            message=SUBMIT_FAILED_MESSAGE,
            buttons_enabled=True,
            command_in_flight=False,
        )
    status_code = result.status_code
    effective = result.effective_status or ""
    if status_code == 200:
        if effective in _HTTP_APPROVED_STATUSES:
            message = (
                APPROVED_CONTINUING_MESSAGE
                if effective == "EXECUTION_CLAIMED"
                else ""
            )
            return replace(
                model,
                state=ApprovalPresentationState.APPROVED,
                message=message,
                buttons_enabled=False,
                command_in_flight=False,
            )
        if effective in _HTTP_REJECTED_STATUSES:
            return replace(
                model,
                state=ApprovalPresentationState.REJECTED,
                message="",
                buttons_enabled=False,
                command_in_flight=False,
            )
        if effective in _HTTP_EXPIRED_STATUSES:
            return replace(
                model,
                state=ApprovalPresentationState.EXPIRED,
                message=EXPIRED_MESSAGE,
                buttons_enabled=False,
                command_in_flight=False,
            )
    if status_code == 410:
        return replace(
            model,
            state=ApprovalPresentationState.EXPIRED,
            message=EXPIRED_MESSAGE,
            buttons_enabled=False,
            command_in_flight=False,
        )
    if status_code == 409:
        return replace(
            model,
            state=ApprovalPresentationState.ERROR,
            message=CONFLICT_MESSAGE,
            buttons_enabled=False,
            command_in_flight=False,
        )
    if status_code == 404:
        return replace(
            model,
            state=ApprovalPresentationState.EXPIRED,
            message=EXPIRED_MESSAGE,
            buttons_enabled=False,
            command_in_flight=False,
        )
    return replace(
        model,
        state=ApprovalPresentationState.ERROR,
        message=SUBMIT_FAILED_MESSAGE,
        buttons_enabled=True,
        command_in_flight=False,
    )


def apply_decided(
    model: ApprovalCardModel,
    decision_status: str,
) -> ApprovalCardModel:
    """DECIDED 是观察到的 lifecycle 确认；terminal 展示态优先于 SUBMITTING。"""
    if decision_status == _DECIDED_APPROVED:
        return replace(
            model,
            state=ApprovalPresentationState.APPROVED,
            message="",
            buttons_enabled=False,
            command_in_flight=False,
        )
    if decision_status == _DECIDED_REJECTED:
        return replace(
            model,
            state=ApprovalPresentationState.REJECTED,
            message="",
            buttons_enabled=False,
            command_in_flight=False,
        )
    if decision_status in {
        _DECIDED_INVALIDATED_CANCELLED,
        _DECIDED_INVALIDATED_TIMEOUT,
    }:
        return replace(
            model,
            state=ApprovalPresentationState.EXPIRED,
            message=EXPIRED_MESSAGE,
            buttons_enabled=False,
            command_in_flight=False,
        )
    return replace(model, command_in_flight=False)


def expire_model(model: ApprovalCardModel) -> ApprovalCardModel:
    """Run/stream terminal 后结束未完成卡片，禁止继续发 command。"""
    if model.is_terminal:
        return replace(model, buttons_enabled=False, command_in_flight=False)
    return replace(
        model,
        state=ApprovalPresentationState.EXPIRED,
        message=EXPIRED_MESSAGE,
        buttons_enabled=False,
        command_in_flight=False,
    )


def apply_decided_to_store(
    models: dict[tuple[str, str], ApprovalCardModel],
    event: Mapping[str, Any] | object,
    fallback_run_id: str = "",
) -> ApprovalCardModel | None:
    parsed = parse_decided_event(event, fallback_run_id=fallback_run_id)
    if parsed is None:
        return None
    run_id, approval_id, decision_status = parsed
    model = models.get((run_id, approval_id))
    if model is None:
        return None
    updated = apply_decided(model, decision_status)
    models[model.key] = updated
    return updated


def expire_store(
    models: dict[tuple[str, str], ApprovalCardModel],
    run_id: str | None = None,
) -> list[ApprovalCardModel]:
    """将匹配 Run（或全部）的未收口卡片标为 EXPIRED。"""
    updated_models: list[ApprovalCardModel] = []
    for key, model in list(models.items()):
        if run_id and model.run_id != run_id:
            continue
        updated = expire_model(model)
        if updated is not model:
            models[key] = updated
            updated_models.append(updated)
        elif not updated.buttons_enabled and not updated.command_in_flight:
            continue
        else:
            models[key] = updated
            updated_models.append(updated)
    return updated_models


def _parse_risk_facts(raw: object) -> tuple[str, ...]:
    if isinstance(raw, str):
        return tuple(part for part in raw.split("|") if part)
    if isinstance(raw, (list, tuple)):
        facts: list[str] = []
        for item in raw:
            text = str(item).strip()
            if text:
                facts.append(text)
        return tuple(facts)
    return ()
