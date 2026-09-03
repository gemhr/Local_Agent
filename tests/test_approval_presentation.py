"""Stage5-Phase7-WP4 桌面端 Tool Approval 展示与命令回归。"""

from __future__ import annotations

import os
from dataclasses import replace
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication

from core.approval_client import request_tool_approval_decision, tool_approval_decision_url
from core.approval_presentation import (
    ApprovalCommandHttpResult,
    ApprovalPresentationState,
    apply_decided,
    apply_http_result,
    begin_submit,
    expire_store,
    remember_requested,
)
from ui.chat_panel import ChatPanel


_RUN_ID = "run-1"
_APPROVAL_ID = "approval-1"
_DIGEST = "a" * 64


def _requested() -> dict:
    return {
        "event_type": "TOOL_APPROVAL_REQUESTED",
        "run_id": _RUN_ID,
        "payload": {
            "approval_id": _APPROVAL_ID,
            "invocation_binding_digest": _DIGEST,
            "tool_name": "write_file",
            "risk_level": "HIGH",
            "risk_facts": "LOCAL_STATE_MUTATION|NON_IDEMPOTENT",
        },
    }


def _model():
    models = {}
    return remember_requested(models, _requested()), models


def test_requested_card_model_is_idempotent_and_has_safe_risk_details():
    model, models = _model()
    assert model is not None
    assert model.tool_name == "write_file"
    assert model.risk_level == "HIGH"
    assert model.risk_facts == ("LOCAL_STATE_MUTATION", "NON_IDEMPOTENT")
    assert model.buttons_enabled
    assert remember_requested(models, _requested()) is None
    assert len(models) == 1


def test_approve_and_reject_routes_only_send_frozen_binding_digest():
    calls: list[tuple[str, dict, float]] = []

    def post(url, *, json, timeout):
        calls.append((url, json, timeout))
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"effective_status": "APPROVED", "idempotent": False},
        )

    approve = tool_approval_decision_url("http://localhost:8000/", _RUN_ID, _APPROVAL_ID, "approve")
    reject = tool_approval_decision_url("http://localhost:8000", _RUN_ID, _APPROVAL_ID, "reject")
    assert approve.endswith(f"/{_APPROVAL_ID}/approve")
    assert reject.endswith(f"/{_APPROVAL_ID}/reject")
    result = request_tool_approval_decision(post, approve, _DIGEST)
    assert result.effective_status == "APPROVED"
    assert calls == [(approve, {"invocation_binding_digest": _DIGEST}, 5.0)]


def test_double_click_and_http_stream_races_keep_terminal_state():
    model, _ = _model()
    assert model is not None
    submitting, should_send = begin_submit(model)
    assert should_send and submitting.state is ApprovalPresentationState.SUBMITTING
    assert begin_submit(submitting)[1] is False

    decided_first = apply_decided(submitting, "APPROVED")
    late_http = apply_http_result(
        decided_first, ApprovalCommandHttpResult(network_error=False, status_code=200, effective_status="APPROVED")
    )
    assert late_http.state is ApprovalPresentationState.APPROVED
    assert apply_http_result(
        decided_first, ApprovalCommandHttpResult(network_error=True)
    ).state is ApprovalPresentationState.APPROVED

    http_first = apply_http_result(
        submitting, ApprovalCommandHttpResult(network_error=False, status_code=200, effective_status="APPROVED")
    )
    assert apply_decided(http_first, "APPROVED").state is ApprovalPresentationState.APPROVED

    invalidated_first = apply_decided(submitting, "INVALIDATED_TIMEOUT")
    assert apply_http_result(
        invalidated_first,
        ApprovalCommandHttpResult(network_error=False, status_code=200, effective_status="APPROVED"),
    ).state is ApprovalPresentationState.EXPIRED


def test_http_error_conflict_expiry_and_terminal_cleanup_are_safe():
    model, models = _model()
    assert model is not None
    submitting, _ = begin_submit(model)
    conflict = apply_http_result(
        submitting, ApprovalCommandHttpResult(network_error=False, status_code=409, error_code="APPROVAL_DECISION_CONFLICT")
    )
    assert conflict.state is ApprovalPresentationState.ERROR
    assert "CONFLICT" not in conflict.message
    expired = apply_http_result(
        submitting, ApprovalCommandHttpResult(network_error=False, status_code=410, error_code="APPROVAL_INVALIDATED")
    )
    assert expired.state is ApprovalPresentationState.EXPIRED and not expired.buttons_enabled
    models[model.key] = submitting
    other_run = replace(model, run_id="run-2")
    models[other_run.key] = other_run
    assert expire_store(models, _RUN_ID)[0].state is ApprovalPresentationState.EXPIRED
    assert models[other_run.key].state is ApprovalPresentationState.PENDING


@pytest.mark.parametrize(
    ("decision_status", "expected"),
    [
        ("APPROVED", ApprovalPresentationState.APPROVED),
        ("REJECTED", ApprovalPresentationState.REJECTED),
        ("INVALIDATED_CANCELLED", ApprovalPresentationState.EXPIRED),
        ("INVALIDATED_TIMEOUT", ApprovalPresentationState.EXPIRED),
    ],
)
def test_decided_event_statuses_have_terminal_presentation(decision_status, expected):
    model, _ = _model()
    assert model is not None
    updated = apply_decided(model, decision_status)
    assert updated.state is expected
    assert not updated.buttons_enabled


def test_network_failure_allows_a_deliberate_retry():
    model, _ = _model()
    assert model is not None
    submitting, _ = begin_submit(model)
    failed = apply_http_result(submitting, ApprovalCommandHttpResult(network_error=True))
    assert failed.state is ApprovalPresentationState.ERROR
    assert failed.buttons_enabled
    assert begin_submit(failed)[1] is True


def test_chat_panel_creates_one_card_submits_once_and_applies_decided_event():
    app = QApplication.instance() or QApplication([])
    panel = ChatPanel("http://localhost:8000")
    commands: list[tuple[object, str]] = []
    panel.approval_command_requested.connect(lambda model, action: commands.append((model, action)))

    assert panel.handle_approval_event(_requested(), "core_router")
    assert panel.handle_approval_event(_requested(), "core_router")
    assert len(panel.approval_widgets) == 1
    card = panel.approval_widgets[(_RUN_ID, _APPROVAL_ID)]
    card.approve_button.click()
    card.approve_button.click()
    assert len(commands) == 1 and commands[0][1] == "approve"
    assert panel.approval_models[(_RUN_ID, _APPROVAL_ID)].state is ApprovalPresentationState.SUBMITTING

    decided = {
        "event_type": "TOOL_APPROVAL_DECIDED",
        "run_id": _RUN_ID,
        "payload": {"approval_id": _APPROVAL_ID, "decision_status": "APPROVED"},
    }
    assert panel.handle_approval_event(decided, "core_router")
    assert panel.approval_models[(_RUN_ID, _APPROVAL_ID)].state is ApprovalPresentationState.APPROVED
    assert not card.approve_button.isEnabled()
    panel.deleteLater()
    app.processEvents()
