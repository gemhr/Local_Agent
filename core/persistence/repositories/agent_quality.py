"""唯一 INSERT 授予一次评测准入；重复请求永不触发执行。"""
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert

from core.persistence.models import RuntimeAgentEvaluationReceiptRow as Receipt
from core.runtime.agent_loop.quality_contract import catalog_digest, canonical_bytes, HTTP_LIMIT


class AgentQualityReceiptRepository:
    def __init__(self, database):
        self.database = database

    async def admit(self, *, correlation, principal, manifest, profile):
        identity = {"principal_id": str(principal.user_id), "tenant_id": principal.tenant_id,
                    "correlation": correlation, "execution_manifest": manifest, "capture_profile": profile}
        fingerprint = catalog_digest(identity)
        values = dict(remote_run_id=correlation["run_id"], attempt_id=correlation["attempt_id"],
            request_id=correlation["request_id"], principal_id=identity["principal_id"], tenant_id=principal.tenant_id,
            evaluation_project_id=correlation["evaluation_project_id"], evaluation_run_id=correlation["evaluation_run_id"],
            request_fingerprint=fingerprint, correlation=correlation, execution_manifest=manifest,
            capture_profile=profile, evaluation_capture={})
        async with self.database.transaction() as session:
            winner = (await session.execute(insert(Receipt).values(**values).on_conflict_do_nothing()
                .returning(Receipt.remote_run_id))).scalar_one_or_none()
            if winner is not None:
                from core.persistence.models import RunControlRow
                if await session.get(RunControlRow, correlation["run_id"]) is not None:
                    raise ValueError("QUALITY_REQUEST_CONFLICT")
            rows = tuple((await session.execute(select(Receipt).where(or_(
                Receipt.remote_run_id == values["remote_run_id"], Receipt.attempt_id == values["attempt_id"],
                Receipt.request_id == values["request_id"])))).scalars())
            if len(rows) != 1 or rows[0].request_fingerprint != fingerprint:
                raise ValueError("QUALITY_REQUEST_CONFLICT")
            return winner is not None, rows[0]

    async def read(self, run_id):
        async with self.database.session() as session:
            return await session.get(Receipt, run_id)

    async def seal_in_transaction(self, session, run_id, response):
        """调用方先锁定 control/root/frame/source，再在最后 CAS Receipt。"""
        encoded = canonical_bytes(response)
        if len(encoded) > HTTP_LIMIT:
            raise ValueError("QUALITY_RESPONSE_LIMIT")
        row = await session.get(Receipt, run_id, with_for_update=True)
        if row is None:
            raise ValueError("QUALITY_RECEIPT_MISSING")
        if row.sealed_response is None:
            row.sealed_response = encoded.decode("utf-8")
            row.sealed_digest = catalog_digest(response)
            await session.flush()
        return row.sealed_response

    async def capture(self, execution_repository, run_id, *, final_answer=None):
        """只查询和封存，绝不启动或恢复 Run。终态锁后才锁 Receipt。"""
        from core.persistence.models import (RunControlRow, RuntimeRunExecutionRow, RuntimeLoopFrameRow,
            RuntimeLoopIterationRow, RuntimeLoopActionRow, RuntimeAgentDelegationRow, RuntimeMemoryBindingRow)
        from core.runtime.agent_loop.quality_evidence import observations, component, bundle
        from core.runtime.agent_loop.quality_contract import PROTOCOL
        from core.runtime.agent_loop.evidence import build_loop_evidence
        from core.runtime.agent_loop.execution_evidence import build_execution_evidence
        from core.runtime.agent_loop.context_evidence import build_context_evidence
        # 旧组件由原 Producer 封存，随后主事务重新验证当前 Memory 资格。
        # 此处尚未持有 Runtime 锁，不能在 root/Receipt 锁内开启嵌套事务。
        from core.runtime.memory_scope import MemoryAccessContext, MemoryContextError
        from .memory_lifecycle import MemoryLifecycleRepository
        memory_limit_reached = False
        preliminary = await execution_repository.load(run_id)
        if preliminary and preliminary.root.status in {"SUCCEEDED", "FAILED", "CANCELLED"} and preliminary.root.execution_policy == "durable-agent-loop.v1":
            memory = MemoryLifecycleRepository(self.database)
            original_binding = await memory.binding(run_id)
            if original_binding is not None and original_binding.mode == "SCOPED" and original_binding.evidence is None:
                try:
                    await memory.evidence(MemoryAccessContext.from_dict(original_binding.access), run_id, operation_limit=256)
                except MemoryContextError:
                    pass  # 后续资格检查及 unavailable 组件会显式保留缺证。
                except ValueError as exc:
                    if str(exc) != "MEMORY_EVIDENCE_SOURCE_LIMIT":
                        raise
                    memory_limit_reached = True
        async with self.database.transaction() as session:
            await session.get(RunControlRow, run_id, with_for_update=True)
            root = await session.get(RuntimeRunExecutionRow, run_id, with_for_update=True)
            if root is None or root.status not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                return None
            frames = tuple((await session.execute(select(RuntimeLoopFrameRow).where(RuntimeLoopFrameRow.run_id == run_id)
                .order_by(RuntimeLoopFrameRow.frame_id).with_for_update())).scalars())
            await session.execute(select(RuntimeLoopIterationRow).where(RuntimeLoopIterationRow.run_id == run_id)
                .order_by(RuntimeLoopIterationRow.frame_id, RuntimeLoopIterationRow.iteration_no).with_for_update())
            image = await execution_repository.load(run_id, session=session)
            actions = tuple((await session.execute(select(RuntimeLoopActionRow).where(RuntimeLoopActionRow.run_id == run_id)
                .order_by(RuntimeLoopActionRow.frame_id, RuntimeLoopActionRow.iteration_no, RuntimeLoopActionRow.ordinal))).scalars())
            children = tuple((await session.execute(select(RuntimeAgentDelegationRow).where(RuntimeAgentDelegationRow.run_id == run_id)
                .order_by(RuntimeAgentDelegationRow.delegation_id))).scalars())
            binding = await session.get(RuntimeMemoryBindingRow, run_id)
            current_eligibility = "UNAVAILABLE"
            if binding is not None:
                from .memory_lifecycle import MemoryLifecycleRepository
                from core.runtime.memory_scope import MemoryAccessContext, MemoryContextError
                memory = MemoryLifecycleRepository(self.database)
                await memory._locks(session, [s["scope_id"] for s in binding.scopes] + [d["scope_id"] for d in binding.manifest])
                current_eligibility = "VALID" if binding.mode == "STATELESS" else "INVALID"
                if binding.mode == "SCOPED":
                    try:
                        access = MemoryAccessContext.from_dict(binding.access)
                        await memory._actor(session, access)
                        for item in binding.scopes:
                            scope = await memory._scope(session, access, item["scope_id"])
                            if scope.authz_epoch != item["epoch"]:
                                raise MemoryContextError("MEMORY_CONTEXT_REVOKED")
                        await memory._dependencies(session, binding.manifest)
                        current_eligibility = "VALID" if binding.status == "ACTIVE" else "INVALID"
                    except MemoryContextError:
                        pass
            # 当前资格已在 Scope 锁下确认；Receipt 是最后一把锁。
            receipt = await session.get(Receipt, run_id, with_for_update=True)
            if receipt is None:
                raise ValueError("QUALITY_RECEIPT_MISSING")
            if receipt.sealed_response is not None:
                return receipt.sealed_response
            if receipt.execution_manifest.get("memory_effect_policy") == "SERVICE_PROJECT_READ_ONLY.v1":
                from core.persistence.models import MessageExchangeRow, MemoryOperationRow
                from sqlalchemy import func
                exchanges = int(await session.scalar(select(func.count()).select_from(MessageExchangeRow).where(MessageExchangeRow.run_id == run_id)))
                formation_models = [m for m in image.models if m.purpose == "FORMATION"]
                operations = int(await session.scalar(select(func.count()).select_from(MemoryOperationRow).where(
                    MemoryOperationRow.operation_id.in_([m.memory_operation_id for m in formation_models if m.memory_operation_id]))))
                root_frames = [f for f in frames if f.frame_role == "ROOT"]
                final = root_frames[0].final_delivery_receipt if len(root_frames) == 1 else None
                from core.runtime.execution_aggregate import canonical_payload_digest
                pinned = (root.resume_input.get("agent_loop") or {}).get("memory_effect_policy") == "SERVICE_PROJECT_READ_ONLY.v1"
                status = "NOT_ATTEMPTED" if final else "NOT_REACHED"
                if not pinned or exchanges or operations or formation_models:
                    status = "UNPROVEN" if not pinned else "UNEXPECTED_ACTIVITY"
                receipt.evaluation_capture = {**receipt.evaluation_capture, "memory_effects": {
                    "policy": receipt.execution_manifest["memory_effect_policy"], "read_mode": "PROJECT_SCOPED",
                    "conversation": {"status": status, "reason": "DISABLED_BY_POLICY" if final else "FINAL_NOT_DELIVERED"},
                    "formation": {"status": status, "reason": "DISABLED_BY_POLICY" if final else "FINAL_NOT_DELIVERED"},
                    "final_receipt_digest": canonical_payload_digest(final) if final else None,
                    "exchange_count": exchanges, "formation_operation_count": operations,
                    "formation_model_attempt_count": len(formation_models)}}
            parts = []
            if root.execution_policy == "durable-agent-loop.v1":
                saved = frames[0].sealed_evidence if len(frames) == 1 else None
                parts.append(component("loop", "loop-evidence.v1", saved["body"] if saved else build_loop_evidence(image)))
                context = frames[0].sealed_context_evidence if len(frames) == 1 else None
                parts.append(component("context", "context-evidence.v1", (context["body"] if context else build_context_evidence(image))
                    if frames and frames[0].context_policy else None, "NOT_APPLICABLE" if not frames or not frames[0].context_policy else None))
                parts.append(component("memory", "memory-lifecycle-evidence.v1", binding.evidence if binding else None,
                    "NOT_APPLICABLE" if binding and binding.mode == "STATELESS" else "EVIDENCE_LIMIT" if memory_limit_reached else "MEMORY_EVIDENCE_UNAVAILABLE"))
            elif root.execution_policy == "durable-agent-loop.v2":
                execution = build_execution_evidence(image, actions, children, binding)
                parts.append(component("execution", "agent-execution-evidence.v1", execution))
                for kind, schema in (("loop", "loop-evidence.v1"), ("context", "context-evidence.v1"), ("memory", "memory-lifecycle-evidence.v1")):
                    parts.append(component(kind, schema, reason="UNSUPPORTED_EXECUTION_POLICY"))
            else:
                raise ValueError("QUALITY_EXECUTION_POLICY_UNSUPPORTED")
            observed = observations(image, actions, children, binding, receipt)
            recorded = (root.resume_input.get("agent_loop") or {}).get("correlation", {})
            if any(recorded.get(key) != receipt.correlation[key] for key in ("evaluation_run_id", "attempt_id", "request_id")):
                observed["coverage"] = "PARTIAL"
                observed["gaps"] = sorted(set(observed["gaps"]) | {"ROOT_REQUEST_CORRELATION_MISSING"})
            observed["memory"]["current_eligibility"] = current_eligibility
            if current_eligibility != "VALID":
                observed["coverage"] = "PARTIAL"
                observed["gaps"] = sorted(set(observed["gaps"]) | {"MEMORY_ELIGIBILITY_UNPROVEN"})
            body = bundle(image, receipt, parts, observed)
            if final_answer is not None:
                import hashlib
                content = final_answer.get("content")
                root_final = body["root_final"]
                if (not isinstance(content, str) or len(content.encode("utf-8")) > 65536 or root_final is None
                        or hashlib.sha256(content.encode("utf-8")).hexdigest() != root_final["content_digest"]):
                    final_answer = None
            response = {"protocol_version": PROTOCOL, "run_id": run_id, "status": root.status,
                "stop_reason": root.stop_reason, "capture_status": body["coverage"],
                "bundle": body, "bundle_digest": catalog_digest(body), "final_answer_evidence": final_answer}
            return await self.seal_in_transaction(session, run_id, response)
