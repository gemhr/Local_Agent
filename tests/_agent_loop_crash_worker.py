"""公开合成 Provider：精确 durable 边界退出与 SQLite 业务幂等回执。"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.persistence.database import Database, DatabaseConfig
from core.persistence.repositories.execution import DurableExecutionRepository
from core.runtime.agent_loop.contracts import LoopSuspended
from core.runtime.model_invocation import ModelAdapterResponse
from core.runtime.recovery_coordinator import RecoveryCoordinator
from core.runtime.run_control import DurableRunControlService
from core.runtime.tool_adapters import ToolAdapterResponse
from core.runtime.tool_contract import (RetryDisposition, ToolExecutionResult, ToolExecutionStatus,
    ToolSideEffectKind, ToolSideEffectState, build_tool_output, canonical_json_digest, safe_key_digest)
from core.runtime.tool_idempotency import ProviderReconciliationEvidence, ProviderReconciliationResult
from core.runtime.retry import OperationIdempotency
from core.runtime.budget import RunBudget
from tests.test_agent_loop_runtime import ObservationTool, assembly, execute, native


class BusinessReceiptTool(ObservationTool):
    def __init__(self, directory, phase):
        super().__init__("loop_a")
        self.directory, self.phase = directory, phase
        self.spec = replace(self.spec, side_effect_kind=ToolSideEffectKind.EXTERNAL_STATE_MUTATION,
            idempotency=OperationIdempotency.IDEMPOTENT_WITH_KEY, supports_idempotency_replay=True)
        with self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS receipts (operation_key TEXT PRIMARY KEY, invocation_id TEXT NOT NULL, payload TEXT NOT NULL)")

    def connection(self):
        return sqlite3.connect(str(self.directory / "business.sqlite"))

    def invoke_once(self, invocation, context):
        context.raise_if_cancelled()
        context.before_side_effect()
        now = datetime.now(UTC)
        receipt = {"provider_receipt_digest": canonical_json_digest({"operation_key": invocation.idempotency_key, "side_effect_count": 1}),
            "business_operation_key_digest": safe_key_digest(invocation.idempotency_key), "side_effect_count": 1}
        result = ToolExecutionResult(invocation_id=invocation.invocation_id, attempt_id=context.attempt_id,
            tool_name="loop_a", status=ToolExecutionStatus.SUCCEEDED,
            output=build_tool_output("loop_a committed business receipt", "text/plain", 4096),
            safe_summary="synthetic business committed", side_effect_state=ToolSideEffectState.COMMITTED,
            idempotency_replayed=False, retry_disposition=RetryDisposition.UNSAFE, resource_key_digest=None,
            started_at=now, completed_at=now, duration_ms=0, provider_receipt=receipt)
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO receipts VALUES (?,?,?)", (invocation.idempotency_key, invocation.invocation_id, json.dumps(result.to_safe_dict(include_output=True))))
            db.commit()
        with (self.directory / "dispatch.jsonl").open("a", encoding="utf-8") as out:
            out.write(json.dumps({"invocation_id": invocation.invocation_id, "attempt_id": context.attempt_id}) + "\n")
        if self.phase == "dispatch":
            os._exit(72)
        return ToolAdapterResponse("loop_a committed business receipt", "text/plain", "synthetic business committed",
            side_effect_state=ToolSideEffectState.COMMITTED, provider_receipt=receipt)

    def reconcile_durable(self, record):
        assert record.state.value == "UNKNOWN"
        with self.connection() as db:
            row = db.execute("SELECT payload FROM receipts WHERE invocation_id=?", (record.invocation_id,)).fetchone()
        assert row is not None
        (self.directory / "queried-unknown.json").write_text(json.dumps({"invocation_id": record.invocation_id, "state": record.state.value}), encoding="utf-8")
        return ProviderReconciliationEvidence(ProviderReconciliationResult.COMMITTED, json.loads(row[0]))


class BoundaryRepository(DurableExecutionRepository):
    def __init__(self, database, control, phase):
        super().__init__(database, control)
        self.phase = phase

    async def finish_model_attempt(self, *args, **kwargs):
        result = await super().finish_model_attempt(*args, **kwargs)
        if self.phase == "winner" and kwargs["state"].value == "COMPLETED":
            os._exit(76)
        return result

    async def accept_loop_decision(self, *args, **kwargs):
        result = await super().accept_loop_decision(*args, **kwargs)
        if self.phase == "decision" and result.iteration_no == 1:
            os._exit(71)
        return result

    async def commit_loop_observation(self, *args, **kwargs):
        result = await super().commit_loop_observation(*args, **kwargs)
        if self.phase == "observation" and result.iteration_no == 1:
            os._exit(73)
        if self.phase == "no-progress" and result.iteration_no == 2:
            os._exit(77)
        return result

    async def publish_loop_final(self, *args, **kwargs):
        result = await super().publish_loop_final(*args, **kwargs)
        if self.phase == "final":
            os._exit(74)
        return result


class RepeatObservationTool(ObservationTool):
    def invoke_once(self, invocation, context):
        with (self.directory / "read-only-dispatch.jsonl").open("a", encoding="utf-8") as out:
            out.write(json.dumps({"invocation_id": invocation.invocation_id}) + "\n")
        return super().invoke_once(invocation, context)


async def main():
    if sys.argv[1].startswith("context-"):
        from tests.test_context_compaction import context_crash_worker
        await context_crash_worker(*sys.argv[1:])
        return
    from core.runtime.agent_loop.driver import AgentLoopDriver
    original = AgentLoopDriver.execute
    def diagnose(self, *args, **kwargs):
        try:
            return original(self, *args, **kwargs)
        except BaseException:
            import traceback
            traceback.print_exc()
            raise
    AgentLoopDriver.execute = diagnose
    phase, dsn, run_id, output_directory = sys.argv[1:]
    directory = Path(output_directory)
    database = Database(DatabaseConfig(url=dsn, use_null_pool=True))
    control = DurableRunControlService(database, lease_seconds=1)
    repository = BoundaryRepository(database, control, phase)
    repeat_mode = phase in {"no-progress", "no-progress-complete"}
    winner_mode = phase in {"winner", "winner-complete"}
    tool = RepeatObservationTool("loop_a") if repeat_mode else BusinessReceiptTool(directory, phase)
    if repeat_mode:
        tool.directory = directory
    memory_mode = phase in {"memory", "memory-complete"}
    memory = None
    if memory_mode:
        from core.persistence.memory import PostgresMemoryManager, PostgresMemoryManagerBridge
        from core.persistence.sync_bridge import SyncPersistenceBridge
        class MemoryBoundary(PostgresMemoryManager):
            async def append_exchange_atomic(self, *args, **kwargs):
                result = await super().append_exchange_atomic(*args, **kwargs)
                if phase == "memory":
                    os._exit(75)
                return result
        memory = PostgresMemoryManagerBridge(MemoryBoundary(database), SyncPersistenceBridge(asyncio.get_running_loop()))

    def scripted(messages):
        observed = len([m for m in messages if m["role"] == "tool"])
        with (directory / "models.jsonl").open("a", encoding="utf-8") as out:
            out.write(json.dumps({"observations": observed}) + "\n")
        if memory_mode or winner_mode:
            return ModelAdapterResponse("durable memory final", finish_reason="stop", tool_call_count=0)
        if repeat_mode:
            if observed < 2:
                return native("loop_a", f"repeat-{observed}")
            return ModelAdapterResponse("must not consume this decision", finish_reason="stop", tool_call_count=0)
        if observed == 0:
            return native("loop_a", "business-call-a")
        if observed == 1:
            return native("loop_b", "read-call-b")
        return ModelAdapterResponse("durable process final", finish_reason="stop", tool_call_count=0)

    factory, _, model, _ = await assembly(database, directory, [scripted] * 3,
        tools=[tool, ObservationTool("loop_b")], owner="process-worker", repository=repository, control=control, memory_manager=memory)

    async def recovered(rid, lease, image):
        try:
            scope = await factory.create_rehydrated_run_scope(image, lease=lease, run_id=rid, persist=False)
        except BaseException:
            import traceback
            traceback.print_exc()
            raise
        try:
            result = await scope.execute()
            expected_status = "FAILED" if repeat_mode else "SUCCEEDED"
            assert result.status.value == expected_status, (result.error_code, result.safe_message)
            if repeat_mode:
                assert result.stop_reason.value == "REPEATED_ACTION"
        finally:
            await scope.close(abort=True)

    if phase in {"decision", "memory", "winner", "no-progress"}:
        options = {"budget": RunBudget(max_model_calls=1, max_cost_units=1, max_output_tokens=128)} if winner_mode else {}
        await execute(factory, run_id=run_id, persist=memory_mode, **options)
        raise AssertionError("decision crash did not trigger")
    recovery = RecoveryCoordinator(repository, control, recovered, instance_id="process-recovery")
    if phase == "observation":
        # 未知动作不能进入执行扫描；只有原 reconciliation lane 可查询。
        assert run_id not in await repository.stale_run_ids()
        async def reconcile(candidate):
            lease = await control.claim(candidate.run_id, "receipt-query")
            try:
                return await factory.services.durable_tool_invocation.reconcile_durable_record(
                    lease=lease, invocation_id=candidate.invocation_id, provider=tool)
            finally:
                await control.release(lease)
        recovery.configure_reconciliation(reconcile)
        assert await recovery.reconciliation_scan_once() == 1
        await asyncio.gather(*tuple(recovery._reconciliation_tasks))
    assert await recovery.scan_once() == 1
    await asyncio.gather(*tuple(recovery._local_tasks))
    image = await repository.load(run_id)
    if phase in {"complete", "memory-complete", "winner-complete", "no-progress-complete"}:
        assert image.root.status == ("FAILED" if repeat_mode else "SUCCEEDED"), image.root.last_recovery_error
        (directory / "complete.json").write_text(json.dumps({"status": image.root.status, "model_calls": len(image.models),
            "receipt": image.loop_frames[0].final_delivery_receipt}), encoding="utf-8")
    else:
        raise AssertionError("expected crash boundary was not reached")
    await database.dispose()


if __name__ == "__main__":
    asyncio.run(main())
