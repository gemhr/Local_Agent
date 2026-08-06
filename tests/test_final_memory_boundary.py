"""WP4 delivered-only final Memory boundary."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from core.memory_manager import MemoryManager
from core.runtime import (
    CoordinatedRuntimeFactory,
    FaultPoint,
    RunStatus,
    RuntimeEventType,
)
from tests._event_fault_fixtures import event_controller
from tests._wp3_fixtures import delegated_json, make_wp3_services
from tests.test_wp3_history_boundary import FakeModel, make_real_router


def delivered_specialist_content(memory: MemoryManager) -> list[str]:
    return [
        message["content"]
        for message in memory.get_chat_history("core_router", ascending=True)
        if message["role"] == "assistant"
    ]


@pytest.mark.asyncio
async def test_delivered_final_written_exactly_once_to_entry_scope() -> None:
    services = make_wp3_services()
    with tempfile.TemporaryDirectory() as directory:
        memory = MemoryManager(str(Path(directory) / "memory.db"))
        model = FakeModel(
            planning_json=delegated_json(
                task_ids=("code",),
                synthesis_required=True,
            )
        )
        router = make_real_router(memory, model=model)
        scope = await CoordinatedRuntimeFactory(
            router, services
        ).create_run_scope("core_router", "coordinate one review")

        result = await scope.execute()

        assert result.status is RunStatus.SUCCEEDED
        assert memory.count_messages("core_router") == 2
        roles = [
            message["role"]
            for message in memory.get_chat_history(
                "core_router", ascending=True
            )
        ]
        assert roles == ["user", "assistant"]
        # Only the delivered final assistant text is persisted.
        assert delivered_specialist_content(memory) == ["FINAL-SYNTHESIS"]
        assert memory.count_messages("code_expert") == 0
        assert memory.count_messages("synthesis_agent") == 0
        await scope.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault_point", "expected_code"),
    [
        (FaultPoint.EVENT_BEFORE_JOURNAL_APPEND, "FINAL_OUTPUT_DELIVERY_FAILED"),
        (FaultPoint.EVENT_AFTER_JOURNAL_APPEND, "FINAL_OUTPUT_DELIVERY_UNKNOWN"),
    ],
)
async def test_failed_and_unknown_final_never_written_to_memory(
    fault_point, expected_code
) -> None:
    services = make_wp3_services()
    with tempfile.TemporaryDirectory() as directory:
        memory = MemoryManager(str(Path(directory) / "memory.db"))
        model = FakeModel(
            planning_json=delegated_json(
                task_ids=("code",),
                synthesis_required=True,
            )
        )
        router = make_real_router(memory, model=model)
        controller = event_controller(
            fault_point,
            event_type=RuntimeEventType.OUTPUT_DELTA,
        )
        scope = await CoordinatedRuntimeFactory(
            router, services
        ).create_run_scope(
            "core_router",
            "coordinate one review",
            fault_controller=controller,
        )

        result = await scope.execute()

        assert result.status is RunStatus.FAILED
        assert result.error_code == expected_code
        assert memory.count_messages("core_router") == 0
        assert delivered_specialist_content(memory) == []
        await scope.close()


@pytest.mark.asyncio
async def test_specialist_raw_never_enters_memory() -> None:
    services = make_wp3_services()
    with tempfile.TemporaryDirectory() as directory:
        memory = MemoryManager(str(Path(directory) / "memory.db"))
        model = FakeModel(
            planning_json=delegated_json(
                task_ids=("code",),
                synthesis_required=True,
            )
        )
        router = make_real_router(memory, model=model)
        scope = await CoordinatedRuntimeFactory(
            router, services
        ).create_run_scope(
            "core_router",
            "coordinate one review",
            persist=False,
        )

        result = await scope.execute()

        assert result.status is RunStatus.SUCCEEDED
        assert memory.count_messages("core_router") == 0
        assert memory.count_messages("code_expert") == 0
        assert memory.count_messages("synthesis_agent") == 0
        assert delivered_specialist_content(memory) == []
        await scope.close()
