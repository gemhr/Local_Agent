"""仅两类站内通知；状态和待办始终来自原始执行与 Incident。"""
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert
from core.ci_simulation.models import NightlyVersionExecutionRow as Version, NightlyCIBatchRow as Batch, CIExecutionRow
from core.ci_simulation.service import CIError
from core.incidents.models import UserNotificationRow as Notification, IncidentRow
from core.incidents.service import require_human


async def create(session, batch, owner, kind):
    await session.execute(insert(Notification).values(notification_id=str(uuid4()), tenant_id=batch.tenant_id,
        recipient_user_id=owner, batch_id=batch.batch_id, kind=kind, created_at=datetime.now(timezone.utc))
        .on_conflict_do_nothing(index_elements=["batch_id", "recipient_user_id", "kind"]))


async def inbox(service, principal, unread_only=False, limit=50):
    require_human(principal)
    async with service.database.session() as session:
        own = (Notification.tenant_id == principal.tenant_id) & (Notification.recipient_user_id == principal.user_id)
        unread = await session.scalar(select(func.count()).select_from(Notification).where(own, Notification.read_at.is_(None)))
        query = select(Notification).where(own)
        if unread_only:
            query = query.where(Notification.read_at.is_(None))
        rows = (await session.execute(query.order_by(Notification.created_at.desc(), Notification.notification_id).limit(limit))).scalars().all()
        items = []
        for row in rows:
            batch = await session.get(Batch, row.batch_id)
            facts = (await session.execute(select(CIExecutionRow, IncidentRow).join(Version,
                CIExecutionRow.nightly_version_execution_id == Version.version_execution_id).outerjoin(IncidentRow,
                IncidentRow.execution_id == CIExecutionRow.execution_id).where(Version.batch_id == batch.batch_id,
                Version.owner_user_id == principal.user_id))).all()
            items.append(dict(notification_id=row.notification_id, batch_id=batch.batch_id, kind=row.kind,
                created_at=row.created_at, read_at=row.read_at, batch_status=batch.status, total=len(facts),
                original_failures=sum(e.outcome == "FAIL" for e, _ in facts),
                current_todos=[dict(incident_id=i.incident_id, status=i.status) for _, i in facts if i and i.status != "CLOSED"]))
        todos = (await session.execute(select(IncidentRow).where(IncidentRow.tenant_id == principal.tenant_id,
            IncidentRow.owner_user_id == principal.user_id, IncidentRow.status != "CLOSED").order_by(IncidentRow.created_at.desc()))).scalars().all()
        return dict(items=items, unread_count=unread, current_todos=[dict(incident_id=i.incident_id, title=i.title, status=i.status) for i in todos])


async def mark_read(service, principal, notification_id):
    require_human(principal)
    async with service.database.transaction() as session:
        row = (await session.execute(select(Notification).where(Notification.notification_id == notification_id,
            Notification.tenant_id == principal.tenant_id, Notification.recipient_user_id == principal.user_id).with_for_update())).scalar_one_or_none()
        if row is None:
            raise CIError("CI_NOT_FOUND", 404)
        if row.read_at is None:
            row.read_at = datetime.now(timezone.utc)
        return dict(notification_id=row.notification_id, read_at=row.read_at)
