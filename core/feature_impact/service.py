"""业务事务和服务端身份边界；不读取评分参考。"""
from datetime import datetime, timezone
from uuid import uuid4
import json
from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert
from core.feature_impact.models import TaskRow, ReportRow, PlanRow, ExecutionRow
from core.feature_impact.knowledge import development_input, digest, read, search
from core.feature_impact.reports import rule_report, validate, RULE_VERSION
from core.feature_impact.resource_simulation import execute
from core.agent_platform.ci_diagnosis_tools import strict_object


class BusinessError(ValueError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def human(principal):
    if principal.principal_kind != "HUMAN":
        raise BusinessError("FEATURE_HUMAN_REQUIRED", 403)


def now():
    return datetime.now(timezone.utc).isoformat()


def report_payload(row):
    return dict(**row.envelope, status="REJECTED" if row.rejection else "SEALED", raw_output=row.raw_output,
        report=row.payload, rejection=row.rejection, source_record=row.source_record)


class FeatureImpactService:
    def __init__(self, database):
        self.database = database

    async def owned(self, session, principal, task_id, lock=False):
        human(principal)
        q = select(TaskRow).where(TaskRow.task_id == task_id, TaskRow.tenant_id == principal.tenant_id, TaskRow.owner_user_id == principal.user_id)
        row = (await session.execute(q.with_for_update().execution_options(populate_existing=True) if lock else q)).scalar_one_or_none()
        if row is None:
            raise BusinessError("FEATURE_NOT_FOUND", 404)
        return row

    async def create(self, principal, request_id, *, public_task=None):
        human(principal)
        if public_task is not None:
            if not principal.roles.intersection({'OPERATOR', 'ADMIN'}):
                raise BusinessError('FEATURE_EXPERIMENT_ROLE_REQUIRED', 403)
            from core.feature_impact.public_tasks import load_public_task
            snapshot = load_public_task(public_task)
        else:
            snapshot = development_input()
        async with self.database.transaction() as session:
            await session.execute(insert(TaskRow).values(task_id=str(uuid4()), tenant_id=principal.tenant_id,
                owner_user_id=principal.user_id, request_id=request_id, snapshot=snapshot, created_at=datetime.now(timezone.utc))
                .on_conflict_do_nothing(index_elements=["tenant_id", "owner_user_id", "request_id"]))
            row = (await session.execute(select(TaskRow).where(TaskRow.tenant_id == principal.tenant_id,
                TaskRow.owner_user_id == principal.user_id, TaskRow.request_id == request_id))).scalar_one()
            if row.snapshot['input_digest'] != snapshot['input_digest']:
                raise BusinessError('REQUEST_CONFLICT')
            return self.task_payload(row)

    @staticmethod
    def task_payload(row):
        return dict(task_id=row.task_id, **row.snapshot, agent_execution="NOT_EXECUTED", model_budget="KNOWN_DEFERRED_CONSTRAINT",
            exposed=row.exposed_at is not None)

    async def tasks(self, principal):
        human(principal)
        async with self.database.session() as session:
            rows = (await session.execute(select(TaskRow).where(TaskRow.tenant_id == principal.tenant_id,
                TaskRow.owner_user_id == principal.user_id).order_by(TaskRow.created_at.desc()).limit(50))).scalars().all()
            return dict(items=[{k: v for k, v in self.task_payload(r).items() if k != "documents"} for r in rows])

    async def task(self, principal, task_id, query=None, source_ref=None):
        async with self.database.session() as session:
            row = await self.owned(session, principal, task_id)
            if source_ref is not None:
                return read(row.snapshot, source_ref)
            if query is not None:
                return dict(items=search(row.snapshot, query))
            result = self.task_payload(row)
            reports = (await session.execute(select(ReportRow).where(ReportRow.task_id == task_id).order_by(ReportRow.report_version))).scalars().all()
            result["reports"] = [dict(status="REJECTED" if r.rejection else "SEALED", **r.envelope) for r in reports]
            return result

    async def submit(self, principal, task_id, request_id, raw=None, *, rule=False, agent_result=None):
        async with self.database.transaction() as session:
            task = await self.owned(session, principal, task_id, lock=True)
            intent = digest(dict(rule=rule, raw=raw, agent_run_id=agent_result[0].run_id if agent_result else None))
            old = (await session.execute(select(ReportRow).where(ReportRow.task_id == task_id, ReportRow.request_id == request_id))).scalar_one_or_none()
            if old:
                if old.intent_digest != intent:
                    raise BusinessError("REQUEST_CONFLICT")
                return report_payload(old)
            if task.exposed_at is not None:
                raise BusinessError("PLANNING_ALREADY_EXPOSED_CREATE_NEW_TASK")
            kind, source, error, parsed = "RULE" if rule else "HUMAN", {}, None, None
            if rule:
                value, source = rule_report(task.snapshot)
                raw = json.dumps(value, ensure_ascii=False, sort_keys=True)
            try:
                if agent_result:
                    from core.feature_impact.agent_reports import validated_agent_report, require_binding_owner
                    kind = "AGENT"
                    binding, image, actions = agent_result
                    from core.feature_impact.models import InvestigationRow
                    trusted = await session.get(InvestigationRow, binding.run_id)
                    if (trusted is None or trusted.task_id != task_id or trusted.session_id != binding.session_id):
                        raise ValueError("AGENT_RUN_OWNER_INVALID")
                    await require_binding_owner(session, trusted, task)
                    raw, _, source = validated_agent_report(trusted, image, actions, task)
                elif not rule:
                    source = dict(task_input_sources=[d["source_ref"] for d in task.snapshot["documents"]], submitted_by=str(principal.user_id))
                parsed = validate(strict_object(raw), task.snapshot, kind=kind, reads=source.get("read_sources", []), task_inputs=source.get("task_input_sources", []))
            except (ValueError, TypeError, KeyError):
                error = "REPORT_SCHEMA_OR_EVIDENCE_INVALID"
            version = (await session.execute(select(func.max(ReportRow.report_version)).where(ReportRow.task_id == task_id))).scalar() or 0
            report_id = str(uuid4())
            envelope = dict(schema_version="feature-impact-report.v1", report_id=report_id, report_version=version + 1,
                task_id=task_id, task_version=task.snapshot["task_version"], input_digest=task.snapshot["input_digest"],
                corpus_digest=task.snapshot["corpus_digest"], candidate_kind=kind, candidate_version="1.1.0" if agent_result else RULE_VERSION if rule else "human.v1",
                run_id=agent_result[0].run_id if agent_result else None, sealed_at=now(), report_digest=digest(parsed if parsed else raw),
                discovery_origins={f["finding_id"]: ("DEVELOPER_KNOWN" if f["historical_feature_id"] in task.snapshot["known_impacts"] else "UNPROMPTED_CANDIDATE") for f in (parsed or {}).get("findings", [])})
            row = ReportRow(report_id=report_id, task_id=task_id, request_id=request_id, intent_digest=intent, report_version=version + 1,
                envelope=envelope, raw_output=raw, payload=parsed, rejection=error, source_record=source)
            session.add(row)
            return report_payload(row)

    async def owned_report(self, session, principal, report_id, lock=False):
        report = await session.get(ReportRow, report_id)
        if report is None:
            raise BusinessError("FEATURE_NOT_FOUND", 404)
        task = await self.owned(session, principal, report.task_id, lock=lock)
        return task, report

    async def report(self, principal, report_id):
        async with self.database.session() as session:
            _, row = await self.owned_report(session, principal, report_id)
            result = report_payload(row)
            plan = (await session.execute(select(PlanRow).where(PlanRow.report_id == report_id))).scalar_one_or_none()
            result["coverage_plan"] = plan.payload if plan else None
            result["executions"] = [dict(execution_id=e.execution_id, test_id=e.result["test_id"]) for e in
                (await session.execute(select(ExecutionRow).where(ExecutionRow.plan_id == plan.plan_id))).scalars().all()] if plan else []
            return result

    async def review(self, principal, report_id, body):
        async with self.database.transaction() as session:
            task, report = await self.owned_report(session, principal, report_id, lock=True)
            intent = digest(body)
            old = (await session.execute(select(PlanRow).where(PlanRow.report_id == report_id))).scalar_one_or_none()
            if old:
                if old.request_id != body["request_id"] or old.intent_digest != intent:
                    raise BusinessError("PLAN_ALREADY_SEALED")
                return old.payload
            latest = (await session.execute(select(func.max(ReportRow.report_version)).where(ReportRow.task_id == task.task_id))).scalar()
            if report.rejection or body["report_digest"] != report.envelope["report_digest"] or body["report_version"] != report.report_version or report.report_version != latest:
                raise BusinessError("REPORT_VERSION_CONFLICT")
            if task.exposed_at:
                raise BusinessError("PLANNING_ALREADY_EXPOSED")
            findings = {f["finding_id"]: f for f in report.payload["findings"]}
            decisions, selected, seen = [], [], set()
            for d in body["decisions"]:
                if d["finding_id"] not in findings or d["finding_id"] in seen:
                    raise BusinessError("REVIEW_FINDING_INVALID")
                seen.add(d["finding_id"])
                decisions.append(d)
                if d["decision"] == "ACCEPT":
                    selected.extend(dict(finding_id=d["finding_id"], source="CANDIDATE", **t) for t in findings[d["finding_id"]]["recommended_tests"])
            if set(findings) != seen:
                raise BusinessError("REVIEW_INCOMPLETE")
            plan_id = str(uuid4())
            payload = dict(plan_id=plan_id, plan_version=1, task_id=task.task_id, report_id=report_id,
                report_version=report.report_version, report_digest=report.envelope["report_digest"],
                reviewer=str(principal.user_id), sealed_at=now(), decisions=decisions, tests=selected)
            payload["plan_digest"] = digest(payload)
            session.add(PlanRow(plan_id=plan_id, report_id=report_id, request_id=body["request_id"], intent_digest=intent, payload=payload))
            return payload

    async def plan(self, principal, plan_id):
        async with self.database.session() as session:
            row = await session.get(PlanRow, plan_id)
            if row is None:
                raise BusinessError("FEATURE_NOT_FOUND", 404)
            await self.owned_report(session, principal, row.report_id)
            return row.payload

    async def execute(self, principal, plan_id, body):
        async with self.database.transaction() as session:
            plan = await session.get(PlanRow, plan_id)
            if plan is None:
                raise BusinessError("FEATURE_NOT_FOUND", 404)
            task, _ = await self.owned_report(session, principal, plan.report_id, lock=True)
            intent = digest(body)
            old = (await session.execute(select(ExecutionRow).where(ExecutionRow.plan_id == plan_id, ExecutionRow.request_id == body["request_id"]))).scalar_one_or_none()
            if old:
                if old.intent_digest != intent:
                    raise BusinessError("REQUEST_CONFLICT")
                return old.result
            if body["plan_digest"] != plan.payload["plan_digest"]:
                raise BusinessError("PLAN_VERSION_CONFLICT")
            test = next((t for t in plan.payload["tests"] if t["test_id"] == body["test_id"]), None)
            if test is None:
                raise BusinessError("TEST_NOT_APPROVED")
            result, trace = execute(test["policy"], test["operations"])
            result.update(execution_id=str(uuid4()), plan_id=plan_id, plan_version=1, plan_digest=body["plan_digest"], test_id=body["test_id"], completed_at=now())
            result["result_digest"] = digest(result)
            session.add(ExecutionRow(execution_id=result["execution_id"], plan_id=plan_id, request_id=body["request_id"], intent_digest=intent, result=result, internal_trace=trace))
            if task.exposed_at is None:
                task.exposed_at = datetime.now(timezone.utc)
            return result

    async def execution(self, principal, execution_id):
        async with self.database.session() as session:
            row = await session.get(ExecutionRow, execution_id)
            if row is None:
                raise BusinessError("FEATURE_NOT_FOUND", 404)
            plan = await session.get(PlanRow, row.plan_id)
            await self.owned_report(session, principal, plan.report_id)
            return row.result

    async def bound_evidence(self, context, arguments):
        from core.feature_impact.models import InvestigationRow
        from core.feature_impact.tools import AGENT_ID
        from core.feature_impact.agent_reports import require_binding_owner
        from sqlalchemy.exc import SQLAlchemyError
        if (set(arguments) not in ({'query'}, {'source_ref'}) or
                any(not isinstance(v, str) or len(v) > 300 for v in arguments.values())):
            raise BusinessError('FEATURE_TOOL_INPUT_INVALID', 422)
        async with self.database.session() as session:
            binding = await session.get(InvestigationRow, context.run_id)
            if binding is None:
                raise BusinessError("FEATURE_SCOPE_DENIED", 403)
            task = await session.get(TaskRow, binding.task_id)
            access = context.memory_access_context
            if (task is None
                    or access is None or access.tenant_id != task.tenant_id or access.user_id != str(task.owner_user_id)
                    or access.agent_id != AGENT_ID or access.session_id != binding.session_id
                    or context.data.entry_agent_id != AGENT_ID or context.data.agent_version != "1.1.0"
                    or context.session_id != binding.session_id or task.exposed_at is not None):
                raise BusinessError("FEATURE_SCOPE_DENIED", 403)
            try:
                await require_binding_owner(session, binding, task)
            except (ValueError, SQLAlchemyError):
                raise BusinessError("FEATURE_SCOPE_DENIED", 403) from None
            return read(task.snapshot, arguments["source_ref"]) if "source_ref" in arguments else dict(items=search(task.snapshot, arguments["query"]))
