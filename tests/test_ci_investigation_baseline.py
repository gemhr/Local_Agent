"""通过原业务入口采集受控 CI 基线；延迟样本只验证等待结构。"""
import asyncio
import json
import threading
import time
from datetime import UTC, datetime

import pytest
from sqlalchemy import event

from core.agent_platform.application import AgentApplicationService
from core.agent_platform.ci_diagnosis import CIInvestigationService, definition
from core.agent_platform.ci_diagnosis_tools import CIDiagnosisTool
from core.auth import ObjectAuthorizationService
from core.chat_service import ChatService
from core.runtime.model_invocation import ModelAdapterResponse
from server import RunExecutionSupervisor
from tests.fixtures.adaptive_execution_server import ObservedInvestigator
from tests.test_ci_adaptive_stop import actions, build


@pytest.mark.asyncio
@pytest.mark.parametrize("delay", [0, 0.03], ids=["unmodified-io", "controlled-wait"])
async def test_ci_investigation_serial_resource_baseline(clean_database, tmp_path, monkeypatch, delay):
    policy = definition().business_options["loop_policy"]
    q = await build(clean_database, tmp_path, loop_limits={"total_tokens": policy["total_tokens"]})
    provider = ObservedInvestigator()  # 不持有 manifest 或预制最终报告。
    supervisor = RunExecutionSupervisor(max_active_runs=1)
    application = AgentApplicationService(ChatService(q.factory._router,
        coordinated_runtime_factory=q.factory), q.factory._agent_registry)
    service = CIInvestigationService(q.incidents, application, q.repo, supervisor,
        ObjectAuthorizationService(clean_database), real_model=True)
    executor = q.factory.services.coordinated_step_executor
    controller = q.factory._router.tool_execution_service.concurrency_controller
    timeline, model_times, reads, db_holds = [], [], [], []
    origin = time.perf_counter()
    clock = lambda: round((time.perf_counter() - origin) * 1000, 3)

    def sample():
        snapshot = executor.snapshot()
        return dict(step_active=snapshot.active_count,
            step_tasks=[t.kind.value for t in snapshot.tasks],
            run_slot_held=supervisor._slots.locked(),
            producer_tasks=len(supervisor._tasks), tool_permits=controller.active_permit_count)

    def checkout(connection, record, proxy):
        record.info["baseline_start"] = clock()

    def checkin(connection, record):
        start = record.info.pop("baseline_start", None)
        if start is not None:
            db_holds.append(dict(start_ms=start, end_ms=clock()))

    original_read = q.incidents.bound_execution

    async def read(context, evidence_id=None):
        row = dict(evidence_id=evidence_id, started_ms=clock(), before=sample())
        reads.append(row)
        if evidence_id and delay:
            await asyncio.sleep(delay)  # 仅受控样本；生产读取函数完全保留。
            row["after_wait"] = sample()
        value = await original_read(context, evidence_id)
        row["finished_ms"] = clock()
        return value

    original_tool = CIDiagnosisTool.invoke_once

    def tool(adapter, invocation, context):
        row = dict(action_id=invocation.invocation_id, tool=invocation.tool_name,
            evidence_id=invocation.arguments.get("evidence_id"), started_ms=clock(),
            thread=threading.get_ident(), resources=sample())
        timeline.append(row)
        try:
            return original_tool(adapter, invocation, context)
        finally:
            row["finished_ms"] = clock()

    def model(messages, **kwargs):
        row = dict(started_ms=clock(), thread=threading.get_ident(), resources=sample())
        model_times.append(row)
        # 同一受控等待样本分别观察同步模型与同步工具等待。
        if delay:
            time.sleep(delay)
            row["after_wait"] = sample()
        result = provider.invoke_request(messages, **kwargs)
        assert isinstance(result, ModelAdapterResponse)
        row["finished_ms"] = clock()
        return result

    original_commit = q.repo.commit_loop_batch
    batches = []

    async def commit(*args, **kwargs):
        result = await original_commit(*args, **kwargs)
        batches.append(dict(iteration=args[2], committed_ms=clock()))
        return result

    monkeypatch.setattr(q.incidents, "bound_execution", read)
    monkeypatch.setattr(CIDiagnosisTool, "invoke_once", tool)
    monkeypatch.setattr(q.model, "invoke_request", model)
    monkeypatch.setattr(q.repo, "commit_loop_batch", commit)
    event.listen(clean_database.engine.sync_engine, "checkout", checkout)
    event.listen(clean_database.engine.sync_engine, "checkin", checkin)
    try:
        links = await service.start(q.principal, q.incident.incident_id, "baseline-" + tmp_path.name)
        # start 创建并授权本次真实调查；build 的预备调查没有执行。
        q.row = await q.incidents.investigation(q.principal, q.incident.incident_id, links["investigation_id"])
        task = supervisor._tasks[links["run_id"]]
        await asyncio.wait_for(asyncio.shield(task), timeout=180)
        finished = clock()
        result = await service.get(q.principal, q.incident.incident_id, links["investigation_id"])
        image = await q.repo.load(links["run_id"])
        saved_actions = await actions(q)
        # 已封存的 get() 不再 load Runtime，runtime_status 按现合同为 None。
        assert image.root.status == "SUCCEEDED", result
        assert result["investigation_status"] == "COMPLETED", result
        assert result["diagnosis_status"] == "SUPPORTED" and result["safe_error_code"] is None
        assert image.steps[0].status == "SUCCEEDED"
        assert image.loop_frames[0].final_delivery_receipt["status"] == "DELIVERED"
        assert image.root.budget_totals["max_total_tokens"] == policy["total_tokens"] == 32768
        assert image.loop_frames[0].loop_policy["max_batch_concurrency"] == 1
        assert all(t.state == "COMMITTED" for t in image.tool_invocations)
        assert len(image.models) == 4 and len(image.tool_invocations) == 4
        assert all(m.request_assessment["reliable"] and m.request_assessment["admissible"] for m in image.models)
        pair = [a for a in saved_actions if a.iteration_no == 2]
        assert [a.ordinal for a in pair] == [0, 1]
        assert len({a.recipe["arguments"]["evidence_id"] for a in pair}) == 2
        assert all(not a.recipe["concurrent_safe"] and a.recipe["resource_key"] is None for a in pair)
        first, second = timeline[1:3]
        assert first["finished_ms"] <= second["started_ms"]
        assert next(b["committed_ms"] for b in batches if b["iteration"] == 2) >= second["finished_ms"]
        assert all(r["resources"]["step_active"] == 1 and r["resources"]["run_slot_held"] for r in timeline + model_times)
        assert all(r["resources"]["tool_permits"] == 1 for r in timeline)
        if delay:
            assert all(r["after_wait"]["step_active"] == 1 and r["after_wait"]["run_slot_held"] for r in model_times)
            assert all(r["after_wait"]["step_active"] == 1 and r["after_wait"]["run_slot_held"] for r in reads if r["evidence_id"])
        assert not supervisor._slots.locked() and not supervisor._tasks
        measurement = dict(captured_at=datetime.now(UTC).isoformat(), artificial_wait_seconds=delay,
            identities=links, incident_id=q.incident.incident_id, execution_id=q.execution.execution_id,
            result=result, total_ms=finished, tools=timeline, models=model_times, reads=reads,
            batches=batches, db_connection_holds=db_holds,
            budget=image.root.budget_consumed,
            final_receipt=image.loop_frames[0].final_delivery_receipt,
            observations=[dict(iteration=i.iteration_no, state=i.state, cursor=i.observation_cursor) for i in image.loop_iterations],
            final_resources=sample())
        target = tmp_path / "ci_baseline.json"
        target.write_text(json.dumps(measurement, ensure_ascii=False, indent=2), encoding="utf-8")
        print("BASELINE_ARTIFACT=" + str(target))
    finally:
        if supervisor._tasks:
            await asyncio.gather(*supervisor._tasks.values(), return_exceptions=True)
        event.remove(clean_database.engine.sync_engine, "checkout", checkout)
        event.remove(clean_database.engine.sync_engine, "checkin", checkin)
