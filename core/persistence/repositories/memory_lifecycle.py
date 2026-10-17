"""既有 Memory 表的 scoped 事务、来源抑制和当前使用资格。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, NAMESPACE_URL, uuid5
import re

from sqlalchemy import and_, cast, func, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB, insert

from core.persistence.models import (MemoryScopeRow, MemoryOperationRow, RuntimeMemoryBindingRow,
    LongTermMemoryRow, ProjectSemanticMemoryRow, MessageRow, MessageExchangeRow, UserRow,
    RunControlRow, RuntimeRunExecutionRow)
from core.runtime.execution_aggregate import canonical_payload_digest as digest
from core.runtime.memory_scope import MemoryAccessContext, MemoryContextError, bounded_dependencies


def identity(*values):
    return str(uuid5(NAMESPACE_URL, "memory:" + digest(list(values))))


class MemoryLifecycleRepository:
    def __init__(self, database):
        self.database = database

    async def _actor(self, session, access):
        if not isinstance(access, MemoryAccessContext):
            raise MemoryContextError("MEMORY_PRINCIPAL_REQUIRED")
        actor = await session.get(UserRow, UUID(access.user_id))
        if (actor is None or actor.disabled_at is not None or actor.tenant_id != access.tenant_id
                or actor.principal_kind != access.principal_kind):
            raise MemoryContextError("MEMORY_ACCESS_DENIED")

    async def bind_run(self, lease, access, scopes, manifest):
        manifest = bounded_dependencies(manifest)
        async with self.database.transaction() as session:
            await self._control(session, lease)
            await session.get(RuntimeRunExecutionRow, lease.run_id, with_for_update=True)
            await self._locks(session, [s["scope_id"] for s in scopes] + [d["scope_id"] for d in manifest])
            if access is not None:
                await self._actor(session,access)
                for item in scopes:
                    row = await self._scope(session, access, item["scope_id"])
                    if row.authz_epoch != item["epoch"]:
                        raise MemoryContextError("MEMORY_CONTEXT_REVOKED")
                await self._dependencies(session, manifest)
            elif manifest or scopes:
                raise MemoryContextError("MEMORY_PRINCIPAL_REQUIRED")
            row = await session.get(RuntimeMemoryBindingRow, lease.run_id, with_for_update=True)
            if row:
                if row.manifest_digest != digest(manifest) or row.access != (access.to_dict() if access else None):
                    raise MemoryContextError("MEMORY_BINDING_CONFLICT")
                return row
            expiries=[datetime.fromisoformat(d["expires_at"]) for d in manifest if d.get("expires_at")]
            if access is not None and access.session_expires_at is not None:
                expiries.append(access.session_expires_at)
            row = RuntimeMemoryBindingRow(run_id=lease.run_id, mode="SCOPED" if access else "STATELESS",
                access=access.to_dict() if access else None, scopes=scopes, manifest=manifest, manifest_digest=digest(manifest),
                last_fencing_token=lease.fencing_token, earliest_expiry=min(expiries) if expiries else None)
            session.add(row); await session.flush()
            return row

    async def _control(self, session, lease):
        row = await session.get(RunControlRow, lease.run_id, with_for_update=True)
        if (row is None or row.owner_id != lease.owner_id or row.fencing_token != lease.fencing_token
                or row.state != "ACTIVE" or row.lease_until is None or row.lease_until <= datetime.now(UTC)):
            from core.runtime.run_control import OwnershipLost
            raise OwnershipLost()
        return row

    async def guard_in_transaction(self, session, lease, *, admission=None, seed_digest=None):
        """调用方已经按 control/root/frame 顺序加锁；此处只向后取 Scope 锁。"""
        binding = await session.get(RuntimeMemoryBindingRow, lease.run_id)
        if binding is None:
            raise MemoryContextError("MEMORY_CONTEXT_UNVERIFIABLE")
        await self._locks(session, [s["scope_id"] for s in binding.scopes] + [d["scope_id"] for d in binding.manifest])
        binding = await session.get(RuntimeMemoryBindingRow, lease.run_id, with_for_update=True)
        if binding.mode == "SCOPED":
            access = MemoryAccessContext.from_dict(binding.access)
            try:
                await self._actor(session,access)
                for item in binding.scopes:
                    row = await self._scope(session, access, item["scope_id"])
                    if row.authz_epoch != item["epoch"]:
                        raise MemoryContextError("MEMORY_CONTEXT_REVOKED")
                await self._dependencies(session, binding.manifest)
            except MemoryContextError:
                expired = False
                for model, field in ((LongTermMemoryRow, "memory_id"), (ProjectSemanticMemoryRow, "memory_id"), (MessageExchangeRow, "exchange_id")):
                    ids = [d["id"] for d in binding.manifest if d.get("kind") in {"RECORD", "EXCHANGE"}]
                    expired = expired or bool(await session.scalar(select(getattr(model, field)).where(
                        getattr(model, field).in_(ids), model.expires_at <= func.clock_timestamp()).limit(1)))
                binding.status = "INVALID"
                binding.invalid_code = "MEMORY_CONTEXT_EXPIRED" if expired else "MEMORY_CONTEXT_REVOKED"
        if binding.earliest_expiry is not None and binding.earliest_expiry <= datetime.now(UTC):
            binding.status, binding.invalid_code = "INVALID", "MEMORY_CONTEXT_EXPIRED"
        if binding.manifest_digest != digest(binding.manifest):
            binding.status, binding.invalid_code = "INVALID", "MEMORY_CONTEXT_UNVERIFIABLE"
        if seed_digest:
            if binding.seed_digest is not None and binding.seed_digest != seed_digest:
                raise MemoryContextError("MEMORY_BINDING_CONFLICT")
            binding.seed_digest = seed_digest
        binding.last_fencing_token = lease.fencing_token
        binding.row_version += 1
        if binding.status != "ACTIVE":
            return binding.invalid_code
        if admission:
            if admission.startswith("preplan:") and any(
                    a["id"] == admission or (a["id"].startswith(admission.rsplit(":",1)[0]+":")
                    and a["fence"] != lease.fencing_token) for a in binding.admissions):
                binding.status,binding.invalid_code="INVALID","MEMORY_CONTEXT_UNVERIFIABLE"
                return binding.invalid_code
            if len(binding.admissions) >= 256:
                return "MEMORY_CONTEXT_UNVERIFIABLE"
            binding.admissions = [*binding.admissions, {"id": admission, "fence": lease.fencing_token,
                "manifest_digest": binding.manifest_digest, "outcome": "ALLOW", "state": "STARTED"}]
        return None

    async def guard(self, lease, *, admission=None, seed_digest=None):
        async with self.database.transaction() as session:
            await self._control(session, lease)
            await session.get(RuntimeRunExecutionRow, lease.run_id, with_for_update=True)
            code = await self.guard_in_transaction(session, lease, admission=admission, seed_digest=seed_digest)
        if code:
            raise MemoryContextError(code)

    async def binding(self, run_id):
        async with self.database.session() as session:
            return await session.get(RuntimeMemoryBindingRow, run_id)

    async def _scope(self, session, access, scope_id, action="READ"):
        await self._actor(session, access)
        row = await session.get(MemoryScopeRow, scope_id)
        if row is None or row.tenant_id != access.tenant_id or row.agent_id != access.agent_id or row.status != "ACTIVE":
            raise MemoryContextError("MEMORY_SCOPE_DENIED")
        if row.scope_kind == "PROJECT":
            if action not in row.grants.get(access.user_id, []):
                raise MemoryContextError("MEMORY_PROJECT_GRANT_REQUIRED")
        elif (access.principal_kind != "HUMAN" or str(row.owner_user_id) != access.user_id
                or (row.scope_kind == "SESSION" and row.session_id != access.session_id)):
            raise MemoryContextError("MEMORY_SCOPE_DENIED")
        return row

    async def _locks(self, session, scope_ids):
        ids = sorted(set(scope_ids))
        if ids:
            await session.execute(select(MemoryScopeRow).where(MemoryScopeRow.scope_id.in_(ids)).order_by(MemoryScopeRow.scope_id).with_for_update())

    async def private_scopes(self, access):
        async with self.database.transaction() as session:
            await self._actor(session, access)
            if access.principal_kind != "HUMAN":
                raise MemoryContextError("MEMORY_PRIVATE_SCOPE_DENIED")
            result = []
            for kind in ("SESSION", "USER"):
                sid = access.scope_id(kind)
                await session.execute(insert(MemoryScopeRow).values(scope_id=sid, tenant_id=access.tenant_id,
                    scope_kind=kind, owner_user_id=UUID(access.user_id), agent_id=access.agent_id,
                    session_id=access.session_id if kind == "SESSION" else None).on_conflict_do_nothing())
                row = await self._scope(session, access, sid)
                result.append({"scope_id": sid, "epoch": row.authz_epoch, "kind": kind})
            return result

    async def configure_project(self, *, tenant_id, project_id, agent_id, grants, ttl_seconds):
        """仅由受信 Composition 调用；不存在从 HTTP body 到这里的路由。"""
        for actions in grants.values():
            if not set(actions) <= {"READ", "WRITE", "UPDATE", "REVOKE", "DELETE", "PROMOTE"}:
                raise MemoryContextError("MEMORY_PROJECT_GRANT_INVALID")
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 2592000:
            raise MemoryContextError("MEMORY_TTL_INVALID")
        sid = identity("PROJECT", tenant_id, project_id, agent_id)
        async with self.database.transaction() as session:
            await session.execute(insert(MemoryScopeRow).values(scope_id=sid, tenant_id=tenant_id, project_id=project_id,
                scope_kind="PROJECT", agent_id=agent_id, grants=grants).on_conflict_do_nothing())
            row = await session.get(MemoryScopeRow, sid, with_for_update=True)
            row.grants, row.authz_epoch, row.row_version = grants, row.authz_epoch + 1, row.row_version + 1
            row.policy = {"ttl_seconds": ttl_seconds}
            return sid

    @staticmethod
    def visible(model):
        return and_(model.access_scope_id.is_not(None), model.lifecycle_status == "ACTIVE", model.body_state == "PRESENT",
            or_(model.expires_at.is_(None), model.expires_at > func.clock_timestamp()),
            func.memory_dependencies_valid(model.source_dependencies),
            ~select(MemoryOperationRow.operation_id).where(MemoryOperationRow.scope_id == model.access_scope_id,
                MemoryOperationRow.kind == "SUPPRESS", MemoryOperationRow.source_identity == model.source_identity).exists())

    @staticmethod
    def reference(row, scope, kind="RECORD"):
        return {"kind": kind, "id": row.exchange_id if kind == "EXCHANGE" else str(row.id) if kind == "USER_MESSAGE" else row.memory_id,
            "scope_id": scope.scope_id, "epoch": scope.authz_epoch, "version": row.record_version, "digest": row.body_digest,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None}

    async def retrieve(self, access, scope_ids, query="", *, limit=32):
        if not 1 <= limit <= 64:
            raise MemoryContextError("MEMORY_LIMIT_INVALID")
        async with self.database.transaction() as session:
            scopes = {sid: await self._scope(session, access, sid) for sid in sorted(set(scope_ids))}
            rows = []
            for model in (LongTermMemoryRow, ProjectSemanticMemoryRow):
                statement = select(model).where(model.access_scope_id.in_(scopes), self.visible(model),
                    model.epistemic_kind.in_(["USER_ASSERTION", "OBSERVATION"]))
                # eligibility 在 SQL 排名和 LIMIT 前；无全局 top-k 再过滤。
                statement = statement.order_by(model.canonical_text.ilike("%" + query[:256] + "%").desc(), model.memory_id).limit(limit)
                for row in (await session.execute(statement)).scalars():
                    scope = scopes[row.access_scope_id]
                    rows.append({"memory_id": row.memory_id, "scope_id": scope.scope_id, "text": row.canonical_text,
                        "epistemic_kind": row.epistemic_kind, "scope_kind": scope.scope_kind, "version": row.record_version,
                        "dependencies": bounded_dependencies([*row.source_dependencies, self.reference(row, scope)])})
            return rows[:limit]

    async def history(self, access, scope_id, *, limit=20):
        async with self.database.transaction() as session:
            await self._scope(session, access, scope_id)
            rows = (await session.execute(select(MessageExchangeRow).where(MessageExchangeRow.access_scope_id == scope_id,
                self.visible(MessageExchangeRow), MessageExchangeRow.state == "COMMITTED").order_by(MessageExchangeRow.created_at.desc()).limit(min(limit, 32)))).scalars().all()
            result = []
            scope = await session.get(MemoryScopeRow, scope_id)
            for row in reversed(rows):
                messages = (await session.execute(select(MessageRow).where(MessageRow.exchange_id == row.exchange_id,
                    MessageRow.access_scope_id == scope_id, self.visible(MessageRow)).order_by(MessageRow.sequence))).scalars().all()
                if len(messages) == 2:
                    result.append({"messages": [{"role": m.role, "content": m.content} for m in messages],
                        "dependencies": bounded_dependencies([*row.source_dependencies, self.reference(row, scope, "EXCHANGE")])})
            return result

    async def _dependencies(self, session, dependencies):
        deps = bounded_dependencies(dependencies)
        if not await session.scalar(select(func.memory_dependencies_valid(cast(deps, JSONB)))):
            raise MemoryContextError("MEMORY_CONTEXT_REVOKED")
        return deps

    async def _suppressed(self, session, scope_id, source):
        return bool(await session.scalar(select(MemoryOperationRow.operation_id).where(MemoryOperationRow.scope_id == scope_id,
            MemoryOperationRow.source_identity == source, MemoryOperationRow.kind == "SUPPRESS").limit(1)))

    async def append_exchange(self, access, *, run_id, user_text, final_text):
        scopes = await self.private_scopes(access)
        sid = next(s["scope_id"] for s in scopes if s["kind"] == "SESSION")
        eid, source = identity(sid, run_id, "exchange"), digest([sid, run_id, "exchange"])
        async with self.database.transaction() as session:
            # 有 Run 时先锁 control/root，再进入 scope，不能从 scope 反锁 Run。
            await session.get(RunControlRow, run_id, with_for_update=True)
            await session.get(RuntimeRunExecutionRow, run_id, with_for_update=True)
            binding = await session.get(RuntimeMemoryBindingRow, run_id)
            deps = bounded_dependencies([*binding.manifest, *({"kind":"SCOPE","scope_id":s["scope_id"],"epoch":s["epoch"]}
                for s in binding.scopes)]) if binding and binding.mode == "SCOPED" else []
            await self._locks(session, [sid, *(d["scope_id"] for d in deps), *(s["scope_id"] for s in (binding.scopes if binding else []))])
            await self._scope(session, access, sid, "WRITE")
            if binding is not None:
                try:
                    for item in binding.scopes:
                        current = await self._scope(session, access, item["scope_id"])
                        if current.authz_epoch != item["epoch"]:
                            raise MemoryContextError("MEMORY_CONTEXT_REVOKED")
                except MemoryContextError:
                    binding.status, binding.invalid_code = "INVALID", "MEMORY_CONTEXT_REVOKED"
            existing = await session.get(MessageExchangeRow, eid, with_for_update=True)
            if existing:
                active = await session.scalar(select(MessageExchangeRow.exchange_id).where(MessageExchangeRow.exchange_id == eid, self.visible(MessageExchangeRow)))
                return {"exchange_id": eid, "state": "ACTIVE" if active and binding is not None and binding.status=="ACTIVE" else "SKIPPED_REVOKED", "replayed": True}
            valid = binding is not None and binding.mode == "SCOPED" and binding.access == access.to_dict() and binding.status == "ACTIVE"
            valid = valid and bool(await session.scalar(select(func.memory_dependencies_valid(cast(deps, JSONB))))) and not await self._suppressed(session, sid, source)
            if not valid:
                await self._suppress(session, sid, source, access)
                return {"exchange_id": eid, "state": "SKIPPED_REVOKED", "replayed": True}
            expiry = datetime.now(UTC) + timedelta(hours=24)
            if access.session_expires_at:
                expiry = min(expiry, access.session_expires_at)
            common = dict(access_scope_id=sid, agent_id=access.agent_id, memory_scope="direct", run_id=run_id,
                lifecycle_status="ACTIVE", body_state="PRESENT", record_version=1, source_identity=source,
                source_dependencies=deps, expires_at=expiry, memory_policy_version=1, epistemic_kind="USER_ASSERTION")
            row = MessageExchangeRow(exchange_id=eid, state="COMMITTED", body_digest=digest([user_text, final_text]), **common)
            user = MessageRow(exchange_id=eid, sequence=0, role="user", content=user_text, body_digest=digest(user_text), **{**common,"source_dependencies":[]})
            assistant = MessageRow(exchange_id=eid, sequence=1, role="assistant", content=final_text, body_digest=digest(final_text), **{**common,"epistemic_kind":"INFERENCE"})
            session.add_all([row, user, assistant]); await session.flush()
            row.user_message_id, row.assistant_message_id = user.id, assistant.id
            return {"exchange_id": eid, "state": "ACTIVE", "replayed": False}

    async def prepare_formation(self, access, scope_id, exchange_id, *, run_id, kind="SEMANTIC", direct_user_only=False):
        oid = identity(scope_id, exchange_id, kind, 1)
        async with self.database.transaction() as session:
            await session.get(RunControlRow, run_id, with_for_update=True)
            await session.get(RuntimeRunExecutionRow, run_id, with_for_update=True)
            binding=await session.get(RuntimeMemoryBindingRow, run_id)
            if binding is None or binding.status!="ACTIVE" or binding.access!=access.to_dict():
                raise MemoryContextError("MEMORY_CONTEXT_REVOKED")
            exchange = await session.get(MessageExchangeRow, exchange_id)
            deps = list(exchange.source_dependencies) if exchange else []
            if exchange:
                source_scope = await session.get(MemoryScopeRow, exchange.access_scope_id)
                if source_scope:
                    deps.append(self.reference(exchange, source_scope, "EXCHANGE"))
            await self._locks(session, [scope_id, *(d["scope_id"] for d in deps)])
            scope = await self._scope(session, access, scope_id, "WRITE")
            if scope.scope_kind == "PROJECT":
                raise MemoryContextError("MEMORY_PROJECT_FORMATION_EXPLICIT_ONLY")
            if (exchange is None or exchange.agent_id != access.agent_id or exchange.run_id != run_id
                    or exchange.access_scope_id != access.scope_id("SESSION")):
                raise MemoryContextError("MEMORY_SOURCE_INVALID")
            old = await session.get(MemoryOperationRow, oid, with_for_update=True)
            if old:
                if old.state == "PREPARED":
                    old.state = "UNKNOWN"
                return old, False
            await self._dependencies(session, deps)
            if (await self._suppressed(session, exchange.access_scope_id, exchange.source_identity)
                    or await self._suppressed(session, scope_id, exchange.source_identity)):
                raise MemoryContextError("MEMORY_CONTEXT_REVOKED")
            user_row = (await session.execute(select(MessageRow).where(MessageRow.exchange_id==exchange_id,
                MessageRow.access_scope_id==access.scope_id("SESSION"),MessageRow.role=="user"))).scalar_one()
            user_text=user_row.content
            if digest(user_text)!=user_row.body_digest:
                raise MemoryContextError("MEMORY_SOURCE_INVALID")
            direct_sources=bounded_dependencies([self.reference(user_row,source_scope,"USER_MESSAGE"),
                {"kind":"SCOPE","scope_id":scope_id,"epoch":scope.authz_epoch}]) if direct_user_only else []
            correction=bool(user_text and re.search(r"更正|改为|改用|换成|不再使用|替换为", user_text))
            versions=(await session.execute(select(LongTermMemoryRow.logical_key,LongTermMemoryRow.record_version).where(
                LongTermMemoryRow.access_scope_id==scope_id,self.visible(LongTermMemoryRow),
                LongTermMemoryRow.logical_key.is_not(None)).order_by(LongTermMemoryRow.memory_id).limit(129))).all()
            if len(versions)>128:
                raise MemoryContextError("MEMORY_CAS_UNVERIFIABLE")
            expected_versions=dict(versions)
            row = MemoryOperationRow(operation_id=oid, scope_id=scope_id, kind="FORMATION", actor_digest=digest(access.to_dict()),
                source_identity=exchange.source_identity, request_digest=digest([scope_id, exchange_id, kind, 1, deps, expected_versions, correction, direct_user_only, direct_sources]),
                dependencies=bounded_dependencies(deps), state="PREPARED", receipt={"formation_kind": kind,
                    "expected_versions":expected_versions,"explicit_correction":correction,
                    "direct_user_only":direct_user_only,"direct_source_manifest":direct_sources,"user_message_digest":user_row.body_digest})
            session.add(row); await session.flush()
            return row, True

    async def save_candidates(self, access, operation_id, candidates):
        if not isinstance(candidates, list) or len(candidates) > 4:
            raise MemoryContextError("MEMORY_CANDIDATE_INVALID")
        keys = [c.get("logical_key") for c in candidates if c.get("logical_key")]
        if len(keys) != len(set(keys)) or any(c.get("epistemic_kind") not in {"USER_ASSERTION", "INFERENCE"} for c in candidates):
            raise MemoryContextError("MEMORY_PROVENANCE_UNSUPPORTED")
        async with self.database.transaction() as session:
            old = await session.get(MemoryOperationRow, operation_id)
            if old is None:
                raise MemoryContextError("MEMORY_OPERATION_UNKNOWN")
            await self._locks(session, [old.scope_id, *(d["scope_id"] for d in old.dependencies)])
            await self._scope(session, access, old.scope_id, "WRITE")
            row = await session.get(MemoryOperationRow, operation_id, with_for_update=True)
            await self._dependencies(session, row.dependencies)
            if await self._suppressed(session, row.scope_id, row.source_identity) or row.state in {"SUPPRESSED", "SKIPPED_REVOKED"}:
                raise MemoryContextError("MEMORY_CONTEXT_REVOKED")
            for candidate in candidates:
                expected=candidate.get("expected_version")
                if expected is not None and (not row.receipt.get("explicit_correction") or
                        row.receipt.get("expected_versions",{}).get(candidate.get("logical_key"))!=expected):
                    raise MemoryContextError("MEMORY_CORRECTION_DENIED")
            body = {"candidates": candidates}
            if row.candidate_digest and row.candidate_digest != digest(body):
                raise MemoryContextError("MEMORY_OPERATION_CONFLICT")
            if row.state not in {"PREPARED", "CANDIDATE"}:
                raise MemoryContextError("MEMORY_OPERATION_INCOMPLETE")
            row.candidate_digest, row.candidate_body, row.state = digest(body), body, "CANDIDATE"

    async def commit_candidates(self, access, operation_id):
        async with self.database.transaction() as session:
            initial = await session.get(MemoryOperationRow, operation_id)
            if initial is None:
                raise MemoryContextError("MEMORY_OPERATION_UNKNOWN")
            await self._locks(session, [initial.scope_id, *(d["scope_id"] for d in initial.dependencies)])
            scope = await self._scope(session, access, initial.scope_id, "WRITE")
            row = await session.get(MemoryOperationRow, operation_id, with_for_update=True)
            if row.state == "COMMITTED":
                return row.receipt
            await self._dependencies(session, row.dependencies)
            if await self._suppressed(session, scope.scope_id, row.source_identity) or row.state != "CANDIDATE":
                raise MemoryContextError("MEMORY_CONTEXT_REVOKED")
            if digest(row.candidate_body) != row.candidate_digest:
                raise MemoryContextError("MEMORY_OPERATION_CONFLICT")
            savepoint = await session.begin_nested()
            source_exchange = (await session.execute(select(MessageExchangeRow).where(
                MessageExchangeRow.access_scope_id == access.scope_id("SESSION"),
                MessageExchangeRow.source_identity == row.source_identity))).scalar_one_or_none()
            if source_exchange is None:
                raise MemoryContextError("MEMORY_SOURCE_INVALID")
            records = []
            for slot, candidate in enumerate(row.candidate_body["candidates"]):
                text_value = candidate["text"]
                if not isinstance(text_value, str) or not 1 <= len(text_value) <= 4096 or candidate["epistemic_kind"] not in {"USER_ASSERTION", "OBSERVATION", "INFERENCE"}:
                    raise MemoryContextError("MEMORY_CANDIDATE_INVALID")
                key = candidate.get("logical_key")
                expected = candidate.get("expected_version")
                active = (await session.execute(select(LongTermMemoryRow).where(LongTermMemoryRow.access_scope_id == scope.scope_id,
                    LongTermMemoryRow.logical_key == key, LongTermMemoryRow.lifecycle_status == "ACTIVE").with_for_update())).scalar_one_or_none() if key else None
                if active:
                    eligible = await session.scalar(select(LongTermMemoryRow.memory_id).where(
                        LongTermMemoryRow.memory_id==active.memory_id,self.visible(LongTermMemoryRow)))
                    if not eligible:
                        expired=active.expires_at is not None and active.expires_at<=datetime.now(UTC)
                        active.lifecycle_status,active.record_version=("EXPIRED" if expired else "REVOKED"),active.record_version+1
                        active = None
                if active and (expected is None or expected != active.record_version):
                    row.state, row.receipt = "CONFLICT", {**row.receipt, "outcome": "VERSION_CONFLICT", "expected_version": expected, "result_version": active.record_version}
                    conflict = dict(row.receipt)
                    await savepoint.rollback()
                    row.state, row.receipt = "CONFLICT", conflict
                    return conflict
                if not active and expected is not None:
                    row.state, row.receipt = "CONFLICT", {**row.receipt, "outcome": "VERSION_CONFLICT"}
                    conflict = dict(row.receipt)
                    await savepoint.rollback()
                    row.state, row.receipt = "CONFLICT", conflict
                    return conflict
                mid = identity(row.operation_id, slot)
                now = datetime.now(UTC)
                ephemeral = candidate["epistemic_kind"] == "INFERENCE"
                record = LongTermMemoryRow(memory_id=mid, memory_type="SEMANTIC", status="CANDIDATE" if ephemeral else "ACTIVE",
                    agent_id=access.agent_id, memory_scope="direct", access_scope_id=scope.scope_id,
                    canonical_text=text_value, payload=candidate.get("payload", {}), logical_key=key,
                    origin_type=candidate["epistemic_kind"], origin_run_id=source_exchange.run_id, origin_exchange_id=source_exchange.exchange_id,
                    origin_agent_id=access.agent_id, origin_memory_scope="direct", formation_method="SCOPED_FORMATION",
                    created_at=now.isoformat(), updated_at=now.isoformat(), record_version=1,
                    expires_at=now + timedelta(minutes=30) if ephemeral else now + timedelta(days=30),
                    lifecycle_status="CANDIDATE" if ephemeral else "ACTIVE", body_state="PRESENT", body_digest=digest([text_value, candidate.get("payload", {})]),
                    source_identity=row.source_identity, source_dependencies=(row.receipt["direct_source_manifest"]
                        if row.receipt.get("direct_user_only") and candidate["epistemic_kind"]=="USER_ASSERTION" else row.dependencies), epistemic_kind=candidate["epistemic_kind"], memory_policy_version=1)
                if active:
                    await self._scope(session, access, scope.scope_id, "UPDATE")
                    if active.epistemic_kind != "USER_ASSERTION" or record.epistemic_kind != "USER_ASSERTION":
                        raise MemoryContextError("MEMORY_CORRECTION_DENIED")
                    active.lifecycle_status, active.status, active.record_version = "SUPERSEDED", "SUPERSEDED", active.record_version + 1
                    active.superseded_by_memory_id = mid
                    record.record_version = active.record_version
                    await session.flush()
                session.add(record)
                records.append({"record_id": mid, "expected_version": expected, "result_version": record.record_version})
            await savepoint.commit()
            row.state, row.receipt = "COMMITTED", {**row.receipt, "outcome": "COMMITTED", "records": records}
            row.record_id = records[0]["record_id"] if len(records) == 1 else None
            row.expected_version = row.candidate_body["candidates"][0].get("expected_version") if len(records) == 1 else None
            row.result_version = records[0]["result_version"] if len(records) == 1 else None
            await session.flush()
            return row.receipt

    async def _suppress(self, session, scope_id, source, access):
        oid = identity(scope_id, source, "SUPPRESS")
        await session.execute(insert(MemoryOperationRow).values(operation_id=oid, scope_id=scope_id, kind="SUPPRESS",
            actor_digest=digest(access.to_dict()), source_identity=source, request_digest=digest([scope_id, source, "SUPPRESS"]),
            state="SUPPRESSED", dependencies=[], receipt={"outcome": "SUPPRESSED"}).on_conflict_do_nothing())

    async def mutate(self, access, scope_id, memory_id, *, expected_version, command_id, action, deletion_scope="MEMORY_STORE_AND_FUTURE_USE"):
        if deletion_scope != "MEMORY_STORE_AND_FUTURE_USE":
            raise MemoryContextError("MEMORY_ERASURE_SCOPE_UNSUPPORTED")
        if action not in {"DELETE", "REVOKE"}:
            raise MemoryContextError("MEMORY_ACTION_UNSUPPORTED")
        oid = identity(scope_id, command_id, action)
        async with self.database.transaction() as session:
            # 独立 Memory mutation 不锁 Run；按 scope_id 排序，传播不反向锁 control。
            all_scopes = (await session.execute(select(MemoryScopeRow.scope_id).where(MemoryScopeRow.tenant_id == access.tenant_id))).scalars().all()
            await self._locks(session, all_scopes)
            scope = await self._scope(session, access, scope_id, action)
            old = await session.get(MemoryOperationRow, oid, with_for_update=True)
            request = digest([scope_id, memory_id, expected_version, command_id, action, deletion_scope])
            if old:
                if old.request_digest != request:
                    raise MemoryContextError("MEMORY_OPERATION_CONFLICT")
                return old.receipt
            model = ProjectSemanticMemoryRow if scope.scope_kind == "PROJECT" else LongTermMemoryRow
            target = await session.get(model, memory_id, with_for_update=True)
            if target is None or target.access_scope_id != scope_id:
                raise MemoryContextError("MEMORY_RECORD_DENIED")
            if target.record_version != expected_version:
                raise MemoryContextError("MEMORY_VERSION_CONFLICT")
            rows = (await session.execute(select(model).where(or_(model.memory_id == memory_id,
                and_(model.access_scope_id == scope_id, model.logical_key == target.logical_key)) if target.logical_key else model.memory_id == memory_id).with_for_update())).scalars().all()
            ids = [r.memory_id for r in rows]
            for model in (LongTermMemoryRow, ProjectSemanticMemoryRow):
                derived = (await session.execute(select(model).where(or_(*(model.source_dependencies.contains([{"kind": "RECORD", "id": mid}]) for mid in ids))).with_for_update())).scalars().all()
                for r in derived:
                    if r not in rows:
                        rows.append(r)
            for row in rows:
                row.lifecycle_status, row.record_version = ("DELETED" if action == "DELETE" else "REVOKED"), row.record_version + 1
                if action == "DELETE":
                    row.canonical_text, row.payload, row.body_state = "", {}, "REDACTED"
                await self._suppress(session, row.access_scope_id, row.source_identity, access)
            # 私人 Memory 的原会话也不得成为删除/撤销后的 history fallback。
            # PROJECT 删除只影响该共享来源及其后代，不反向撤销私人上游。
            if scope.scope_kind in {"USER","SESSION"}:
                source_scopes=(await session.execute(select(MemoryScopeRow).where(MemoryScopeRow.tenant_id==access.tenant_id,
                    MemoryScopeRow.agent_id==access.agent_id,MemoryScopeRow.owner_user_id==UUID(access.user_id),
                    MemoryScopeRow.scope_kind=="SESSION"))).scalars().all()
                exchanges=(await session.execute(select(MessageExchangeRow).where(
                    MessageExchangeRow.access_scope_id.in_([s.scope_id for s in source_scopes]),
                    MessageExchangeRow.source_identity.in_([r.source_identity for r in rows])))).scalars().all()
                for exchange in exchanges:
                    await self._suppress(session,exchange.access_scope_id,exchange.source_identity,access)
            operations = (await session.execute(select(MemoryOperationRow).where(or_(
                *(and_(MemoryOperationRow.scope_id == r.access_scope_id, MemoryOperationRow.source_identity == r.source_identity) for r in rows),
                *(MemoryOperationRow.dependencies.contains([{"kind": "RECORD", "id": mid}]) for mid in ids))).order_by(MemoryOperationRow.operation_id).with_for_update())).scalars().all()
            for operation in operations:
                if action == "DELETE":
                    operation.candidate_body = None
                if operation.kind != "SUPPRESS":
                    operation.state = "SKIPPED_REVOKED"
            receipt = {"outcome": "COMPLETE_FOR_DECLARED_SCOPE", "operation_id": oid, "authority_committed": True, "retrieval_blocked": True,
                "canonical_body_redacted": action == "DELETE", "derived_reuse_blocked": True,
                "scope_of_deletion": deletion_scope, "all_copies_erased": False, "cache": "NOT_PRESENT", "index": "DISABLED",
                "historical_payloads": "RETAINED_RESTRICTED_NO_REUSE", "backup_erasure": "UNKNOWN", "gaps": [],
                "expected_version": expected_version, "result_version": target.record_version}
            session.add(MemoryOperationRow(operation_id=oid, scope_id=scope_id, kind=action, actor_digest=digest(access.to_dict()),
                source_identity=target.source_identity, request_digest=request, expected_version=expected_version,
                result_version=target.record_version, record_id=memory_id, state="COMMITTED", dependencies=[], receipt=receipt))
            await session.flush()
            return receipt

    async def promote(self, access, source_id, project_scope_id, *, expected_version, command_id):
        async with self.database.transaction() as session:
            source = await session.get(LongTermMemoryRow, source_id)
            if source is None:
                raise MemoryContextError("MEMORY_SOURCE_INVALID")
            await self._locks(session, [source.access_scope_id, project_scope_id, *(d["scope_id"] for d in source.source_dependencies)])
            origin_scope = await self._scope(session, access, source.access_scope_id, "PROMOTE")
            project = await self._scope(session, access, project_scope_id, "PROMOTE")
            await self._scope(session, access, project_scope_id, "WRITE")
            if project.scope_kind != "PROJECT" or not project.policy.get("ttl_seconds"):
                raise MemoryContextError("MEMORY_PROJECT_GRANT_REQUIRED")
            source = await session.get(LongTermMemoryRow, source_id, with_for_update=True)
            deps = bounded_dependencies([*source.source_dependencies, self.reference(source, origin_scope)])
            await self._dependencies(session, deps)
            if source.record_version != expected_version or source.memory_type != "SEMANTIC" or not source.logical_key:
                raise MemoryContextError("MEMORY_VERSION_CONFLICT")
            if await self._suppressed(session,project_scope_id,source.source_identity):
                raise MemoryContextError("MEMORY_CONTEXT_REVOKED")
            oid = identity(project_scope_id, command_id, "PROMOTE")
            request = digest([source_id, expected_version, project_scope_id, command_id])
            old = await session.get(MemoryOperationRow, oid)
            if old:
                if old.request_digest != request:
                    raise MemoryContextError("MEMORY_OPERATION_CONFLICT")
                return old.receipt
            active = await session.scalar(select(ProjectSemanticMemoryRow.memory_id).where(
                ProjectSemanticMemoryRow.access_scope_id == project_scope_id, ProjectSemanticMemoryRow.logical_key == source.logical_key,
                ProjectSemanticMemoryRow.lifecycle_status == "ACTIVE"))
            if active:
                raise MemoryContextError("MEMORY_VERSION_CONFLICT")
            now = datetime.now(UTC)
            mid = identity(oid, 0)
            expiry = now + timedelta(seconds=project.policy["ttl_seconds"])
            if source.expires_at:
                expiry = min(expiry, source.expires_at)
            session.add(ProjectSemanticMemoryRow(memory_id=mid, project_id=project.project_id, scope_id=project_scope_id,
                access_scope_id=project_scope_id, owner_kind="PROJECT", owner_id=project.project_id, visibility="PROJECT",
                logical_key=source.logical_key, canonical_text=source.canonical_text, payload=source.payload, status="ACTIVE",
                origin_agent_id=access.agent_id, origin_run_id=source.origin_run_id, created_by_agent_id=access.agent_id,
                updated_by_agent_id=access.agent_id, source_memory_id=source_id, created_at=now.isoformat(), updated_at=now.isoformat(),
                record_version=1, expires_at=expiry, lifecycle_status="ACTIVE", body_state="PRESENT", body_digest=source.body_digest,
                source_identity=source.source_identity, source_dependencies=deps, epistemic_kind=source.epistemic_kind, memory_policy_version=1))
            receipt = {"outcome": "COMMITTED", "record_id": mid, "expected_version": expected_version, "result_version": 1}
            session.add(MemoryOperationRow(operation_id=oid, scope_id=project_scope_id, kind="PROMOTE", actor_digest=digest(access.to_dict()),
                source_identity=source.source_identity, request_digest=request, expected_version=expected_version,
                result_version=1, record_id=mid, dependencies=deps, state="COMMITTED", receipt=receipt))
            return receipt

    async def receipt(self, access, scope_id, operation_id):
        async with self.database.session() as session:
            await self._scope(session, access, scope_id)
            row = await session.get(MemoryOperationRow, operation_id)
            if row is None or row.scope_id != scope_id:
                return {"outcome": "UNKNOWN", "coverage": "PARTIAL", "gaps": ["OPERATION_UNCONFIRMED"]}
            return dict(row.receipt)

    async def project_scopes(self, access):
        async with self.database.session() as session:
            await self._actor(session, access)
            rows = (await session.execute(select(MemoryScopeRow).where(MemoryScopeRow.tenant_id == access.tenant_id,
                MemoryScopeRow.agent_id == access.agent_id, MemoryScopeRow.scope_kind == "PROJECT",
                MemoryScopeRow.status == "ACTIVE", MemoryScopeRow.grants[access.user_id].contains(["READ"])))).scalars().all()
            return [{"scope_id": r.scope_id, "epoch": r.authz_epoch, "kind": "PROJECT"} for r in rows]

    async def revoke_source(self, access, scope_id, operation_id, *, command_id):
        """未创建 canonical 的来源操作可撤销；已有记录必须走 record_version CAS。"""
        async with self.database.transaction() as session:
            initial = await session.get(MemoryOperationRow, operation_id)
            if initial is None or initial.scope_id != scope_id:
                raise MemoryContextError("MEMORY_OPERATION_UNKNOWN")
            await self._locks(session, [scope_id, *(d["scope_id"] for d in initial.dependencies)])
            scope=await self._scope(session, access, scope_id, "REVOKE")
            command=identity(scope_id,command_id,"REVOKE_SOURCE")
            request=digest([scope_id,operation_id,command_id,"REVOKE_SOURCE"])
            existing=await session.get(MemoryOperationRow,command,with_for_update=True)
            if existing:
                if existing.request_digest!=request:
                    raise MemoryContextError("MEMORY_OPERATION_CONFLICT")
                return existing.receipt
            row = await session.get(MemoryOperationRow, operation_id, with_for_update=True)
            if row.state=="COMMITTED":
                raise MemoryContextError("MEMORY_ACTION_UNSUPPORTED")
            await self._suppress(session, scope_id, row.source_identity, access)
            if scope.scope_kind in {"USER","SESSION"}:
                exchanges=(await session.execute(select(MessageExchangeRow).join(MemoryScopeRow,
                    MemoryScopeRow.scope_id==MessageExchangeRow.access_scope_id).where(
                    MessageExchangeRow.source_identity==row.source_identity,MemoryScopeRow.tenant_id==access.tenant_id,
                    MemoryScopeRow.agent_id==access.agent_id,MemoryScopeRow.owner_user_id==UUID(access.user_id),
                    MemoryScopeRow.scope_kind=="SESSION"))).scalars().all()
                for exchange in exchanges:
                    await self._suppress(session,exchange.access_scope_id,exchange.source_identity,access)
            row.state, row.candidate_body = "SKIPPED_REVOKED", None
            receipt={"outcome":"SUPPRESSED","operation_id":command}
            session.add(MemoryOperationRow(operation_id=command,scope_id=scope_id,kind="REVOKE_SOURCE",
                actor_digest=digest(access.to_dict()),source_identity=row.source_identity,request_digest=request,
                state="COMMITTED",receipt=receipt,dependencies=[]))
            return receipt

    async def delete_exchange(self, access, exchange_id, *, expected_version, command_id):
        sid = access.scope_id("SESSION")
        async with self.database.transaction() as session:
            scope_ids=(await session.execute(select(MemoryScopeRow.scope_id).where(MemoryScopeRow.tenant_id==access.tenant_id))).scalars().all()
            await self._locks(session, scope_ids)
            await self._scope(session, access, sid, "DELETE")
            row = await session.get(MessageExchangeRow, exchange_id, with_for_update=True)
            if row is None or row.access_scope_id != sid:
                raise MemoryContextError("MEMORY_RECORD_DENIED")
            oid = identity(sid, command_id, "DELETE_EXCHANGE")
            request = digest([sid, exchange_id, expected_version, command_id])
            old = await session.get(MemoryOperationRow, oid)
            if old:
                if old.request_digest != request:
                    raise MemoryContextError("MEMORY_OPERATION_CONFLICT")
                return old.receipt
            if row.record_version != expected_version:
                raise MemoryContextError("MEMORY_VERSION_CONFLICT")
            row.lifecycle_status, row.body_state, row.record_version = "DELETED", "REDACTED", row.record_version + 1
            await session.execute(update(MessageRow).where(MessageRow.exchange_id == exchange_id,
                MessageRow.access_scope_id == sid).values(content="", metadata_json=None, lifecycle_status="DELETED",
                    body_state="REDACTED", record_version=MessageRow.record_version+1))
            await self._suppress(session, sid, row.source_identity, access)
            for model in (LongTermMemoryRow, ProjectSemanticMemoryRow):
                descendants=(await session.execute(select(model).where(or_(model.source_dependencies.contains(
                    [{"kind":"EXCHANGE","id":exchange_id}]),model.source_dependencies.contains(
                    [{"kind":"USER_MESSAGE","id":str(row.user_message_id)}]))).order_by(model.memory_id).with_for_update())).scalars().all()
                for record in descendants:
                    record.lifecycle_status,record.body_state="DELETED","REDACTED"
                    record.canonical_text,record.payload,record.record_version="",{},record.record_version+1
                    await self._suppress(session,record.access_scope_id,record.source_identity,access)
            candidates=(await session.execute(select(MemoryOperationRow).where(MemoryOperationRow.kind=="FORMATION",
                or_(MemoryOperationRow.source_identity==row.source_identity,
                    MemoryOperationRow.dependencies.contains([{"kind":"EXCHANGE","id":exchange_id}]),
                    MemoryOperationRow.dependencies.contains([{"kind":"USER_MESSAGE","id":str(row.user_message_id)}]))
                ).order_by(MemoryOperationRow.operation_id).with_for_update())).scalars().all()
            for candidate in candidates:
                candidate.candidate_body,candidate.state=None,"SKIPPED_REVOKED"
            # 不改写冻结执行正文；全部来源闭包由 SQL 即时禁止后续使用。
            receipt = {"outcome": "COMPLETE_FOR_DECLARED_SCOPE", "all_copies_erased": False,
                "scope_of_deletion": "MEMORY_STORE_AND_FUTURE_USE", "retrieval_blocked": True,
                "historical_payloads": "RETAINED_RESTRICTED_NO_REUSE", "backup_erasure": "UNKNOWN",
                "expected_version": expected_version, "result_version": row.record_version}
            session.add(MemoryOperationRow(operation_id=oid, scope_id=sid, kind="DELETE_EXCHANGE",
                source_identity=row.source_identity, actor_digest=digest(access.to_dict()), request_digest=request,
                expected_version=expected_version, result_version=row.record_version, state="COMMITTED", receipt=receipt))
            return receipt

    async def evidence(self, access, run_id):
        from core.runtime.memory_evidence import build_memory_evidence
        from core.persistence.models import RuntimeContextSnapshotRow
        async with self.database.transaction() as session:
            await self._actor(session, access)
            binding = await session.get(RuntimeMemoryBindingRow, run_id)
            if binding is None or binding.access != access.to_dict():
                raise MemoryContextError("MEMORY_RECORD_DENIED")
            ids = sorted(set([s["scope_id"] for s in binding.scopes] + [d["scope_id"] for d in binding.manifest]))
            await self._locks(session, ids)
            # 已撤销的原 Owner 可查询自己的审计证据，但不能重新获得使用资格。
            for sid in ids:
                row = await session.get(MemoryScopeRow, sid)
                if row.tenant_id != access.tenant_id or row.agent_id != access.agent_id:
                    raise MemoryContextError("MEMORY_SCOPE_DENIED")
                if (sid in {s["scope_id"] for s in binding.scopes} and row.scope_kind != "PROJECT"
                        and str(row.owner_user_id) != access.user_id):
                    raise MemoryContextError("MEMORY_SCOPE_DENIED")
            binding = await session.get(RuntimeMemoryBindingRow, run_id, with_for_update=True)
            if binding.evidence is not None:
                return {"body":binding.evidence,"digest":binding.evidence_digest}
            scopes = (await session.execute(select(MemoryScopeRow).where(MemoryScopeRow.scope_id.in_(ids)).order_by(MemoryScopeRow.scope_id))).scalars().all()
            record_ids=[d["id"] for d in binding.manifest if d["kind"]=="RECORD"]
            records=[]
            for model in (LongTermMemoryRow,ProjectSemanticMemoryRow):
                records.extend((await session.execute(select(model).where(or_(model.memory_id.in_(record_ids),and_(model.origin_run_id==run_id,model.access_scope_id.in_(ids)))).order_by(model.memory_id))).scalars().all())
            operations=(await session.execute(select(MemoryOperationRow).where(MemoryOperationRow.scope_id.in_(ids),
                or_(MemoryOperationRow.record_id.in_(record_ids),MemoryOperationRow.source_identity.in_([r.source_identity for r in records]),
                    MemoryOperationRow.source_identity.in_(select(MessageExchangeRow.source_identity).where(MessageExchangeRow.run_id==run_id)))).order_by(MemoryOperationRow.operation_id))).scalars().all()
            snapshots=(await session.execute(select(RuntimeContextSnapshotRow).where(RuntimeContextSnapshotRow.run_id==run_id).order_by(RuntimeContextSnapshotRow.version))).scalars().all()
            refs=[{"snapshot_ref":r.snapshot_id,"digest":r.snapshot_digest} for r in snapshots]
            body=build_memory_evidence(binding,scopes,records,operations,refs)
            for fact,record in zip(body["records"],records):
                model=type(record)
                fact["lifecycle_eligible_at_seal"]=bool(await session.scalar(select(model.memory_id).where(
                    model.memory_id==record.memory_id,self.visible(model))))
            from core.persistence.models import RuntimeModelInvocationRow
            models=(await session.execute(select(RuntimeModelInvocationRow).where(RuntimeModelInvocationRow.run_id==run_id
                ).order_by(RuntimeModelInvocationRow.step_id,RuntimeModelInvocationRow.attempt_number,RuntimeModelInvocationRow.model_attempt_number))).scalars().all()
            eligible=bool(await session.scalar(select(func.memory_dependencies_valid(cast(binding.manifest,JSONB)))))
            eligible=eligible and binding.status=="ACTIVE" and all(s.status=="ACTIVE" for s in scopes)
            for item in binding.scopes:
                current=next((r for r in scopes if r.scope_id==item["scope_id"]),None)
                eligible=eligible and current is not None and current.authz_epoch==item["epoch"]
                if current is not None and current.scope_kind=="PROJECT":
                    eligible=eligible and "READ" in current.grants.get(access.user_id,[])
            body["eligibility_at_seal"]="VALID" if eligible else "INVALID"
            body["model_invocations"]=[{"attempt_ref":digest([r.run_id,r.step_id,r.attempt_number,r.model_attempt_number]),
                "purpose":r.purpose,"state":r.state,"request_digest":r.request_digest,
                "formation_operation_ref":r.memory_operation_id} for r in models]
            admissions={a["id"] for a in binding.admissions}
            for model in models:
                if model.state in {"STARTED","UNKNOWN"}:
                    body["coverage"]="PARTIAL"; body["gaps"].append("MODEL_OUTCOME_UNCONFIRMED")
                if f"model:{model.step_id}:{model.attempt_number}:{model.model_attempt_number}" not in admissions:
                    body["coverage"]="PARTIAL"; body["gaps"].append("MODEL_BINDING_MISSING")
                if model.state=="COMPLETED" and digest(model.result_binding)!=model.result_digest:
                    body["coverage"]="PARTIAL"; body["gaps"].append("MODEL_RESULT_UNVERIFIABLE")
            body["gaps"]=sorted(set(body["gaps"]))
            root = await session.get(RuntimeRunExecutionRow, run_id)
            if root is None or root.status not in {"SUCCEEDED","FAILED","CANCELLED"}:
                body["coverage"]="PARTIAL"; body["gaps"]=sorted(set([*body["gaps"],"RUN_NOT_TERMINAL"]))
            if not set(record_ids) <= {r.memory_id for r in records}:
                body["coverage"]="PARTIAL"; body["gaps"]=sorted(set([*body["gaps"],"RECORD_PROOF_MISSING"]))
            binding.evidence,binding.evidence_digest=body,digest(body)
            return {"body":body,"digest":binding.evidence_digest}

    async def completed_formation_response(self, access, operation_id):
        from core.persistence.models import RuntimeModelInvocationRow
        from core.runtime.model_invocation import restore_loop_model_response
        async with self.database.transaction() as session:
            operation = await session.get(MemoryOperationRow, operation_id)
            if operation is None:
                raise MemoryContextError("MEMORY_OPERATION_UNKNOWN")
            await self._locks(session, [operation.scope_id, *(d["scope_id"] for d in operation.dependencies)])
            await self._scope(session, access, operation.scope_id, "WRITE")
            await self._dependencies(session, operation.dependencies)
            if await self._suppressed(session, operation.scope_id, operation.source_identity):
                raise MemoryContextError("MEMORY_CONTEXT_REVOKED")
            operation = await session.get(MemoryOperationRow, operation_id, with_for_update=True)
            if operation.state not in {"UNKNOWN","PREPARED"}:
                return None
            row = (await session.execute(select(RuntimeModelInvocationRow).where(
                RuntimeModelInvocationRow.memory_operation_id==operation_id,
                RuntimeModelInvocationRow.purpose=="FORMATION",RuntimeModelInvocationRow.state=="COMPLETED"
                ).order_by(RuntimeModelInvocationRow.model_attempt_number.desc()).limit(1))).scalar_one_or_none()
            if row is None:
                return None
            response=restore_loop_model_response(row)
            operation.state="PREPARED"
            return response.output

    async def finish_admission(self, lease, admission, *, state, result_digest=None):
        """只保存原 Model Owner 的 pre-root 完成事实；不授权新调用。"""
        if state not in {"COMPLETED","UNKNOWN"}:
            raise MemoryContextError("MEMORY_ADMISSION_INVALID")
        async with self.database.transaction() as session:
            await self._control(session, lease)
            await session.get(RuntimeRunExecutionRow, lease.run_id, with_for_update=True)
            binding=await session.get(RuntimeMemoryBindingRow, lease.run_id)
            if binding is None:
                raise MemoryContextError("MEMORY_CONTEXT_UNVERIFIABLE")
            await self._locks(session,[s["scope_id"] for s in binding.scopes]+[d["scope_id"] for d in binding.manifest])
            binding=await session.get(RuntimeMemoryBindingRow, lease.run_id, with_for_update=True)
            facts=[dict(a) for a in binding.admissions]
            matching=[a for a in facts if a["id"]==admission]
            if len(matching)!=1 or matching[0].get("state")!="STARTED":
                raise MemoryContextError("MEMORY_ADMISSION_INVALID")
            matching[0].update(state=state,result_digest=result_digest)
            binding.admissions=facts
            binding.row_version+=1
