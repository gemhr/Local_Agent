from dataclasses import fields
from datetime import UTC, datetime

import pytest

from core.runtime import (
    FaultAction,
    FaultInjectionController,
    FaultInjectionRecorder,
    FaultMatchContext,
    FaultPlan,
    FaultPoint,
    FaultRule,
    FaultScope,
    FaultTrigger,
    InjectedFaultCode,
    InjectedFaultError,
)

MARKERS = (
    "SECRET_PROMPT_TEXT",
    "MODEL_OUTPUT_SECRET",
    "TOOL_ARGUMENT_SECRET",
    "TOOL_OUTPUT_SECRET",
    "RAG_CHUNK_SECRET",
    "MEMORY_SECRET",
    "C:\\Users\\private-user",
    "provider-secret-error",
)
NOW = datetime(2026, 7, 31, tzinfo=UTC)


def safe_rule() -> FaultRule:
    return FaultRule(
        rule_id="safe-rule",
        fault_point=FaultPoint.MODEL_BEFORE_PROVIDER_CALL,
        action=FaultAction.RAISE_TYPED_ERROR,
        trigger=FaultTrigger.FIRST_MATCH,
        scope=FaultScope.INVOCATION_SCOPE,
        max_hits=1,
        component="model",
        safe_fault_code=InjectedFaultCode.INJECTED_PERMANENT_FAILURE,
    )


def assert_markers_absent(value: object) -> None:
    rendered = str(value)
    for marker in MARKERS:
        assert marker not in rendered


def test_context_exposes_no_arbitrary_metadata_or_sensitive_content_fields() -> None:
    names = {field.name for field in fields(FaultMatchContext)}
    denied = {
        "metadata",
        "prompt",
        "messages",
        "tool_arguments",
        "tool_output",
        "query",
        "memory",
        "path",
        "api_key",
        "provider_url",
        "exception",
        "runtime",
        "agent_state",
    }
    assert names.isdisjoint(denied)


@pytest.mark.parametrize("marker", MARKERS)
def test_sensitive_text_cannot_enter_safe_token_fields_or_error_messages(
    marker: str,
) -> None:
    with pytest.raises(ValueError) as exc:
        FaultMatchContext(
            fault_point=FaultPoint.MODEL_BEFORE_PROVIDER_CALL,
            component=marker,
        )
    assert marker not in str(exc.value)


def test_plan_json_digest_repr_decision_recorder_and_exception_are_content_free(
    caplog,
) -> None:
    plan = FaultPlan("safe-plan", (safe_rule(),), created_at=NOW)
    recorder = FaultInjectionRecorder(capacity=1)
    controller = FaultInjectionController.for_test(plan, recorder=recorder)
    context = FaultMatchContext(
        fault_point=FaultPoint.MODEL_BEFORE_PROVIDER_CALL,
        component="model",
    )
    decision = controller.evaluate(context)
    record_snapshot = recorder.snapshot()
    error = InjectedFaultError(InjectedFaultCode.INJECTED_PERMANENT_FAILURE)

    for value in (
        plan.to_safe_json(),
        plan.digest,
        repr(plan),
        repr(safe_rule()),
        repr(context),
        repr(decision),
        repr(record_snapshot),
        repr(recorder),
        repr(controller),
        str(error),
        repr(error),
        caplog.text,
    ):
        assert_markers_absent(value)
    assert set(plan.to_safe_dict()) == {
        "plan_id",
        "schema_version",
        "created_at",
        "rules",
    }


@pytest.mark.parametrize("field", ["plan_id", "component"])
def test_recorder_rejects_sensitive_manual_identity_without_echo(field: str) -> None:
    recorder = FaultInjectionRecorder(capacity=1)
    plan = FaultPlan("safe-plan", (safe_rule(),), created_at=NOW)
    decision = FaultInjectionController.for_test(plan).evaluate(
        FaultMatchContext(
            fault_point=FaultPoint.MODEL_BEFORE_PROVIDER_CALL,
            component="model",
        )
    )
    values = {
        "plan_id": "safe-plan",
        "component": "model",
        "decision": decision,
    }
    values[field] = "SECRET_PROMPT_TEXT"
    with pytest.raises(ValueError) as exc:
        recorder.record(**values)
    assert "SECRET_PROMPT_TEXT" not in str(exc.value)


def test_foundation_has_no_settings_http_runtime_event_or_store_dependency() -> None:
    import ast
    import inspect
    import core.runtime.fault_injection as implementation
    import core.runtime.fault_injection_contract as contract
    import core.runtime.fault_injection_recording as recording

    imported_modules: set[str] = set()
    for module in (contract, implementation, recording):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
    denied_modules = {
        "core.settings",
        "fastapi",
        "random",
    }
    assert imported_modules.isdisjoint(denied_modules)
    source = inspect.getsource(implementation)
    assert "ContextVar(" not in source
    assert "os.environ" not in source
