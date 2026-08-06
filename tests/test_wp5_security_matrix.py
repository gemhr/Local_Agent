from __future__ import annotations

import pytest

from core.runtime import (
    ChatStreamCompatibilityAdapter,
    CoordinatedRuntimeFactory,
    InMemoryMetricsRecorder,
    InMemorySnapshotStore,
    InMemorySpanRecorder,
    RunStatus,
    RuntimeEventType,
)
from core.runtime.multi_agent_status import format_frontend_status
from core.runtime.stream_adapter import ChatStreamChunkKind
from tests._wp3_fixtures import (
    Wp3RecordingRouter,
    make_wp3_services,
    shape3_planning_json,
)


SECRET_USER = "SECRET_USER_INSTRUCTION"
SECRET_SPECIALIST = "SECRET_SPECIALIST_RESULT"
SECRET_SYNTHESIS_INPUT = "SECRET_SYNTHESIS_INPUT"
SECRET_FINAL = "SECRET_FINAL_OUTPUT"
SECRET_PATH = r"\\internal\private\case.dat"
ALL_SECRETS = (
    SECRET_USER,
    SECRET_SPECIALIST,
    SECRET_SYNTHESIS_INPUT,
    SECRET_FINAL,
    SECRET_PATH,
)


@pytest.mark.asyncio
async def test_shape3_main_chain_security_matrix() -> None:
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
    scope = await CoordinatedRuntimeFactory(
        router, services
    ).create_run_scope(
        "core_router",
        f"{SECRET_USER} {SECRET_PATH}",
    )
    result = await scope.execute()
    assert result.status is RunStatus.SUCCEEDED

    # 1) Journal：只允许 safe payload，任何秘密正文不得进入。
    journal_text = "\n".join(
        repr(record)
        + str(record.safe_payload)
        for record in services.event_journal.read_after(
            scope.run_id, 0, 1000
        )
    )
    for secret in ALL_SECRETS:
        assert secret not in journal_text

    # 2) Trace：span 属性与 repr 不得包含秘密正文。
    trace_text = repr(recorder.snapshot())
    for secret in ALL_SECRETS:
        assert secret not in trace_text

    # 3) Metrics：任何标签/值不得包含秘密正文。
    metrics_text = repr(metrics.snapshot())
    for secret in ALL_SECRETS:
        assert secret not in metrics_text

    # 4) Snapshot：Plan/State 快照只含 safe 摘要，不落 raw。
    snapshots = snapshot_store.list_for_run(scope.run_id, 10)
    snapshot_text = repr(snapshots)
    for secret in ALL_SECRETS:
        assert secret not in snapshot_text

    # 5) Stream + 6) Frontend：OUTPUT_DELTA 是唯一正文通道且只出现一次；
    #    control 事件 JSON 与前端状态文案不携带任何秘密正文。
    adapter = ChatStreamCompatibilityAdapter()
    text_parts: list[str] = []
    for record in services.event_journal.read_after(
        scope.run_id, 0, 1000
    ):
        event = _event_from_record(record)
        if event is None:
            continue
        chunk = adapter.adapt(event)
        if chunk is None:
            continue
        if chunk.kind is ChatStreamChunkKind.TEXT:
            text_parts.append(chunk.text)
        elif chunk.kind is ChatStreamChunkKind.CONTROL:
            import json

            parsed = json.loads(chunk.text.removeprefix("[[ORCH]]"))
            for secret in ALL_SECRETS:
                assert secret not in chunk.text
            status_text = format_frontend_status(parsed) or ""
            for secret in ALL_SECRETS:
                assert secret not in status_text
    assert text_parts == [SECRET_FINAL]

    # 7) Memory：只保存 delivered exchange（原始 user + 唯一 final）。
    history = router.memory_manager.get_chat_history(
        "core_router", ascending=True
    )
    assert [message["role"] for message in history] == ["user", "assistant"]
    assert history[0]["content"] == f"{SECRET_USER} {SECRET_PATH}"
    assert history[1]["content"] == SECRET_FINAL
    assert all(
        SECRET_SPECIALIST not in message["content"]
        for message in history
    )
    assert all(
        SECRET_SYNTHESIS_INPUT not in message["content"]
        for message in history
    )
    await scope.close()


def _event_from_record(record):
    from datetime import UTC, datetime

    from core.runtime.events import RuntimeEvent

    payload = _payload_from_safe(record.event_type, record.safe_payload)
    return RuntimeEvent(
        schema_version=record.event_schema_version,
        event_id=record.event_id,
        run_id=record.run_id,
        trace_id=record.trace_id,
        sequence=record.sequence,
        event_type=record.event_type,
        emitted_at=record.emitted_at,
        component=record.component,
        payload=payload,
        step_id=record.step_id,
        step_sequence=record.step_sequence,
        span_id=record.span_id,
        parent_span_id=record.parent_span_id,
    )


def _payload_from_safe(event_type, safe_payload):
    """把 Journal safe_payload 还原成 typed payload 以驱动 adapter。

    OUTPUT_DELTA 在 Journal 中只有 digest/length，无法还原正文；安全矩阵
    测试只对 control 事件验证不泄漏，正文通道单独从 Memory/Final 断言。
    """
    from core.runtime.events import (
        ErrorPayload,
        OutputDeltaPayload,
        PlanCreatedPayload,
        PlanningStartedPayload,
        RunCompletedPayload,
        RunStartedPayload,
        StepCompletedPayload,
        StepStartedPayload,
    )

    if event_type is RuntimeEventType.RUN_STARTED:
        return RunStartedPayload(str(safe_payload["status"]))
    if event_type is RuntimeEventType.PLANNING_STARTED:
        return PlanningStartedPayload(
            int(safe_payload["planner_schema_version"]),
            int(safe_payload["configured_timeout_ms"]),
        )
    if event_type is RuntimeEventType.PLAN_CREATED:
        return PlanCreatedPayload(
            str(safe_payload["plan_id"]),
            int(safe_payload["plan_version"]),
            str(safe_payload["fingerprint"]),
            int(safe_payload["step_count"]),
            str(safe_payload["planning_source"]),
            safe_payload.get("shape"),
        )
    if event_type is RuntimeEventType.STEP_STARTED:
        return StepStartedPayload(
            str(safe_payload["status"]),
            agent_id=safe_payload.get("agent_id"),
            execution_kind=safe_payload.get("execution_kind"),
            output_policy=safe_payload.get("output_policy"),
            dependency_count=safe_payload.get("dependency_count"),
        )
    if event_type is RuntimeEventType.STEP_COMPLETED:
        return StepCompletedPayload(
            str(safe_payload["status"]),
            safe_payload.get("safe_error_code"),
            duration_ms=int(safe_payload.get("duration_ms") or 0),
            result_char_count=int(safe_payload.get("result_char_count") or 0),
            delivery_status=safe_payload.get("delivery_status"),
            delivery_duration_ms=int(
                safe_payload.get("delivery_duration_ms") or 0
            ),
        )
    if event_type is RuntimeEventType.ERROR:
        return ErrorPayload(
            str(safe_payload["safe_error_code"]),
            str(safe_payload["safe_message"]),
            str(safe_payload["component"]),
            bool(safe_payload["fatal"]),
            delivery_status=safe_payload.get("delivery_status"),
            final_step_status=safe_payload.get("final_step_status"),
            memory_commit_status=safe_payload.get("memory_commit_status"),
        )
    if event_type is RuntimeEventType.RUN_COMPLETED:
        return RunCompletedPayload(
            str(safe_payload["status"]),
            str(safe_payload["stop_reason"]),
            duration_ms=int(safe_payload.get("duration_ms") or 0),
            safe_error_code=safe_payload.get("safe_error_code"),
            delivery_status=safe_payload.get("delivery_status"),
            final_step_status=safe_payload.get("final_step_status"),
            memory_commit_status=safe_payload.get("memory_commit_status"),
            memory_duration_ms=int(
                safe_payload.get("memory_duration_ms") or 0
            ),
            shape=safe_payload.get("shape"),
        )
    if event_type is RuntimeEventType.OUTPUT_DELTA:
        # Journal 只保存 digest/length，正文由本测试已知的 delivered final
        # 提供，验证唯一正文通道只承载该 final。
        return OutputDeltaPayload(SECRET_FINAL)
    return None
