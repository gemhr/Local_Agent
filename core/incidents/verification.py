"""同版本 SINGLE/GROUP 验证与人类主动关闭，不调用 Runtime。"""
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid4, uuid5
from sqlalchemy import select
from core.ci_simulation.execution import case_catalog
from core.ci_simulation.service import execute_instance
from core.ci_simulation.models import CIExecutionRow
from core.incidents.models import IncidentActionRow, IncidentVerificationRow
from core.incidents.contracts import CIError
from core.incidents.service import evidence_catalog

VERIFICATION_POLICY = "ci-manual-verification.v1"


def state_differences(before, after):
    paths = [["environment", key] for key in ("software_version", "parser_version", "decoder_table_version",
        "parser_supported_field_revision", "decoder_supported_field_revision", "buffer_behavior")]
    paths += [["environment", "feature_flags", "compressed_transport"], ["test_case", "input_revision"], ["report", "field_revision"]]
    def read(state, path):
        for key in path:
            state = state.get(key, {}) if isinstance(state, dict) else None
        return state if state != {} else None
    return [dict(path=path, before=read(before, path), after=read(after, path))
        for path in paths if read(before, path) != read(after, path)]


async def verification_payload(session, row, origin):
    items = []
    primary = None
    for item in row.items:
        execution = await session.get(CIExecutionRow, item["execution_id"])
        if execution is None or execution.instance_id != row.instance_id or execution.case_id != item["case_id"] or execution.state_revision != row.state_revision:
            raise CIError("CI_VERIFICATION_INCOMPLETE", 409)
        items.append(dict(case_id=execution.case_id, execution_id=execution.execution_id, outcome=execution.outcome,
            state_revision=execution.state_revision, incident_id=row.incident_id,
            summary=execution.summary, evidence=evidence_catalog(execution)))
        if execution.case_id == origin.case_id:
            primary = execution
    return dict(verification_id=row.verification_id, incident_id=row.incident_id, instance_id=row.instance_id,
        mode=row.mode, state_revision=row.state_revision, verification_policy_version=row.verification_policy_version,
        requested_case_ids=row.requested_case_ids, created_by_user_id=row.created_by_user_id,
        completed_at=row.completed_at, outcome="PASS" if all(i["outcome"] == "PASS" for i in items) else "FAIL", items=items,
        comparison=dict(original_execution_id=origin.execution_id, original_outcome=origin.outcome,
            new_execution_id=primary.execution_id if primary else None, new_outcome=primary.outcome if primary else None,
            changes=state_differences(origin.execution_snapshot, primary.execution_snapshot) if primary else []))


async def create_verification(service, principal, incident_id, body):
    async with service.database.transaction() as session:
        incident, origin, instance = await service.resolution_scope(session, principal, incident_id, lock=True)
        old = (await session.execute(select(IncidentVerificationRow).where(IncidentVerificationRow.incident_id == incident_id,
            IncidentVerificationRow.request_id == str(body.request_id)))).scalar_one_or_none()
        if old:
            if old.mode != body.mode or old.requested_case_ids != body.case_ids or old.state_revision != body.expected_state_revision:
                raise CIError("CI_REQUEST_CONFLICT", 409)
            return await verification_payload(session, old, origin)
        if incident.status == "CLOSED":
            raise CIError("CI_INCIDENT_CLOSED", 409)
        if instance.state_revision != body.expected_state_revision:
            raise CIError("CI_STATE_STALE", 409)
        required = [origin.case_id] if body.mode == "SINGLE" else [c["case_id"] for c in case_catalog(instance.current_state)]
        if body.case_ids != required or required[0] != origin.case_id:
            raise CIError("CI_VERIFICATION_CASES_INVALID", 422)
        verification_id = str(uuid4())
        items, completed = [], None
        for case_id in required:
            # 仅此人工验证路径不创建 FAIL Incident；执行和证据仍逐条追加。
            execution, _ = await execute_instance(session, principal, instance.instance_id,
                str(uuid5(NAMESPACE_URL, verification_id + ":" + case_id)),
                case_id=case_id, expected_state_revision=instance.state_revision)
            items.append(dict(case_id=case_id, execution_id=execution.execution_id))
            completed = execution.completed_at
        row = IncidentVerificationRow(verification_id=verification_id, incident_id=incident_id,
            instance_id=instance.instance_id, request_id=str(body.request_id), mode=body.mode,
            verification_policy_version=VERIFICATION_POLICY, state_revision=instance.state_revision,
            requested_case_ids=required, items=items, created_by_user_id=principal.user_id, completed_at=completed)
        session.add(row)
        await session.flush()
        return await verification_payload(session, row, origin)


async def read_verification(service, principal, incident_id, verification_id):
    async with service.database.session() as session:
        incident, origin, instance = await service.resolution_scope(session, principal, incident_id)
        row = await session.get(IncidentVerificationRow, verification_id)
        if row is None or row.incident_id != incident_id:
            raise CIError("CI_NOT_FOUND", 404)
        return await verification_payload(session, row, origin)


async def closure_inputs(session, incident, instance):
    action = (await session.execute(select(IncidentActionRow).where(IncidentActionRow.incident_id == incident.incident_id,
        IncidentActionRow.status == "APPLIED").order_by(IncidentActionRow.applied_state_revision.desc()).limit(1))).scalar_one_or_none()
    batches = []
    for mode in ("SINGLE", "GROUP"):
        row = (await session.execute(select(IncidentVerificationRow).where(IncidentVerificationRow.incident_id == incident.incident_id,
            IncidentVerificationRow.mode == mode, IncidentVerificationRow.state_revision == instance.state_revision)
            .order_by(IncidentVerificationRow.completed_at.desc(), IncidentVerificationRow.verification_id).limit(1))).scalar_one_or_none()
        batches.append(row)
    return action, *batches


async def validate_closure(session, incident, origin, instance, action, single, group):
    if action is None or single is None or group is None or action.applied_state_revision != instance.state_revision:
        raise CIError("CI_VERIFICATION_INCOMPLETE", 409)
    required = [c["case_id"] for c in case_catalog(instance.current_state)]
    for batch, mode, cases in ((single, "SINGLE", [origin.case_id]), (group, "GROUP", required)):
        if (batch.incident_id != incident.incident_id or batch.instance_id != instance.instance_id
                or batch.state_revision != instance.state_revision or batch.mode != mode
                or batch.verification_policy_version != VERIFICATION_POLICY or batch.requested_case_ids != cases
                or len(batch.items) != len(cases) or batch.completed_at < action.confirmed_at):
            raise CIError("CI_VERIFICATION_INCOMPLETE", 409)
        for item, case_id in zip(batch.items, cases):
            execution = await session.get(CIExecutionRow, item["execution_id"])
            if (item["case_id"] != case_id or execution is None or execution.case_id != case_id
                    or execution.instance_id != instance.instance_id or execution.state_revision != instance.state_revision
                    or execution.started_at < action.confirmed_at):
                raise CIError("CI_VERIFICATION_INCOMPLETE", 409)
            if execution.outcome != "PASS":
                raise CIError("CI_VERIFICATION_FAILED", 409)


async def closure_eligibility(session, incident, origin, instance):
    action, single, group = await closure_inputs(session, incident, instance)
    error = None
    if incident.status == "CLOSED":
        error = "CI_INCIDENT_CLOSED"
    else:
        try:
            await validate_closure(session, incident, origin, instance, action, single, group)
        except CIError as exc:
            error = exc.code
    return dict(eligible=error is None, reasons=[error] if error else [],
        action_id=action.action_id if action else None,
        single_verification_id=single.verification_id if single else None,
        group_verification_id=group.verification_id if group else None)


def closed_payload(incident):
    return dict(incident_id=incident.incident_id, status=incident.status, closed_at=incident.closed_at,
        closed_by_user_id=incident.closed_by_user_id, closure=incident.closure_payload)


async def close_incident(service, principal, incident_id, body):
    async with service.database.transaction() as session:
        incident, origin, instance = await service.resolution_scope(session, principal, incident_id, lock=True)
        requested = body.model_dump(mode="json")
        if incident.status == "CLOSED":
            if incident.closure_payload["confirmation"] == requested:
                return closed_payload(incident)
            raise CIError("CI_INCIDENT_CLOSED", 409)
        if body.regression_confirmed is not True or instance.state_revision != body.expected_state_revision:
            raise CIError("CI_VERIFICATION_INCOMPLETE", 409)
        action, single, group = await closure_inputs(session, incident, instance)
        if (action is None or single is None or group is None or body.action_id != action.action_id
                or body.single_verification_id != single.verification_id or body.group_verification_id != group.verification_id):
            raise CIError("CI_VERIFICATION_INCOMPLETE", 409)
        await validate_closure(session, incident, origin, instance, action, single, group)
        incident.status, incident.closed_at, incident.closed_by_user_id = "CLOSED", datetime.now(timezone.utc), principal.user_id
        incident.closure_payload = dict(source_kind="HUMAN_MANUAL", state_revision=instance.state_revision,
            action_id=action.action_id, single_verification_id=single.verification_id, group_verification_id=group.verification_id,
            confirmation=requested)
        await session.flush()
        return closed_payload(incident)
