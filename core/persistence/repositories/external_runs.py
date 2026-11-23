"""外部 Run 的接纳、身份和终态。与 Native 共用 Run 级事务锁。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func

from core.persistence.models import ExternalRunRow, RunControlRow, RuntimeRunExecutionRow
from core.persistence.repositories.runtime import lock_run_scope, workflow_claim_allowed

if TYPE_CHECKING:
    from core.agent_platform.runtime_adapter import ExecutionResult, RuntimeRequest
    from core.persistence.database import Database
    from core.runtime.client_event_feed import PostgresClientEventFeed


class ExternalRunConflict(RuntimeError):
    """Run ID 已被某次执行占用。"""


class ExternalRunRepository:
    def __init__(self, database: Database, feed: PostgresClientEventFeed) -> None:
        self._database = database
        self._feed = feed

    async def get(self, run_id: str) -> ExternalRunRow | None:
        async with self._database.session() as session:
            return await session.get(ExternalRunRow, run_id)

    async def claim(
        self, request: RuntimeRequest, result: ExecutionResult, target_digest: str, *,
        workflow_target_digest: str | None = None,
    ) -> str | None:
        access = request.memory_access_context
        if access is None:
            raise ValueError("External Run requires a trusted owner context")
        async with self._database.transaction() as session:
            await lock_run_scope(session, result.run_id)
            if not await workflow_claim_allowed(
                session, result.run_id, "OPENCODE", request, runtime_target_digest=workflow_target_digest,
            ):
                raise ExternalRunConflict(result.run_id)
            control = await session.get(RunControlRow, result.run_id)
            if (
                (control is not None and (control.fencing_token > 0 or control.state == "CLOSED"))
                or await session.get(RuntimeRunExecutionRow, result.run_id) is not None
                or await session.get(ExternalRunRow, result.run_id) is not None
            ):
                raise ExternalRunConflict(result.run_id)
            session.add(ExternalRunRow(
                run_id=result.run_id, trace_id=result.trace_id,
                instance_id=request.runtime_instance_id, target_digest=target_digest,
                tenant_id=access.tenant_id, user_id=access.user_id, conversation_id=access.conversation_id,
                session_id=access.session_id, agent_id=result.agent_id, agent_version=result.agent_version,
                observation_state="CREATED", cancel_reason=control.cancel_reason if control else None,
            ))
            await self._feed.append_external_in_transaction(session, result.run_id, "run.started", {"status": "running"})
            return control.cancel_reason if control else None

    async def bind_session(self, run_id: str, session_id: str, user_message_id: str) -> None:
        async with self._database.transaction() as session:
            await lock_run_scope(session, run_id)
            row = await session.get(ExternalRunRow, run_id)
            row.external_session_id = session_id
            row.user_message_id = user_message_id
            row.updated_at = func.now()

    async def dispatch(self, run_id: str) -> str | None:
        """在取消写入使用的同一锁下确认是否仍可派发。"""
        async with self._database.transaction() as session:
            await lock_run_scope(session, run_id)
            row = await session.get(ExternalRunRow, run_id)
            control = await session.get(RunControlRow, run_id)
            if row.observation_state != "CREATED":
                raise ExternalRunConflict(run_id)
            row.updated_at = func.now()
            if control is not None and control.cancel_reason is not None:
                row.cancel_reason = control.cancel_reason
                return row.cancel_reason
            row.observation_state = "ACTIVE"
            return None

    async def remember_cancel(self, run_id: str, reason: str) -> None:
        async with self._database.transaction() as session:
            await lock_run_scope(session, run_id)
            row = await session.get(ExternalRunRow, run_id)
            if row.terminal_status is None and row.cancel_reason is None:
                row.cancel_reason = reason
                row.updated_at = func.now()

    async def append_update(self, run_id: str, event_type: str, payload: dict) -> None:
        async with self._database.transaction() as session:
            await lock_run_scope(session, run_id)
            row = await session.get(ExternalRunRow, run_id)
            if row.observation_state in {"CREATED", "ACTIVE"}:
                await self._feed.append_external_in_transaction(session, run_id, event_type, payload)

    async def observe_unknown(self, run_id: str) -> None:
        async with self._database.transaction() as session:
            await lock_run_scope(session, run_id)
            row = await session.get(ExternalRunRow, run_id)
            if row.observation_state in {"CREATED", "ACTIVE"}:
                row.observation_state = "UNKNOWN"
                row.error_code = "OPENCODE_RESULT_UNKNOWN"
                row.updated_at = func.now()
                await self._feed.append_external_in_transaction(
                    session, run_id, "run.observation_lost", {"error_code": row.error_code},
                )

    async def finish(self, result: ExecutionResult) -> ExternalRunRow:
        """已确认状态与客户端终态使用同一事务。迟到结果不能改写终态。"""
        async with self._database.transaction() as session:
            await lock_run_scope(session, result.run_id)
            row = await session.get(ExternalRunRow, result.run_id)
            if row.terminal_status is not None:
                return row
            row.observation_state = "TERMINAL"
            row.terminal_status = result.status.value
            row.stop_reason = result.stop_reason.value
            row.error_code = result.error_code
            row.assistant_message_id = result.external_assistant_id
            row.updated_at = func.now()
            event_type = {"SUCCEEDED": "run.completed", "FAILED": "run.failed", "CANCELLED": "run.cancelled"}[
                result.status.value
            ]
            payload = {"status": result.status.value.lower(), "stop_reason": row.stop_reason}
            if row.error_code is not None:
                payload["error_code"] = row.error_code
            await self._feed.append_external_in_transaction(session, result.run_id, event_type, payload)
            return row
