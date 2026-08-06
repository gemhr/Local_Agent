"""WP6 security matrix on failure paths: specialist fail / delivery unknown /
memory fail, plus exception-message and path scanning."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from core.memory_manager import MemoryManager
from core.runtime import (
    CoordinatedRuntimeFactory,
    FaultPoint,
    InMemoryMetricsRecorder,
    InMemorySnapshotStore,
    InMemorySpanRecorder,
    RunStatus,
    RuntimeEventType,
)
from core.runtime.multi_agent_status import format_frontend_status
from tests._stage2_5_wp6_fixtures import wp6_controller
from tests._wp3_fixtures import (
    Wp3RecordingRouter,
    delegated_json,
    make_wp3_services,
    shape3_planning_json,
)
from tests.test_wp3_history_boundary import FakeModel, make_real_router


SECRET_USER = "SECRET_USER_INSTRUCTION"
SECRET_SPECIALIST = "SECRET_SPECIALIST_RESULT"
SECRET_SYNTHESIS_INPUT = "SECRET_SYNTHESIS_INPUT"
SECRET_FINAL = "SECRET_FINAL_OUTPUT"
SECRET_EXCEPTION = "SECRET_EXCEPTION_MESSAGE"
SECRET_PATH = r"\\internal\private\case.dat"
ALL_SECRETS = (
    SECRET_USER,
    SECRET_SPECIALIST,
    SECRET_SYNTHESIS_INPUT,
    SECRET_FINAL,
    SECRET_EXCEPTION,
    SECRET_PATH,
)


class FailingRouter(Wp3RecordingRouter):
    """Raises an exception whose message carries a secret marker."""

    def complete_single_agent(self, agent_id, query, **kwargs):
        if agent_id in self.fail_agents:
            raise RuntimeError(f"{SECRET_EXCEPTION} provider exploded")
        return super().complete_single_agent(agent_id, query, **kwargs)


class SecretFinalModel(FakeModel):
    """FakeModel 变体：synthesis 产出带 SECRET_FINAL 标记的正文。"""

    def generate(self, messages, **kwargs):
        for part in super().generate(messages, **kwargs):
            if part == "FINAL-SYNTHESIS":
                yield SECRET_FINAL
            else:
                yield part


def _journal_text(services, run_id: str) -> str:
    return "\n".join(
        repr(record) + str(record.safe_payload)
        for record in services.event_journal.read_after(run_id, 0, 1000)
    )


def _assert_no_secrets_anywhere(
    *, text: str, allow: tuple[str, ...] = ()
) -> None:
    for secret in ALL_SECRETS:
        if secret in allow:
            continue
        assert secret not in text, f"{secret} leaked"


async def _run(
    services,
    router,
    *,
    controller=None,
    query: str = f"{SECRET_USER} {SECRET_PATH}",
):
    scope = await CoordinatedRuntimeFactory(
        router, services
    ).create_run_scope(
        "core_router",
        query,
        fault_controller=controller,
    )
    result = await scope.execute()
    return scope, result


def _assert_common_channels_clean(
    *,
    services,
    scope,
    recorder,
    metrics,
    snapshot_store,
    caplog,
    allow: tuple[str, ...] = (),
) -> None:
    journal_text = _journal_text(services, scope.run_id)
    _assert_no_secrets_anywhere(text=journal_text, allow=allow)
    _assert_no_secrets_anywhere(text=repr(recorder.snapshot()), allow=allow)
    _assert_no_secrets_anywhere(text=repr(metrics.snapshot()), allow=allow)
    _assert_no_secrets_anywhere(
        text=repr(snapshot_store.list_for_run(scope.run_id, 10)),
        allow=allow,
    )
    # 日志（caplog）与异常/repr 不得泄漏。
    _assert_no_secrets_anywhere(text=caplog.text, allow=allow)
    _assert_no_secrets_anywhere(text=repr(services.run_registry), allow=allow)


@pytest.mark.asyncio
async def test_specialist_failure_security_matrix(caplog) -> None:
    recorder = InMemorySpanRecorder()
    metrics = InMemoryMetricsRecorder()
    snapshot_store = InMemorySnapshotStore()
    services = make_wp3_services(
        span_recorder=recorder,
        runtime_metrics_recorder=metrics,
        snapshot_store=snapshot_store,
        snapshot_enabled=True,
    )
    router = FailingRouter(
        shape3_planning_json(),
        fail_agents=("knowledge_expert",),
        output_for={
            "code_expert": f"{SECRET_SPECIALIST}-code",
            "knowledge_expert": f"{SECRET_SPECIALIST}-knowledge",
            "synthesis_agent": SECRET_FINAL,
        },
    )
    scope, result = await _run(services, router)

    assert result.status is RunStatus.FAILED
    types = [item.event_type for item in services.event_journal.read_after(
        scope.run_id, 0, 1000
    )]
    assert RuntimeEventType.OUTPUT_DELTA not in types
    assert scope.coordinator.output_gate is not None
    assert not scope.coordinator.output_gate.attempted
    _assert_common_channels_clean(
        services=services,
        scope=scope,
        recorder=recorder,
        metrics=metrics,
        snapshot_store=snapshot_store,
        caplog=caplog,
    )
    # Memory 未写（失败路径不写）。
    assert router.memory_manager.count_messages("core_router") == 0
    await scope.close()


@pytest.mark.asyncio
async def test_delivery_unknown_security_matrix(caplog) -> None:
    recorder = InMemorySpanRecorder()
    metrics = InMemoryMetricsRecorder()
    snapshot_store = InMemorySnapshotStore()
    services = make_wp3_services(
        span_recorder=recorder,
        runtime_metrics_recorder=metrics,
        snapshot_store=snapshot_store,
        snapshot_enabled=True,
    )
    router = Wp3RecordingRouter(
        shape3_planning_json(),
        output_for={
            "code_expert": f"{SECRET_SPECIALIST}-code",
            "knowledge_expert": f"{SECRET_SPECIALIST}-knowledge",
            "synthesis_agent": SECRET_FINAL,
        },
    )
    controller = wp6_controller(
        FaultPoint.EVENT_BEFORE_CHANNEL_ENQUEUE,
        component="event_channel",
        operation_kind="CHANNEL_ENQUEUE",
        event_type=RuntimeEventType.OUTPUT_DELTA,
    )
    scope, result = await _run(
        services, router, controller=controller
    )

    assert result.status is RunStatus.FAILED
    assert result.error_code == "FINAL_OUTPUT_DELIVERY_UNKNOWN"
    types = [item.event_type for item in services.event_journal.read_after(
        scope.run_id, 0, 1000
    )]
    assert types.count(RuntimeEventType.OUTPUT_DELTA) == 1
    # Journal 只保存 digest/length；正文不得进入任何观测通道。
    journal_text = _journal_text(services, scope.run_id)
    assert SECRET_FINAL not in journal_text
    _assert_common_channels_clean(
        services=services,
        scope=scope,
        recorder=recorder,
        metrics=metrics,
        snapshot_store=snapshot_store,
        caplog=caplog,
    )
    # unknown 不写 Memory。
    assert router.memory_manager.count_messages("core_router") == 0
    # 前端文案不得鼓励立即重试。
    run_completed = next(
        item
        for item in services.event_journal.read_after(
            scope.run_id, 0, 1000
        )
        if item.event_type is RuntimeEventType.RUN_COMPLETED
    )
    text = format_frontend_status(
        {
            "event_type": "RUN_COMPLETED",
            "payload": dict(run_completed.safe_payload),
        }
    )
    assert "避免重复执行" in (text or "")
    assert "重试" not in (text or "")
    await scope.close()


@pytest.mark.asyncio
async def test_memory_failure_security_matrix(caplog) -> None:
    recorder = InMemorySpanRecorder()
    metrics = InMemoryMetricsRecorder()
    snapshot_store = InMemorySnapshotStore()
    services = make_wp3_services(
        span_recorder=recorder,
        runtime_metrics_recorder=metrics,
        snapshot_store=snapshot_store,
        snapshot_enabled=True,
    )
    with tempfile.TemporaryDirectory() as directory:
        controller = wp6_controller(
            FaultPoint.MEMORY_BEFORE_EXCHANGE_BEGIN,
            component="memory_manager",
            operation_kind="EXCHANGE_BEGIN",
        )
        memory = MemoryManager(
            str(Path(directory) / "memory.db"),
            fault_controller=controller,
        )
        model = SecretFinalModel(
            planning_json=delegated_json(
                task_ids=("code",),
                synthesis_required=True,
            )
        )
        router = make_real_router(memory, model=model)
        scope, result = await _run(
            services, router
        )

        assert result.status is RunStatus.FAILED
        assert result.error_code == "FINAL_OUTPUT_MEMORY_COMMIT_FAILED"
        types = [
            item.event_type
            for item in services.event_journal.read_after(
                scope.run_id, 0, 1000
            )
        ]
        assert types.count(RuntimeEventType.OUTPUT_DELTA) == 1
        # 唯一允许正文出现的通道：OUTPUT 事件（Journal 仅 digest）。
        journal_text = _journal_text(services, scope.run_id)
        assert SECRET_FINAL not in journal_text
        _assert_common_channels_clean(
            services=services,
            scope=scope,
            recorder=recorder,
            metrics=metrics,
            snapshot_store=snapshot_store,
            caplog=caplog,
        )
        # Memory 失败后不写入任何 exchange。
        assert memory.count_messages("core_router") == 0
        await scope.close()
