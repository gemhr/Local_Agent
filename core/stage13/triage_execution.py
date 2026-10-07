"""业务 Job lease 与 Runtime lease 分离；相同 Run 永不重新 dispatch 模型。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from uuid import UUID, uuid4, uuid5

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert

from core.agent_platform.application import ExecutionRequest
from core.stage13.contracts import canonical_bytes, sha256
from core.stage13.incident_models import AnalysisJobRow, BudgetRow
from core.stage13.triage_models import EvidenceReadRow, JobExecutionRow, TriageRunRow
from core.stage13.triage_subject import (
    INITIAL_TEMPLATE,
    REPAIR_TEMPLATE,
    PROTOCOL,
    check_manifest,
    content_bytes,
    digest,
    strict_json,
    validate_output,
)


@dataclass(frozen=True)
class JobClaim:
    job_id: str
    owner: str
    token: str
    epoch: int


class TriageExecutionService:
    def __init__(self, database, scope):
        self.database, self.scope = database, scope
        self.manifests = {}

    async def claim(self, owner, job_id=None):
        async with self.database.transaction() as session:
            query = (
                select(AnalysisJobRow, JobExecutionRow)
                .join(JobExecutionRow)
                .join(BudgetRow)
                .where(
                    BudgetRow.scope == self.scope,
                    AnalysisJobRow.status.in_(("READY", "RUNNING")),
                    AnalysisJobRow.subject_manifest["execution_enabled"]
                    .as_boolean()
                    .is_(True),
                    or_(
                        JobExecutionRow.lease_until.is_(None),
                        JobExecutionRow.lease_until <= func.clock_timestamp(),
                    ),
                )
                .order_by(AnalysisJobRow.admitted_at, AnalysisJobRow.job_id)
                .with_for_update(of=(AnalysisJobRow, JobExecutionRow), skip_locked=True)
            )
            if job_id:
                query = query.where(AnalysisJobRow.job_id == job_id)
            pair = (await session.execute(query.limit(1))).first()
            if pair is None:
                return None
            job, execution = pair
            check_manifest(
                job.subject_manifest,
                self.manifests.get(job.subject_manifest.get("agent_id")),
            )
            now = await session.scalar(select(func.clock_timestamp()))
            execution.owner, execution.token = owner, str(uuid4())
            execution.epoch += 1
            execution.lease_until = now + timedelta(seconds=30)
            job.status = "RUNNING"
            return JobClaim(job.job_id, owner, execution.token, execution.epoch)

    async def _fence(self, session, claim):
        execution = await session.get(
            JobExecutionRow, claim.job_id, with_for_update=True
        )
        now = await session.scalar(select(func.clock_timestamp()))
        if (
            execution is None
            or (execution.owner, execution.token, execution.epoch)
            != (claim.owner, claim.token, claim.epoch)
            or execution.lease_until is None
            or execution.lease_until <= now
        ):
            raise ValueError("JOB_OWNERSHIP_LOST")
        return execution, now

    async def renew(self, claim):
        async with self.database.transaction() as session:
            execution, now = await self._fence(session, claim)
            execution.lease_until = now + timedelta(seconds=30)

    async def _heartbeat(self, claim):
        while True:
            await asyncio.sleep(10)
            await self.renew(claim)

    async def reserve_run(
        self,
        run_id,
        query,
        manifest,
        *,
        anchor=None,
        role="INITIAL",
        job_id=None,
        attempt_id=None,
        lane="CONTRACT_TEST",
    ):
        UUID(run_id)
        check_manifest(manifest, self.manifests.get(manifest.get("agent_id")))
        parsed = strict_json(query)
        payload = (
            parsed.get("original_authorized_input", parsed)
            if role == "SCHEMA_REPAIR"
            else parsed
        )
        if not isinstance(payload, dict) or set(payload) - {
            "schema_version",
            "scope",
            "EvidenceRefs",
            "incident_ref",
            "failure_summary",
            "environment_samples",
            "version_samples",
            "visible_change_inventory",
            "visible_evidence",
            "evidence_policy",
        }:
            raise ValueError("UNAUTHORIZED_INPUT_FIELDS")
        if (
            len(query.encode("utf-8")) > 256 * 1024
            or len(canonical_bytes(payload)) > 128 * 1024
            or len(payload["visible_evidence"]) > 32
            or len(payload["environment_samples"]) > 8
        ):
            raise ValueError("INPUT_BUDGET_EXCEEDED")
        deadline = datetime.fromisoformat(
            payload["evidence_policy"]["deadline_at"]
        ).astimezone(timezone.utc)
        values = dict(
            run_id=run_id,
            anchor_run_id=anchor or run_id,
            analysis_job_id=job_id,
            evaluation_attempt_id=attempt_id,
            scope=self.scope,
            lane=lane,
            role=role,
            subject_digest=manifest["subject_manifest_digest"],
            query=query,
            input_digest=sha256(canonical_bytes(payload)),
            deadline_at=deadline,
        )
        async with self.database.transaction() as session:
            existing = await session.get(TriageRunRow, run_id)
            if existing is None:
                now = await session.scalar(select(func.clock_timestamp()))
                if deadline > now + timedelta(seconds=180):
                    raise ValueError("ANALYSIS_DEADLINE_INVALID")
            if payload.get("schema_version") != "stage13.triage-input.v1":
                raise ValueError("INPUT_SCHEMA_MISMATCH")
            for evidence in payload["visible_evidence"]:
                if evidence.get("owner_scope_id") != self.scope:
                    raise ValueError("EVIDENCE_SCOPE_DENIED")
                if evidence.get("availability") == "AVAILABLE":
                    content = evidence["content"]
                    if evidence["digest"] != sha256(
                        content.encode("utf-8")
                        if isinstance(content, str)
                        else canonical_bytes(content)
                    ):
                        raise ValueError("EVIDENCE_DIGEST_MISMATCH")
            await session.execute(
                insert(TriageRunRow).values(**values).on_conflict_do_nothing()
            )
            row = await session.get(TriageRunRow, run_id)
            if row is None or any(getattr(row, k) != v for k, v in values.items()):
                raise ValueError("RUN_BINDING_CONFLICT")
        return run_id

    async def read_evidence(self, run_id, operation_id, arguments):
        async with self.database.transaction() as session:
            run = await session.get(TriageRunRow, run_id)
            if run is None or run.scope != self.scope:
                raise ValueError("EVIDENCE_SCOPE_DENIED")
            # INITIAL 行作为整个分析的只读额度互斥锁，repair 不创建第二份额度。
            await session.get(TriageRunRow, run.anchor_run_id, with_for_update=True)
            existing = await session.get(EvidenceReadRow, operation_id)
            if existing is not None:
                if existing.run_id != run_id or existing.evidence.get(
                    "evidence_id"
                ) != arguments.get("evidence_id"):
                    raise ValueError("EVIDENCE_REPLAY_CONFLICT")
                if not existing.successful:
                    raise ValueError("EVIDENCE_NOT_AVAILABLE")
                return existing.evidence
            reads = (
                (
                    await session.execute(
                        select(EvidenceReadRow)
                        .join(TriageRunRow)
                        .where(TriageRunRow.anchor_run_id == run.anchor_run_id)
                    )
                )
                .scalars()
                .all()
            )
            now = await session.scalar(select(func.clock_timestamp()))
            if now >= run.deadline_at or len(reads) >= 8:
                raise ValueError("EVIDENCE_READ_BUDGET_EXCEEDED")
            parsed = strict_json(run.query)
            payload = parsed.get("original_authorized_input", parsed)
            source = next(
                (
                    e
                    for e in payload["visible_evidence"]
                    if e["evidence_id"] == arguments.get("evidence_id")
                ),
                None,
            )
            available = (
                source is not None
                and source.get("availability", "AVAILABLE") == "AVAILABLE"
                and "content" in source
            )
            evidence = (
                source if available else {"evidence_id": arguments.get("evidence_id")}
            )
            size = len(content_bytes(source.get("content"))) if available else 0
            if available and (
                source.get("owner_scope_id") != self.scope
                or source["digest"]
                != sha256(
                    source["content"].encode("utf-8")
                    if isinstance(source["content"], str)
                    else canonical_bytes(source["content"])
                )
            ):
                raise ValueError("EVIDENCE_DIGEST_MISMATCH")
            if sum(r.byte_count for r in reads) + size > 1024 * 1024 or (
                available
                and source["type"] == "AUTHORIZED_ARTIFACT"
                and sum(r.evidence.get("type") == "AUTHORIZED_ARTIFACT" for r in reads)
                >= 4
            ):
                raise ValueError("ARTIFACT_READ_BUDGET_EXCEEDED")
            session.add(
                EvidenceReadRow(
                    operation_id=operation_id,
                    run_id=run_id,
                    evidence=evidence,
                    byte_count=size,
                    successful=available,
                )
            )
        if not available:
            raise ValueError("EVIDENCE_NOT_AVAILABLE")
        return evidence

    async def execute_run(self, run_id, *, fault=None):
        async with self.database.session() as session:
            run = await session.get(TriageRunRow, run_id)
            if run.receipt is not None:
                return run
        manifest = next(
            m
            for m in self.manifests.values()
            if m["subject_manifest_digest"] == run.subject_digest
        )
        image = await self.repository.load(run_id)
        if image is None:
            remaining = (run.deadline_at - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                raise ValueError("ANALYSIS_DEADLINE_EXCEEDED")
            await self.application.execute(
                ExecutionRequest(
                    agent_id=manifest["agent_id"],
                    input=run.query,
                    run_id=run_id,
                    session_id="stage13-" + run.anchor_run_id,
                    timeout_seconds=min(90, remaining),
                    expected_agent_version=manifest["agent_definition_version"],
                )
            )
        elif image.root.status not in {"SUCCEEDED", "FAILED", "CANCELLED", "BLOCKED"}:
            lease = await self.services.durable_run_control.claim(
                run_id, self.services.run_control_owner_id
            )
            try:
                image = await self.repository.prepare_recovery(lease)
                scope = await self.factory.create_rehydrated_run_scope(
                    image, lease=lease, persist=False
                )
                try:
                    await scope.execute()
                finally:
                    await scope.close()
            finally:
                await self.services.durable_run_control.release(lease)
        image = await self.repository.load(run_id)
        if image is None or image.root.status not in {
            "SUCCEEDED",
            "FAILED",
            "CANCELLED",
            "BLOCKED",
        }:
            raise ValueError("RUNTIME_TERMINAL_UNCONFIRMED")
        if fault:
            await fault("after_runtime_terminal")
        root = image.root
        identity = root.resume_input
        for field, expected in {
            "resolved_agent_id": manifest["agent_id"],
            "agent_version": manifest["agent_definition_version"],
            "toolset_identity": manifest["tool_profile_digest"],
            "resolved_model_profile_id": manifest["model_profile_id"],
        }.items():
            if identity.get(field) != expected:
                raise ValueError("ACTUAL_RUNTIME_IDENTITY_MISMATCH")
        # 当前 Runtime terminal root 未保存 final_result_binding；实际 direct
        # Step 的 durable typed result 是恢复时同样使用的结果 authority。
        raw = (root.final_result_binding or {}).get("content")
        if raw is None:
            succeeded = [s for s in image.steps if s.status == "SUCCEEDED"]
            raw = (
                (succeeded[0].typed_result_payload or {}).get("content", "")
                if len(succeeded) == 1
                else ""
            )
        async with self.database.transaction() as session:
            row = await session.get(TriageRunRow, run_id, with_for_update=True)
            reads = (
                (
                    await session.execute(
                        select(EvidenceReadRow).where(
                            EvidenceReadRow.run_id == run_id,
                            EvidenceReadRow.successful.is_(True),
                        )
                    )
                )
                .scalars()
                .all()
            )
            original = strict_json(row.query)
            payload = original.get("original_authorized_input", original)
            validation = validate_output(raw, payload, [r.evidence for r in reads])
            receipt = {
                "receipt_version": "stage13.actual-subject-receipt.v1",
                "run_id": run_id,
                "anchor_run_id": row.anchor_run_id,
                "analysis_job_id": row.analysis_job_id,
                "evaluation_attempt_id": row.evaluation_attempt_id,
                "role": row.role,
                "actual_subject_manifest": manifest,
                "actual_input_digest": row.input_digest,
                "effective_payload_digest": sha256(row.query.encode("utf-8")),
                "prompt_template_digest": sha256(
                    (
                        INITIAL_TEMPLATE if row.role == "INITIAL" else REPAIR_TEMPLATE
                    ).encode("utf-8")
                ),
                "resolved_toolset_identity": identity["toolset_identity"],
                "final_answer_digest": sha256(raw.encode("utf-8")),
                "model_call_receipts": [row.model_call] if row.model_call else [],
                "model_identity_verification": (
                    row.model_call["verification_status"]
                    if row.model_call
                    else "UNKNOWN"
                ),
            }
            receipt["receipt_digest"] = digest(receipt)
            row.receipt, row.raw_answer, row.validation = receipt, raw, validation
            row.runtime_status, row.stop_reason = root.status, root.stop_reason
        return row

    async def execute_analysis(self, initial_id, manifest, *, fault=None, claim=None):
        initial = await self.execute_run(initial_id, fault=fault)
        if fault:
            await fault("after_initial_terminal")
        final = initial
        if (
            initial.runtime_status == "SUCCEEDED"
            and initial.validation["status"] != "VALID"
        ):
            repair_id = str(uuid5(UUID(initial_id), "SCHEMA_REPAIR"))
            repair_query = content_bytes(
                {
                    "original_authorized_input": strict_json(initial.query),
                    "rejected_output": initial.raw_answer,
                    "validation_errors": initial.validation["errors"],
                }
            ).decode()
            await self.reserve_run(
                repair_id,
                repair_query,
                manifest,
                anchor=initial_id,
                role="SCHEMA_REPAIR",
                job_id=initial.analysis_job_id,
                attempt_id=initial.evaluation_attempt_id,
                lane=initial.lane,
            )
            if initial.analysis_job_id:
                async with self.database.transaction() as session:
                    if claim is None:
                        raise ValueError("JOB_CLAIM_REQUIRED")
                    execution, _ = await self._fence(session, claim)
                    if execution.repair_run_id not in (None, repair_id):
                        raise ValueError("REPAIR_RUN_BINDING_CONFLICT")
                    execution.repair_run_id = repair_id
            if fault:
                await fault("after_repair_reservation")
            final = await self.execute_run(repair_id, fault=fault)
        return initial, final

    async def finish(self, claim, final=None, *, unresolved=None):
        async with self.database.transaction() as session:
            execution, now = await self._fence(session, claim)
            job = await session.get(AnalysisJobRow, claim.job_id, with_for_update=True)
            if final is not None:
                execution.selected_final_run_id = final.run_id
                execution.raw_answer_digest = sha256(final.raw_answer.encode("utf-8"))
                execution.structured_output_digest = (
                    digest(final.validation["output"])
                    if final.validation["output"]
                    else None
                )
                execution.validation = final.validation
                execution.actual_subject_receipt_digest = final.receipt[
                    "receipt_digest"
                ]
                unknown = final.model_call is not None and final.model_call[
                    "state"
                ] in {"STARTED", "UNKNOWN"}
                job.status = (
                    "UNRESOLVED"
                    if unknown
                    else (
                        "COMPLETED"
                        if final.runtime_status == "SUCCEEDED"
                        and final.validation["status"] == "VALID"
                        else "FAILED"
                    )
                )
            else:
                job.status = "UNRESOLVED"
                execution.validation = {"status": "UNRESOLVED", "error": unresolved}
            execution.completed_at, execution.lease_until = now, now

    async def run_job(self, claim, *, fault=None):
        heartbeat = asyncio.create_task(self._heartbeat(claim))
        try:
            async with self.database.session() as session:
                job = await session.get(AnalysisJobRow, claim.job_id)
                execution = await session.get(JobExecutionRow, claim.job_id)
            if fault:
                await fault("after_claim")
            await self.reserve_run(
                execution.initial_run_id,
                canonical_bytes(job.input).decode(),
                job.subject_manifest,
                job_id=job.job_id,
                lane="NIGHTLY",
            )
            if fault:
                await fault("after_initial_reservation")
            try:
                _, final = await self.execute_analysis(
                    execution.initial_run_id,
                    job.subject_manifest,
                    fault=fault,
                    claim=claim,
                )
            except ValueError as exc:
                if str(exc) != "ANALYSIS_DEADLINE_EXCEEDED":
                    raise
                await self.finish(claim, unresolved="ANALYSIS_DEADLINE_EXCEEDED")
                return
            if heartbeat.done():
                heartbeat.result()
            if fault:
                await fault("before_final_binding")
            await self.finish(claim, final)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def evaluation_execute(
        self, *, run_id, agent_id, query, timeout_seconds, expected_subject_manifest
    ):
        if (
            expected_subject_manifest.get("agent_id") != agent_id
            or not 0 < timeout_seconds <= 180
        ):
            raise ValueError("IDENTITY_MISMATCH")
        await self.reserve_run(
            run_id, query, expected_subject_manifest, attempt_id=run_id
        )
        async with asyncio.timeout(timeout_seconds):
            initial, final = await self.execute_analysis(
                run_id, expected_subject_manifest
            )
        async with self.database.session() as session:
            children = (
                (
                    await session.execute(
                        select(TriageRunRow).where(
                            TriageRunRow.anchor_run_id == run_id,
                            TriageRunRow.role == "SCHEMA_REPAIR",
                        )
                    )
                )
                .scalars()
                .all()
            )
        return {
            "protocol_version": PROTOCOL,
            "run_id": run_id,
            "status": initial.runtime_status,
            "anchor_run_id": run_id,
            "stop_reason": initial.stop_reason,
            "actual_subject_receipt": initial.receipt,
            "child_runs": [
                {
                    "run_id": r.run_id,
                    "role": r.role,
                    "status": r.runtime_status,
                    "stop_reason": r.stop_reason,
                    "actual_subject_receipt": r.receipt,
                }
                for r in children
            ],
            "child_subject_receipts": [r.receipt for r in children],
            "selected_final_run_id": final.run_id,
            "business_output_validation": final.validation,
            "triage_capture_status": (
                "COMPLETE" if final.runtime_status == "SUCCEEDED" else "FAILED"
            ),
            "final_answer_evidence": {
                "schema_version": "stage13-final-answer.v1",
                "evidence_id": "final-answer://" + run_id,
                "run_id": run_id,
                "attempt_id": run_id,
                "producer_run_id": final.run_id,
                "content": final.raw_answer,
                "content_sha256": sha256(final.raw_answer.encode("utf-8")),
            },
        }


class TriageWorker:
    """有界后台业务 worker；持久权威仍是 PostgreSQL Job/Runtime 表。"""

    def __init__(self, service, *, enabled=False, concurrency=24, owner=None):
        if not 1 <= concurrency <= 24:
            raise ValueError("TRIAGE_CONCURRENCY_INVALID")
        self.service, self.enabled, self.concurrency = service, enabled, concurrency
        self.owner = owner or "triage-worker-" + uuid4().hex
        self.stopping = False
        self.lock = asyncio.Lock()

    async def tick(self):
        if not self.enabled or self.stopping:
            return 0
        async with self.lock:
            claims = []
            for _ in range(self.concurrency):
                if self.stopping:
                    break
                claim = await self.service.claim(self.owner)
                if claim is None:
                    break
                claims.append(claim)
            batch = asyncio.gather(
                *(self.service.run_job(c) for c in claims), return_exceptions=True
            )
            try:
                results = await asyncio.shield(batch)
            except asyncio.CancelledError:
                await batch
                raise
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            return len(claims)

    async def close(self):
        self.stopping = True
        async with self.lock:
            pass
