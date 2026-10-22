"""HUMAN_MANUAL 预览、确认与审计的短业务事务。"""
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import select
from core.ci_simulation.actions import ACTION_OPTIONS, preview, apply_preview
from core.ci_simulation.execution import case_catalog
from core.incidents.models import IncidentActionRow
from core.incidents.contracts import CIError
from core.runtime.snapshot_serialization import sha256_digest


def action_payload(row, current_revision):
    return dict(action_id=row.action_id, action_kind=row.action_kind, source_kind=row.source_kind, status=row.status,
        created_by_user_id=row.created_by_user_id, created_at=row.created_at, base_state_revision=row.base_state_revision,
        parameters=row.parameters, preview_payload=row.preview_payload, preview_digest=row.preview_digest,
        stale=row.status == "PREVIEWED" and current_revision != row.base_state_revision,
        confirmed_by_user_id=row.confirmed_by_user_id, confirmed_at=row.confirmed_at,
        applied_state_revision=row.applied_state_revision, result_payload=row.result_payload)


async def current_state(service, principal, incident_id):
    async with service.database.session() as session:
        incident, origin, instance = await service.resolution_scope(session, principal, incident_id)
        state = instance.current_state
        options = []
        if incident.status != "CLOSED":
            for option in ACTION_OPTIONS:
                try:
                    preview(state, option["action_kind"], state["test_case"]["case_id"], option["parameters"])
                except CIError:
                    continue
                options.append(option)
        return dict(instance_id=instance.instance_id, observed_at=datetime.now(timezone.utc), state_revision=instance.state_revision,
            environment=state["environment"], cases=case_catalog(state), report_field_revision=state["report"]["field_revision"],
            required_case_ids=[c["case_id"] for c in case_catalog(state)], allowed_actions=options)


async def create_action(service, principal, incident_id, body):
    async with service.database.transaction() as session:
        incident, origin, instance = await service.resolution_scope(session, principal, incident_id, lock=True)
        old = (await session.execute(select(IncidentActionRow).where(IncidentActionRow.incident_id == incident_id,
            IncidentActionRow.request_id == str(body.request_id)))).scalar_one_or_none()
        if old:
            if old.action_kind != body.action_kind or old.parameters != body.parameters or old.preview_payload["target_case_id"] != body.target_case_id:
                raise CIError("CI_REQUEST_CONFLICT", 409)
            return action_payload(old, instance.state_revision)
        if incident.status == "CLOSED":
            raise CIError("CI_INCIDENT_CLOSED", 409)
        frozen = dict(instance_id=instance.instance_id, base_state_revision=instance.state_revision,
            **preview(instance.current_state, body.action_kind, body.target_case_id, body.parameters))
        row = IncidentActionRow(action_id=str(uuid4()), incident_id=incident_id, request_id=str(body.request_id),
            action_kind=body.action_kind, source_kind="HUMAN_MANUAL", created_by_user_id=principal.user_id,
            created_at=datetime.now(timezone.utc), base_state_revision=instance.state_revision, parameters=body.parameters,
            preview_payload=frozen, preview_digest=sha256_digest(frozen), status="PREVIEWED")
        session.add(row)
        await session.flush()
        return action_payload(row, instance.state_revision)


async def read_action(service, principal, incident_id, action_id):
    async with service.database.session() as session:
        incident, origin, instance = await service.resolution_scope(session, principal, incident_id)
        row = await session.get(IncidentActionRow, action_id)
        if row is None or row.incident_id != incident_id:
            raise CIError("CI_NOT_FOUND", 404)
        return action_payload(row, instance.state_revision)


async def confirm_action(service, principal, incident_id, action_id, body):
    async with service.database.transaction() as session:
        incident, origin, instance = await service.resolution_scope(session, principal, incident_id, lock=True)
        row = await session.get(IncidentActionRow, action_id, with_for_update=True)
        if row is None or row.incident_id != incident_id:
            raise CIError("CI_NOT_FOUND", 404)
        if body.confirmed is not True or body.preview_digest != row.preview_digest or sha256_digest(row.preview_payload) != row.preview_digest:
            raise CIError("CI_ACTION_CONFIRMATION_INVALID", 409)
        if row.status == "APPLIED":
            return action_payload(row, instance.state_revision)
        if incident.status == "CLOSED":
            raise CIError("CI_INCIDENT_CLOSED", 409)
        if row.base_state_revision != instance.state_revision:
            raise CIError("CI_ACTION_STALE", 409)
        frozen = {k: v for k, v in row.preview_payload.items() if k not in {"instance_id", "base_state_revision"}}
        instance.current_state = apply_preview(instance.current_state, row.action_kind, frozen["target_case_id"], row.parameters, frozen)
        instance.state_revision += 1
        row.status, row.confirmed_by_user_id, row.confirmed_at = "APPLIED", principal.user_id, datetime.now(timezone.utc)
        row.applied_state_revision = instance.state_revision
        row.result_payload = dict(instance_id=instance.instance_id, state_revision=instance.state_revision,
            source_kind="HUMAN_MANUAL", changes=frozen["changes"])
        incident.status = "PENDING_VERIFICATION"
        await session.flush()
        return action_payload(row, instance.state_revision)
