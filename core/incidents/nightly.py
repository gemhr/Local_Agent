"""夜间演练的短业务事务：明确目标、逐例追加、一次封存。"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4, UUID
from sqlalchemy import select, or_
from sqlalchemy.dialects.postgresql import insert
from core.ci_simulation import nightly as rules
from core.ci_simulation.service import CIError, persist_nightly_case
from core.ci_simulation.models import NightlyCIBatchRow as Batch, NightlyVersionExecutionRow as Version, CIExecutionRow
from core.incidents.models import IncidentRow
from core.incidents.service import require_human
from core.persistence.models import UserRow
from core.runtime.snapshot_serialization import sha256_digest
from core.incidents import grouping, notifications


def controller(principal):
    require_human(principal)
    return bool(principal.roles & {"OPERATOR", "ADMIN"})


def can_manage(principal, batch):
    return controller(principal) and batch.created_by_user_id == principal.user_id


async def scope(session, principal, batch_id, *, manage=False, lock=False):
    require_human(principal)
    query = select(Batch).where(Batch.batch_id == batch_id, Batch.tenant_id == principal.tenant_id)
    batch = (await session.execute(query.with_for_update() if lock else query)).scalar_one_or_none()
    if batch is None:
        raise CIError("CI_NOT_FOUND", 404)
    allowed = can_manage(principal, batch)
    if not allowed and not any(e["owner_user_id"] == str(principal.user_id) for e in batch.frozen_plan["environments"]):
        raise CIError("CI_NOT_FOUND", 404)
    if manage and not allowed:
        raise CIError("CI_NIGHTLY_CONTROL_REQUIRED", 403)
    return batch, allowed


async def options(service, principal):
    if not controller(principal):
        raise CIError("CI_NIGHTLY_CONTROL_REQUIRED", 403)
    async with service.database.session() as session:
        owners = (await session.execute(select(UserRow).where(UserRow.tenant_id == principal.tenant_id,
            UserRow.disabled_at.is_(None), UserRow.principal_kind == "HUMAN").order_by(UserRow.id))).scalars().all()
        return dict(environments=rules.ENVIRONMENTS, profiles=list(rules.PROFILES), versions=rules.VERSIONS,
            duration_profiles=rules.DURATIONS, owners=[dict(user_id=str(u.id), display_name=u.display_name or str(u.id)) for u in owners])


async def create(service, principal, body):
    if not controller(principal):
        raise CIError("CI_NIGHTLY_CONTROL_REQUIRED", 403)
    intent = body.model_dump(mode="json")
    digest = sha256_digest(intent)
    async with service.database.transaction() as session:
        existing = (await session.execute(select(Batch).where(Batch.tenant_id == principal.tenant_id,
            Batch.created_by_user_id == principal.user_id,
            or_(Batch.request_id == str(body.request_id), (Batch.business_date == body.business_date) & (Batch.plan_key == body.plan_key))).limit(1))).scalar_one_or_none()
        if existing:
            if existing.plan_digest != digest:
                raise CIError("CI_REQUEST_CONFLICT", 409)
            return dict(batch_id=existing.batch_id, created=False)
        ids = {e.owner_user_id for e in body.environments}
        owners = (await session.execute(select(UserRow.id).where(UserRow.id.in_(ids), UserRow.tenant_id == principal.tenant_id,
            UserRow.disabled_at.is_(None), UserRow.principal_kind == "HUMAN").with_for_update())).scalars().all()
        if set(owners) != ids:
            raise CIError("CI_NIGHTLY_OWNER_INVALID", 422)
        plan = dict(environments=[rules.expand_environment(e) for e in body.environments])
        now = datetime.now(timezone.utc)
        values = dict(batch_id=str(uuid4()), tenant_id=principal.tenant_id, created_by_user_id=principal.user_id,
            request_id=str(body.request_id), business_date=body.business_date, plan_key=body.plan_key, plan_version="1",
            plan_digest=digest, frozen_plan=plan, simulated_now=rules.scheduled_start(body.business_date) - timedelta(minutes=5),
            status="PLANNED", created_at=now)
        inserted = (await session.execute(insert(Batch).values(**values).on_conflict_do_nothing().returning(Batch.batch_id))).scalar_one_or_none()
        if not inserted:
            retry = (await session.execute(select(Batch).where(Batch.tenant_id == principal.tenant_id,
                Batch.created_by_user_id == principal.user_id, or_(Batch.request_id == str(body.request_id),
                (Batch.business_date == body.business_date) & (Batch.plan_key == body.plan_key))).limit(1))).scalar_one_or_none()
            if retry is None or retry.plan_digest != digest:
                raise CIError("CI_REQUEST_CONFLICT", 409)
            return dict(batch_id=retry.batch_id, created=False)
        for env in plan["environments"]:
            for ordinal, version in enumerate(env["versions"], 1):
                session.add(Version(version_execution_id=str(uuid4()), batch_id=inserted, environment_id=env["environment_id"],
                    owner_user_id=UUID(env["owner_user_id"]), ordinal=ordinal, software_version=version["software_version"],
                    frozen_cases=version["frozen_cases"], status="PENDING", next_case_index=0, created_at=now, updated_at=now))
        await session.flush()
        return dict(batch_id=inserted, created=True)


async def versions(session, batch_id):
    return (await session.execute(select(Version).where(Version.batch_id == batch_id)
        .order_by(Version.environment_id, Version.ordinal))).scalars().all()


async def original_facts(session, batch_id):
    return (await session.execute(select(CIExecutionRow, Version, IncidentRow).join(Version,
        CIExecutionRow.nightly_version_execution_id == Version.version_execution_id)
        .outerjoin(IncidentRow, IncidentRow.execution_id == CIExecutionRow.execution_id)
        .where(Version.batch_id == batch_id).order_by(Version.environment_id, Version.ordinal, CIExecutionRow.case_id))).all()


def fact_payload(execution, version, incident):
    return dict(execution_id=execution.execution_id, version_execution_id=version.version_execution_id,
        environment_id=version.environment_id, owner_user_id=str(version.owner_user_id), software_version=version.software_version,
        case_id=execution.case_id, outcome=execution.outcome, incident_id=incident.incident_id if incident else None,
        business_completed_at=execution.business_completed_at.isoformat(), recorded_at=execution.completed_at.isoformat())


def summarize(facts):
    items = [fact_payload(*f) for f in facts]
    result = dict(total=len(items), passed=sum(i["outcome"] == "PASS" for i in items), failed=sum(i["outcome"] == "FAIL" for i in items), items=items)
    for key in ("environment_id", "version_execution_id", "owner_user_id"):
        result["by_" + key] = {value: dict(total=sum(i[key] == value for i in items),
            failed=sum(i[key] == value and i["outcome"] == "FAIL" for i in items)) for value in sorted({i[key] for i in items})}
    return result


async def detail_in_session(session, principal, batch, manager):
    all_versions = await versions(session, batch.batch_id)
    visible = [v for v in all_versions if manager or v.owner_user_id == principal.user_id]
    facts = [f for f in await original_facts(session, batch.batch_id) if manager or f[1].owner_user_id == principal.user_id]
    items = []
    for version in visible:
        steps = []
        if manager and batch.status != "COMPLETED":
            if version.status == "PENDING" and all(v.status == "COMPLETED" for v in all_versions if v.environment_id == version.environment_id and v.ordinal < version.ordinal):
                steps = [dict(version_execution_id=version.version_execution_id, kind="START")]
            elif version.status == "RUNNING":
                steps = [dict(version_execution_id=version.version_execution_id, kind="COMPLETE_CASE", case_id=version.frozen_cases[version.next_case_index]["case_id"])]
        slots = []
        for index, slot in enumerate(version.frozen_cases):
            found = next((fact_payload(*f) for f in facts if f[1].version_execution_id == version.version_execution_id and f[0].case_id == slot["case_id"]), None)
            slots.append(dict(case_id=slot["case_id"], duration_minutes=slot["duration_minutes"],
                due_at=rules.case_due(version, index).isoformat() if version.started_business_at else None, result=found))
        items.append(dict(version_execution_id=version.version_execution_id, environment_id=version.environment_id,
            owner_user_id=str(version.owner_user_id), ordinal=version.ordinal, software_version=version.software_version,
            status=version.status, started_business_at=version.started_business_at, completed_business_at=version.completed_business_at,
            started_by_user_id=version.started_by_user_id, slots=slots, next_steps=steps))
    frozen = batch.sealed_summary if manager else summarize([f for f in facts])
    return dict(batch_id=batch.batch_id, business_date=batch.business_date, status=batch.status, can_manage=manager,
        scope="MANAGEMENT" if manager else "OWN_ONLY", simulated_now=batch.simulated_now,
        scheduled_start=rules.scheduled_start(batch.business_date), created_at=batch.created_at,
        frozen_plan=dict(environments=[e for e in batch.frozen_plan["environments"] if manager or e["owner_user_id"] == str(principal.user_id)]),
        versions=items, provisional_progress=summarize(facts), frozen_summary=frozen if batch.status == "COMPLETED" else None,
        sealed_digest=batch.sealed_digest if manager else (sha256_digest(frozen) if batch.status == "COMPLETED" else None),
        current_todos=[dict(incident_id=f[2].incident_id, status=f[2].status) for f in facts if f[2] and f[2].status != "CLOSED"])


async def detail(service, principal, batch_id):
    async with service.database.session() as session:
        batch, manager = await scope(session, principal, batch_id)
        return await detail_in_session(session, principal, batch, manager)


async def list_batches(service, principal):
    require_human(principal)
    async with service.database.session() as session:
        batches = (await session.execute(select(Batch).where(Batch.tenant_id == principal.tenant_id).order_by(Batch.created_at.desc()))).scalars().all()
        return dict(can_create=controller(principal), items=[dict(batch_id=b.batch_id, business_date=b.business_date, status=b.status,
            can_manage=can_manage(principal, b)) for b in batches if can_manage(principal, b) or any(e["owner_user_id"] == str(principal.user_id) for e in b.frozen_plan["environments"])])


async def clock(service, principal, batch_id, body):
    async with service.database.transaction() as session:
        batch, _ = await scope(session, principal, batch_id, manage=True, lock=True)
        if batch.status == "COMPLETED":
            raise CIError("CI_NIGHTLY_SEALED", 409)
        if body.target_business_time < batch.simulated_now:
            raise CIError("CI_NIGHTLY_CLOCK_BACKWARDS", 409)
        batch.simulated_now = body.target_business_time
        return dict(batch_id=batch_id, simulated_now=batch.simulated_now)


async def advance(service, principal, batch_id, body):
    async with service.database.transaction() as session:
        batch, _ = await scope(session, principal, batch_id, manage=True, lock=True)
        all_versions = await versions(session, batch_id)
        by_id = {v.version_execution_id: v for v in all_versions}
        targets = [by_id.get(s.version_execution_id) for s in body.steps]
        if any(v is None for v in targets):
            raise CIError("CI_NOT_FOUND", 404)
        if len({v.environment_id for v in targets}) != len(targets):
            raise CIError("CI_NIGHTLY_DUPLICATE_ENVIRONMENT", 422)
        results = []
        for step in sorted(body.steps, key=lambda s: s.version_execution_id):
            version = await session.get(Version, step.version_execution_id, with_for_update=True)
            if step.kind == "START" and version.status != "PENDING":
                results.append(dict(version_execution_id=version.version_execution_id, reused=True, started_business_at=version.started_business_at))
                continue
            old = (await session.execute(select(CIExecutionRow).where(CIExecutionRow.nightly_version_execution_id == version.version_execution_id,
                CIExecutionRow.case_id == step.case_id))).scalar_one_or_none() if step.kind == "COMPLETE_CASE" else None
            if old:
                incident = (await session.execute(select(IncidentRow).where(IncidentRow.execution_id == old.execution_id))).scalar_one_or_none()
                results.append(dict(**fact_payload(old, version, incident), reused=True))
                continue
            if batch.status == "COMPLETED":
                raise CIError("CI_NIGHTLY_SEALED", 409)
            now = datetime.now(timezone.utc)
            if step.kind == "START":
                if batch.simulated_now < rules.scheduled_start(batch.business_date):
                    raise CIError("CI_NIGHTLY_NOT_DUE", 409)
                if any(v.environment_id == version.environment_id and v.ordinal < version.ordinal and v.status != "COMPLETED" for v in all_versions):
                    raise CIError("CI_NIGHTLY_ORDER_CONFLICT", 409)
                version.status, version.started_business_at, version.started_by_user_id = "RUNNING", batch.simulated_now, principal.user_id
                batch.status = "RUNNING"
                results.append(dict(version_execution_id=version.version_execution_id, reused=False, started_business_at=version.started_business_at))
            else:
                if version.status != "RUNNING" or version.frozen_cases[version.next_case_index]["case_id"] != step.case_id:
                    raise CIError("CI_NIGHTLY_ORDER_CONFLICT", 409)
                if batch.simulated_now < rules.case_due(version, version.next_case_index):
                    raise CIError("CI_NIGHTLY_NOT_DUE", 409)
                execution = await persist_nightly_case(session, batch, version, version.frozen_cases[version.next_case_index])
                incident = None
                if execution.outcome == "FAIL":
                    incident = IncidentRow(incident_id=str(uuid4()), execution_id=execution.execution_id, tenant_id=batch.tenant_id,
                        owner_user_id=version.owner_user_id, title="夜间 CI 失败 · " + execution.case_id, status="OPEN",
                        session_id=str(uuid4()), created_at=now)
                    session.add(incident)
                    await session.flush()
                    await grouping.observe(session, batch, execution, incident)
                    await notifications.create(session, batch, version.owner_user_id, "INCREMENTAL_FAILURE")
                version.next_case_index += 1
                if version.next_case_index == 2:
                    version.status, version.completed_business_at = "COMPLETED", batch.simulated_now
                results.append(dict(**fact_payload(execution, version, incident), reused=False))
            version.updated_at = now
            await session.flush()
        if batch.status != "COMPLETED" and all(v.status == "COMPLETED" for v in all_versions):
            facts = await original_facts(session, batch_id)
            expected = {(v.version_execution_id, s["case_id"]) for v in all_versions for s in v.frozen_cases}
            if {(f[0].nightly_version_execution_id, f[0].case_id) for f in facts} != expected or len(facts) != len(expected):
                raise CIError("CI_NIGHTLY_SEAL_INCOMPLETE", 409)
            batch.sealed_summary = summarize(facts)
            batch.sealed_digest = sha256_digest(batch.sealed_summary)
            batch.sealed_at, batch.status = datetime.now(timezone.utc), "COMPLETED"
            for owner in {v.owner_user_id for v in all_versions}:
                await notifications.create(session, batch, owner, "FINAL_SUMMARY")
        return dict(batch_id=batch_id, status=batch.status, results=results)


async def failures(service, principal, batch_id):
    async with service.database.session() as session:
        batch, manager = await scope(session, principal, batch_id)
        items = []
        for execution, version, incident in await original_facts(session, batch_id):
            if not incident or (not manager and incident.owner_user_id != principal.user_id):
                continue
            own = incident.owner_user_id == principal.user_id
            error = next((s for s in execution.summary["stages"] if s.get("error")), None)
            signature, reason, refs = grouping.signature(execution)
            items.append(dict(**fact_payload(execution, version, incident), status=incident.status, first_failure=error,
                grouping_status="ELIGIBLE" if signature else reason, can_open=own,
                evidence_refs=refs if own else [], source="DETERMINISTIC_EVIDENCE_MATCH"))
        return dict(items=items, provisional=batch.status != "COMPLETED")
