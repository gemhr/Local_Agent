"""原 PG Store 的 scoped facade；复用候选 parser、校验和模型 Owner。"""

from dataclasses import replace
from datetime import UTC, datetime
import re

from core.runtime.memory_scope import MemoryContextError, bounded_dependencies


class MemoryService:
    def __init__(self, repository):
        self.repository = repository

    async def load_context(self, access, query):
        from core.runtime.memory_retrieval import MemoryContextBundle
        from core.runtime.model_context import MemoryContextRecord, MemoryProvenance, ContextSourceType
        scopes = await self.repository.private_scopes(access) if access.principal_kind == "HUMAN" else []
        scopes += await self.repository.project_scopes(access)
        rows = await self.repository.retrieve(access, [s["scope_id"] for s in scopes], query, limit=32)
        history = await self.repository.history(access, access.scope_id("SESSION"), limit=10) if access.principal_kind == "HUMAN" else []
        # supplied 集合全部绑定。超出依赖上限拒绝，不截断权限来源。
        manifest = bounded_dependencies([d for item in [*rows, *history] for d in item["dependencies"]])
        selected, size = [], 0
        for row in rows:
            text = "[" + row["scope_kind"] + "; " + row["epistemic_kind"] + "; UNTRUSTED_MEMORY_DATA] " + row["text"]
            if len(text) + size > 8000:
                continue
            size += len(text)
            selected.append(MemoryContextRecord(MemoryProvenance(row["memory_id"], "SCOPED", row["memory_id"]),
                ContextSourceType.MEMORY_RETRIEVAL, text, datetime.now(UTC)))
        bundle = replace(MemoryContextBundle.empty(access.agent_id, access.scope_id("USER")), records=tuple(selected),
            selected_count=len(selected), candidate_count=len(rows), eligible_count=len(rows), budget_used_chars=size,
            scoped_history=tuple(m for h in history for m in h["messages"]))
        return bundle, scopes, manifest

    async def form(self, *, access, receipt, user_request, final_answer, extraction_model):
        from core.runtime.semantic_memory_formation import StrictFormationProposalParser, validate_candidate
        from core.runtime.memory_lifecycle import has_explicit_forget_cue
        if access is None:
            raise MemoryContextError("MEMORY_PRINCIPAL_REQUIRED")
        if has_explicit_forget_cue(user_request):
            return {"outcome":"EXPLICIT_MUTATION_REQUIRED"}
        await self.repository.private_scopes(access)
        operation, created = await self.repository.prepare_formation(access, access.scope_id("USER"), receipt.exchange_id,
            run_id=receipt.run_id,direct_user_only=True)
        from core.runtime.execution_aggregate import canonical_payload_digest
        if operation.state not in {"COMMITTED","SKIPPED_REVOKED","SUPPRESSED"} and (not operation.receipt.get("direct_user_only")
                or operation.receipt.get("user_message_digest")!=canonical_payload_digest(user_request)):
            raise MemoryContextError("MEMORY_SOURCE_INVALID")
        raw = None
        if not created:
            if operation.state == "COMMITTED":
                return operation.receipt
            if operation.state == "CANDIDATE":
                return await self.repository.commit_candidates(access, operation.operation_id)
            raw = await self.repository.completed_formation_response(access, operation.operation_id)
            if raw is None:
                return {"outcome": "UNKNOWN" if operation.state == "UNKNOWN" else "SKIPPED_REVOKED"}
        if raw is None:
            import asyncio
            context = getattr(extraction_model, "_run_context", None)
            if context is not None:
                context.memory_formation_operation_id = operation.operation_id
                previous_purpose = getattr(context, "loop_model_purpose", "DECISION")
                previous_assessments = getattr(context, "loop_request_assessments", None)
                context.loop_model_purpose, context.loop_request_assessments = "FORMATION", None
                emitter = getattr(extraction_model, "_event_emitter", None)
                if emitter is not None and not hasattr(emitter, "step_id"):
                    image = await context.execution_repository.load(context.run_id)
                    extraction_model._event_emitter = emitter.for_step(next(step.step_id for step in image.steps if step.status == "RUNNING"))
            try:
                from core.runtime.semantic_memory_formation import SemanticMemoryFormation
                raw, interrupted = await SemanticMemoryFormation._await_blocking_safe_boundary(
                    extraction_model.extract, user_request, "")
                if isinstance(raw, BaseException):
                    raise raw
                if interrupted:
                    raise asyncio.CancelledError()
            finally:
                if context is not None:
                    context.memory_formation_operation_id = None
                    context.loop_model_purpose, context.loop_request_assessments = previous_purpose, previous_assessments
        candidates = []
        for proposal in StrictFormationProposalParser.parse(raw):
            validated = validate_candidate(proposal, user_query=user_request)
            if validated is None or not validated.accepted:
                continue
            if re.search(r"(?i)(sk-[\w-]{16,}|password\s*[:=]|api[_ -]?key\s*[:=]|身份证|银行卡|私钥)", "\n".join((validated.canonical_text,str(validated.value),str(proposal.source_excerpt)))):
                continue
            expected=operation.receipt.get("expected_versions",{}).get(validated.logical_key) if operation.receipt.get("explicit_correction") else None
            candidates.append({"text": validated.canonical_text, "logical_key": validated.logical_key,"expected_version":expected,
                "payload": {"value": validated.value}, "epistemic_kind": "USER_ASSERTION",
                "run_id": receipt.run_id, "exchange_id": receipt.exchange_id})
        await self.repository.save_candidates(access, operation.operation_id, candidates)
        return await self.repository.commit_candidates(access, operation.operation_id)
