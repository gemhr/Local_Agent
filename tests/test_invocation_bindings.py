from dataclasses import asdict
import pickle

import pytest

from core.runtime.invocation_bindings import (
    AgentInvocationSpec,
    InvocationBindingError,
    InvocationBindingErrorCode,
    StepInvocationBindings,
)


RAW = "private instruction: C:/sensitive/file.md"


def test_bindings_resolve_only_one_expected_step_and_are_repr_safe() -> None:
    spec = AgentInvocationSpec("task-knowledge", "knowledge_expert", RAW)
    bindings = StepInvocationBindings((spec,))
    assert bindings.step_ids == ("task-knowledge",)
    assert bindings.resolve_for_step(
        "task-knowledge", expected_agent_id="knowledge_expert"
    ).instruction == RAW
    assert RAW not in repr(spec)
    assert RAW not in repr(bindings)
    assert not hasattr(bindings, "get_all")


def test_unknown_step_agent_mismatch_and_closed_reads_fail_safely() -> None:
    bindings = StepInvocationBindings(
        (AgentInvocationSpec("answer", "core_router", RAW),)
    )
    with pytest.raises(InvocationBindingError) as unknown:
        bindings.resolve_for_step("missing")
    assert unknown.value.error_code is InvocationBindingErrorCode.UNKNOWN_STEP
    with pytest.raises(InvocationBindingError) as mismatch:
        bindings.resolve_for_step("answer", expected_agent_id="code_expert")
    assert mismatch.value.error_code is InvocationBindingErrorCode.AGENT_MISMATCH
    assert RAW not in str(unknown.value) + str(mismatch.value)
    bindings.close_and_clear()
    bindings.close_and_clear()
    assert bindings.closed
    assert bindings._bindings == {}
    with pytest.raises(InvocationBindingError) as closed:
        bindings.resolve_for_step("answer")
    assert closed.value.error_code is InvocationBindingErrorCode.BINDINGS_CLOSED


def test_bindings_reject_duplicates_mutation_asdict_and_serialization() -> None:
    first = AgentInvocationSpec("answer", "core_router", RAW)
    with pytest.raises(InvocationBindingError) as duplicate:
        StepInvocationBindings((first, AgentInvocationSpec("answer", "core_router", "other")))
    assert duplicate.value.error_code is InvocationBindingErrorCode.DUPLICATE_STEP
    with pytest.raises(AttributeError):
        first.instruction = "changed"
    with pytest.raises(TypeError):
        asdict(first)
    bindings = StepInvocationBindings((first,))
    with pytest.raises(TypeError):
        pickle.dumps(bindings)
