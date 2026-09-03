#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""桌面端 Tool Approval 短 HTTP command（不阻塞 UI 线程）。"""

from __future__ import annotations

from collections.abc import Callable

from core.approval_presentation import ApprovalCommandHttpResult

_APPROVE_ACTION = "approve"
_REJECT_ACTION = "reject"
_ALLOWED_ACTIONS = frozenset({_APPROVE_ACTION, _REJECT_ACTION})


def tool_approval_decision_url(
    api_base_url: str,
    run_id: str,
    approval_id: str,
    action: str,
) -> str:
    """构造现有 WP2 approve/reject route，不接受 cancel 路径。"""
    if action not in _ALLOWED_ACTIONS:
        raise ValueError("unsupported approval action")
    base = api_base_url.rstrip("/")
    return (
        f"{base}/api/runtime/runs/{run_id}/tool-approvals/{approval_id}/{action}"
    )


def request_tool_approval_decision(
    post: Callable[..., object],
    url: str,
    invocation_binding_digest: str,
    *,
    timeout: float = 5.0,
) -> ApprovalCommandHttpResult:
    """发送 approve/reject POST；调用者应在独立线程执行。

    不解析或回传 raw tool args / exception 正文。Reject 不得改走 cancel URL。
    """
    if "/cancel" in url:
        return ApprovalCommandHttpResult(network_error=True)
    try:
        response = post(
            url,
            json={"invocation_binding_digest": invocation_binding_digest},
            timeout=timeout,
        )
    except Exception:
        return ApprovalCommandHttpResult(network_error=True)

    status_code = getattr(response, "status_code", None)
    parsed_status: int | None
    try:
        parsed_status = int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        parsed_status = None

    body: object
    try:
        json_loader = getattr(response, "json", None)
        body = json_loader() if callable(json_loader) else {}
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    return ApprovalCommandHttpResult(
        network_error=False,
        status_code=parsed_status,
        effective_status=_optional_str(body.get("effective_status")),
        error_code=_optional_str(body.get("error_code")),
        idempotent=_optional_bool(body.get("idempotent")),
        decided_at=_optional_str(body.get("decided_at")),
    )


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _optional_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    return None
