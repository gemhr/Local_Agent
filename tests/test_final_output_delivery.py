"""WP4 Shape 0-3: unique user-visible final output through the typed path."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

import pytest

from core.memory_manager import MemoryManager
from core.runtime import (
    CoordinatedRuntimeFactory,
    HistoryPolicy,
    RunStatus,
    RuntimeEventType,
)
from tests._wp3_fixtures import (
    Wp3RecordingRouter,
    delegated_json,
    direct_json,
    make_wp3_services,
    shape3_planning_json,
)
from tests.test_wp3_history_boundary import (
    FakeModel,
    make_real_router,
    make_run_context,
)


def records(services, run_id: str):
    return services.event_journal.read_after(run_id, 0, 1000)


def output_delta_digests(services, run_id: str) -> list[str]:
    """Journal keeps only the safe SHA-256 digest of the delivered text."""
    return [
        item.safe_payload["text_digest"]
        for item in records(services, run_id)
        if item.event_type is RuntimeEventType.OUTPUT_DELTA
    ]


def digest_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.asyncio
async def test_shape0_core_direct_uses_typed_pipeline_and_single_output() -> None:
    services = make_wp3_services()
    with tempfile.TemporaryDirectory() as directory:
        memory = MemoryManager(str(Path(directory) / "memory.db"))
        model = FakeModel(planning_json=direct_json("core_router"))
        router = make_real_router(memory, model=model)
        scope = await CoordinatedRuntimeFactory(
            router, services
        ).create_run_scope("core_router", "direct core question")

        result = await scope.execute()

        assert result.status is RunStatus.SUCCEEDED
        assert result.error_code is None
        assert result.succeeded_step_ids == ("answer",)
        types = [item.event_type for item in records(services, scope.run_id)]
        assert types.count(RuntimeEventType.OUTPUT_DELTA) == 1
        assert types.index(RuntimeEventType.OUTPUT_DELTA) < types.index(
            RuntimeEventType.STEP_COMPLETED
        )
        assert "FINAL_OUTPUT_PIPELINE_NOT_READY" not in repr(result)
        # Core direct keeps AGENT_SCOPE history read behavior.
        assert model.all_messages
        assert digest_of("result-core_router") in output_delta_digests(
            services, scope.run_id
        )
        # Delivered final persisted once under the entry agent's direct scope.
        assert memory.count_messages("core_router") == 2
        await scope.close()


@pytest.mark.asyncio
async def test_shape1_explicit_entry_specialist_single_output() -> None:
    services = make_wp3_services()
    with tempfile.TemporaryDirectory() as directory:
        memory = MemoryManager(str(Path(directory) / "memory.db"))
        model = FakeModel(planning_json=direct_json("code_expert"))
        router = make_real_router(memory, model=model)
        scope = await CoordinatedRuntimeFactory(
            router, services
        ).create_run_scope("code_expert", "explicit code request")

        result = await scope.execute()

        assert result.status is RunStatus.SUCCEEDED
        assert result.succeeded_step_ids == ("answer",)
        types = [item.event_type for item in records(services, scope.run_id)]
        assert types.count(RuntimeEventType.OUTPUT_DELTA) == 1
        assert digest_of("result-code_expert") in output_delta_digests(
            services, scope.run_id
        )
        assert "FINAL_OUTPUT_PIPELINE_NOT_READY" not in repr(result)
        # Explicit entry keeps AGENT_SCOPE history read behavior.
        assert model.all_messages
        assert memory.count_messages("code_expert") == 2
        await scope.close()


@pytest.mark.asyncio
async def test_shape1_delegated_knowledge_direct_single_output() -> None:
    services = make_wp3_services()
    router = Wp3RecordingRouter(
        delegated_json(
            task_ids=("knowledge",),
            synthesis_required=False,
        )
    )
    scope = await CoordinatedRuntimeFactory(
        router, services
    ).create_run_scope(
        "core_router",
        "调用知识专家，总结 cdt_field_mapping.md",
    )

    result = await scope.execute()

    assert result.status is RunStatus.SUCCEEDED
    assert result.succeeded_step_ids == ("task-knowledge",)
    assert router.planning_calls == 0
    calls = router.calls_for("knowledge_expert")
    assert len(calls) == 1
    # Delegated passthrough explicitly uses NONE history policy.
    assert calls[0][2].get("history_policy") is HistoryPolicy.NONE
    assert calls[0][2].get("persist") is False
    types = [item.event_type for item in records(services, scope.run_id)]
    assert types.count(RuntimeEventType.OUTPUT_DELTA) == 1
    assert digest_of("result-knowledge_expert") in output_delta_digests(
        services, scope.run_id
    )
    assert "FINAL_OUTPUT_PIPELINE_NOT_READY" not in repr(result)
    await scope.close()


@pytest.mark.asyncio
async def test_shape2_single_specialist_plus_synthesis_single_output() -> None:
    services = make_wp3_services()
    router = Wp3RecordingRouter(
        delegated_json(task_ids=("code",), synthesis_required=True),
        output_for={"synthesis_agent": "SHAPE2_FINAL_CANDIDATE"},
    )
    scope = await CoordinatedRuntimeFactory(
        router, services
    ).create_run_scope("core_router", "coordinate one review")

    result = await scope.execute()

    assert result.status is RunStatus.SUCCEEDED
    assert result.succeeded_step_ids == ("task-code", "synthesis")
    assert len(router.calls_for("code_expert")) == 1
    assert len(router.calls_for("synthesis_agent")) == 1
    assert len(router.calls_for("data_analyst")) == 0
    assert all(flag is False for flag in router.persist_flags())
    types = [item.event_type for item in records(services, scope.run_id)]
    assert types.count(RuntimeEventType.OUTPUT_DELTA) == 1
    output_digests = output_delta_digests(services, scope.run_id)
    assert digest_of("SHAPE2_FINAL_CANDIDATE") in output_digests
    assert digest_of("result-code_expert") not in output_digests
    assert "FINAL_OUTPUT_PIPELINE_NOT_READY" not in repr(result)
    await scope.close()


@pytest.mark.asyncio
async def test_shape3_fanout_specialists_plus_synthesis_single_output() -> None:
    services = make_wp3_services()
    router = Wp3RecordingRouter(
        shape3_planning_json(),
        output_for={"synthesis_agent": "SHAPE3_FINAL_CANDIDATE"},
    )
    scope = await CoordinatedRuntimeFactory(
        router, services
    ).create_run_scope("core_router", "coordinate two reviews")

    result = await scope.execute()

    assert result.status is RunStatus.SUCCEEDED
    assert result.succeeded_step_ids == (
        "task-code",
        "task-knowledge",
        "synthesis",
    )
    assert len(router.calls_for("code_expert")) == 1
    assert len(router.calls_for("knowledge_expert")) == 1
    assert len(router.calls_for("synthesis_agent")) == 1
    types = [item.event_type for item in records(services, scope.run_id)]
    assert types.count(RuntimeEventType.OUTPUT_DELTA) == 1
    output_digests = output_delta_digests(services, scope.run_id)
    assert digest_of("SHAPE3_FINAL_CANDIDATE") in output_digests
    assert digest_of("result-code_expert") not in output_digests
    assert digest_of("result-knowledge_expert") not in output_digests
    assert "FINAL_OUTPUT_PIPELINE_NOT_READY" not in repr(result)
    await scope.close()
