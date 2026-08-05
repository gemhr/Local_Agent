"""SynthesisAgentAdapter contract: whitelist input, strict prompt, fail-closed."""

from __future__ import annotations

import pytest

from core.runtime import (
    AgentExecutionRequest,
    DependencyResultEntry,
    DependencyResultView,
    ExecutionKind,
    ResultContentType,
    SynthesisAgentAdapter,
    SynthesisInputError,
    SynthesisInputErrorCode,
    TaskCapabilityRequirements,
)


class _RecordingRouter:
    def __init__(self, output: str = "final candidate") -> None:
        self.output = output
        self.calls: list[tuple[str, str, dict]] = []
        self.fail = False

    def complete_single_agent(self, agent_id: str, query: str, **kwargs) -> str:
        self.calls.append((agent_id, query, kwargs))
        if self.fail:
            raise RuntimeError("simulated synthesis failure")
        return self.output


def _view(*entries):
    return DependencyResultView(entries)


def _entry(step_id: str, agent_id: str, content: str, *, complete: bool = True):
    return DependencyResultEntry(
        step_id,
        agent_id,
        ResultContentType.TEXT,
        content,
        complete,
    )


def _request(router, view):
    return AgentExecutionRequest(
        step_id="synthesis",
        agent_id="synthesis_agent",
        instruction="Synthesize all explicitly required specialist results.",
        execution_kind=ExecutionKind.SYNTHESIS,
        input_type="text",
        capability_requirements=TaskCapabilityRequirements(),
        dependency_results=view,
    )


def test_model_call_equals_one_when_all_results_present() -> None:
    router = _RecordingRouter()
    adapter = SynthesisAgentAdapter(router)
    view = _view(
        _entry("task-code", "code_expert", "code finding"),
        _entry("task-knowledge", "knowledge_expert", "knowledge finding"),
    )
    result = adapter.execute(_request(router, view), object())
    assert len(router.calls) == 1
    assert result.content == "final candidate"
    assert result.complete is True


def test_prompt_contains_provided_results_in_stable_order() -> None:
    router = _RecordingRouter()
    adapter = SynthesisAgentAdapter(router)
    view = _view(
        _entry("task-code", "code_expert", "CODE_BODY"),
        _entry("task-knowledge", "knowledge_expert", "KNOWLEDGE_BODY"),
    )
    adapter.execute(_request(router, view), object())
    prompt = router.calls[0][1]
    assert prompt.index("CODE_BODY") < prompt.index("KNOWLEDGE_BODY")
    assert "code_expert" in prompt
    assert "knowledge_expert" in prompt
    assert "Task instruction" in prompt


def test_prompt_never_contains_unlisted_agents() -> None:
    router = _RecordingRouter()
    adapter = SynthesisAgentAdapter(router)
    view = _view(_entry("task-code", "code_expert", "CODE_BODY"))
    adapter.execute(_request(router, view), object())
    prompt = router.calls[0][1]
    assert "data_analyst" not in prompt
    assert "core_router" not in prompt


def test_missing_view_means_zero_model_calls() -> None:
    router = _RecordingRouter()
    adapter = SynthesisAgentAdapter(router)
    request = AgentExecutionRequest(
        step_id="synthesis",
        agent_id="synthesis_agent",
        instruction="synthesize",
        execution_kind=ExecutionKind.SYNTHESIS,
        input_type="text",
        capability_requirements=TaskCapabilityRequirements(),
        dependency_results=None,
    )
    with pytest.raises(SynthesisInputError) as exc_info:
        adapter.execute(request, object())
    assert (
        exc_info.value.error_code
        is SynthesisInputErrorCode.MISSING_DEPENDENCIES
    )
    assert router.calls == []


def test_incomplete_required_result_means_zero_model_calls() -> None:
    router = _RecordingRouter()
    adapter = SynthesisAgentAdapter(router)
    view = _view(
        _entry("task-code", "code_expert", "partial", complete=False)
    )
    with pytest.raises(SynthesisInputError) as exc_info:
        adapter.execute(_request(router, view), object())
    assert exc_info.value.error_code is SynthesisInputErrorCode.INCOMPLETE_RESULT
    assert router.calls == []


def test_empty_view_means_zero_model_calls() -> None:
    router = _RecordingRouter()
    adapter = SynthesisAgentAdapter(router)
    with pytest.raises(SynthesisInputError) as exc_info:
        adapter.execute(_request(router, _view()), object())
    assert (
        exc_info.value.error_code
        is SynthesisInputErrorCode.MISSING_DEPENDENCIES
    )
    assert router.calls == []


def test_synthesis_failure_has_no_fallback_or_second_call() -> None:
    router = _RecordingRouter()
    router.fail = True
    adapter = SynthesisAgentAdapter(router)
    view = _view(_entry("task-code", "code_expert", "code finding"))
    with pytest.raises(SynthesisInputError) as exc_info:
        adapter.execute(_request(router, view), object())
    assert (
        exc_info.value.error_code
        is SynthesisInputErrorCode.SYNTHESIS_MODEL_FAILED
    )
    assert len(router.calls) == 1


def test_synthesis_call_uses_persist_false_and_no_memory() -> None:
    router = _RecordingRouter()
    adapter = SynthesisAgentAdapter(router)
    view = _view(_entry("task-code", "code_expert", "code finding"))
    adapter.execute(_request(router, view), object())
    kwargs = router.calls[0][2]
    assert kwargs["persist"] is False
    assert not hasattr(adapter, "memory_manager")
    assert not hasattr(adapter, "journal")
    assert not hasattr(adapter, "store")


def test_synthesis_adapter_has_no_full_store_interface() -> None:
    adapter = SynthesisAgentAdapter(_RecordingRouter())
    for forbidden in ("get_all", "dependency_view_for", "write_prepared"):
        assert not hasattr(adapter, forbidden)


def test_raw_content_not_in_exception() -> None:
    secret = "SECRET_SPECIALIST_RESULT_DO_NOT_PERSIST"
    router = _RecordingRouter()
    router.fail = True
    adapter = SynthesisAgentAdapter(router)
    view = _view(_entry("task-code", "code_expert", secret))
    try:
        adapter.execute(_request(router, view), object())
    except SynthesisInputError as exc:
        rendered = f"{exc!r} {str(exc)}"
        assert secret not in rendered
    else:
        pytest.fail("expected SynthesisInputError")
