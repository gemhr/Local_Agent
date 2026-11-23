"""从既有接纳、执行和审批账本读取工作台投影。"""

from sqlalchemy import and_, case, cast, func, literal, select, union_all
from sqlalchemy.dialects.postgresql import JSONB

from core.auth import AUTHORIZATION_OBJECT_NOT_OWNED, AuthError, ObjectAuthorizationService
from core.persistence.models import (
    ClientDeliveryEventRow as Feed, ExternalRunRow as External, RunControlRow as Control,
    RuntimeEventJournalRow as Journal, RuntimeRunExecutionRow as Execution,
    SpaceRow, WorkflowApprovalRow as Approval, WorkflowRunRow as Workflow,
    WorkflowStepRunRow as WorkflowStep,
    WorkflowVersionRow as Version, WorkspaceAgentSubmissionRow as Agent,
)
from core.persistence.repositories.spaces import SpaceRepository
from core.persistence.repositories.workflows import TERMINAL
from core.runtime.event_journal_store import journal_record_from_row
from core.runtime.events import RuntimeEventType

_TERMINAL_EVENT = RuntimeEventType.RUN_COMPLETED.value


def visible_spaces(principal):
    query = SpaceRepository.visible_to(principal.tenant_id, principal.user_id)
    return query.with_only_columns(
        SpaceRow.id, SpaceRow.tenant_id, SpaceRow.name, query.selected_columns.space_role.label("role"),
    ).cte("visible_spaces")


def _started(run_id):
    return select(func.min(Feed.created_at)).where(
        Feed.run_id == run_id, Feed.event_type == "run.started",
    ).scalar_subquery()


def _agent_tasks(spaces):
    started = _started(Agent.run_id)
    terminal = cast(Journal.safe_payload, JSONB)
    source = func.coalesce(External.terminal_status, External.observation_state, terminal["status"].astext, Control.state)
    status = case(
        (External.terminal_status.is_not(None), External.terminal_status),
        (terminal["status"].astext.is_not(None), terminal["status"].astext),
        (func.coalesce(External.cancel_reason, Control.cancel_reason).is_not(None), "CANCELLING"),
        (External.observation_state == "UNKNOWN", "UNKNOWN"),
        (External.observation_state == "CREATED", "PENDING"),
        (Execution.manual_required.is_(True), "PAUSED"),
        (Control.state == "CLOSED", "UNKNOWN"),
        else_="RUNNING",
    )
    config = Agent.configuration
    return select(
        literal("AGENT").label("task_kind"), Agent.run_id.label("task_id"),
        spaces.c.id.label("space_id"), spaces.c.name.label("space_name"), spaces.c.role,
        Agent.owner_user_id, func.coalesce(config["agent_config"]["name"].astext, config["agent_id"].astext).label("title"),
        config["agent_id"].astext.label("definition_id"), config["agent_version"].astext.label("version"),
        config["runtime_kind"].astext.label("runtime_kind"),
        source.label("source_status"), status.label("status"),
        func.coalesce(External.created_at, Control.created_at).label("created_at"),
        func.greatest(External.updated_at, Control.updated_at, Execution.updated_at, Journal.emitted_at).label("updated_at"),
        func.coalesce(started, External.created_at).label("started_at"),
        func.coalesce(case((External.terminal_status.is_not(None), External.updated_at)), Journal.emitted_at).label("ended_at"),
        func.coalesce(External.error_code, terminal["safe_error_code"].astext, External.stop_reason,
                      terminal["stop_reason"].astext,
                      case((Execution.manual_required.is_(True), "NATIVE_MANUAL_RECOVERY_REQUIRED"))).label("reason"),
    ).select_from(Agent).join(spaces, and_(
        spaces.c.id == Agent.space_id, spaces.c.tenant_id == Agent.tenant_id,
    )).outerjoin(External, and_(
        External.run_id == Agent.run_id, External.tenant_id == Agent.tenant_id,
        External.user_id == cast(Agent.owner_user_id, External.user_id.type),
    )).outerjoin(Control, Control.run_id == Agent.run_id).outerjoin(Execution, Execution.run_id == Agent.run_id).outerjoin(Journal, and_(
        Journal.run_id == Agent.run_id, Journal.sequence == Control.terminal_sequence, Journal.event_type == _TERMINAL_EVENT,
    )).where(
        ((config["runtime_kind"].astext == "OPENCODE") & External.run_id.is_not(None))
        | ((config["runtime_kind"].astext == "NATIVE") & (Control.fencing_token > 0) & started.is_not(None)),
    )


def _awaiting_owner():
    return and_(Workflow.status.in_(("WAITING_APPROVAL", "PAUSED")), Approval.status == "APPROVED",
                Approval.consumed_at.is_(None))


def _workflow_tasks(spaces):
    started = select(func.min(Feed.created_at)).join(
        WorkflowStep, WorkflowStep.platform_run_id == Feed.run_id,
    ).where(WorkflowStep.workflow_run_id == Workflow.workflow_run_id, Feed.event_type == "run.started").scalar_subquery()
    return select(
        literal("WORKFLOW").label("task_kind"), Workflow.workflow_run_id.label("task_id"),
        spaces.c.id.label("space_id"), spaces.c.name.label("space_name"), spaces.c.role,
        Workflow.owner_user_id, func.coalesce(Version.definition_snapshot["name"].astext, Workflow.workflow_id).label("title"),
        Workflow.workflow_id.label("definition_id"), Workflow.workflow_version.label("version"),
        literal("OPENCODE → NATIVE").label("runtime_kind"),
        Workflow.status.label("source_status"),
        case((_awaiting_owner(), "AWAITING_OWNER"), else_=Workflow.status).label("status"),
        Workflow.created_at, Workflow.updated_at, started.label("started_at"),
        case((Workflow.status.in_(TERMINAL), Workflow.updated_at)).label("ended_at"), Workflow.reason,
    ).select_from(Workflow).join(spaces, and_(
        spaces.c.id == Workflow.space_id, spaces.c.tenant_id == Workflow.tenant_id,
    )).join(Version, and_(
        Version.workflow_id == Workflow.workflow_id, Version.workflow_version == Workflow.workflow_version,
    )).outerjoin(Approval, Approval.workflow_run_id == Workflow.workflow_run_id)


def _summary(row, principal):
    result = dict(row)
    owner = result["owner_user_id"] == principal.user_id
    role = result.pop("role")
    kind = result["task_kind"]
    result["can_cancel"] = owner and result["status"] not in TERMINAL | {"CANCELLING"}
    result["can_resume"] = owner and kind == "WORKFLOW" and result["status"] in {"PAUSED", "PENDING", "AWAITING_OWNER"}
    result["can_approve"] = kind == "WORKFLOW" and role in {"OWNER", "ADMIN"} and result["status"] == "WAITING_APPROVAL"
    return result


async def task_page(database, principal, *, space_id, kind, status, mine, offset, limit, task_id=None):
    async with database.session() as session:
        await ObjectAuthorizationService.require_current_human_in_session(session, principal)
        spaces = visible_spaces(principal)
        if not kind:
            source = union_all(_agent_tasks(spaces), _workflow_tasks(spaces)).subquery()
        else:
            source = (_agent_tasks(spaces) if kind == "AGENT" else _workflow_tasks(spaces)).subquery()
        query = select(source)
        if task_id is not None:
            query = query.where(source.c.task_id == task_id)
        if space_id is not None:
            query = query.where(source.c.space_id == space_id)
        if status:
            query = query.where(source.c.status == status)
        if mine:
            query = query.where(source.c.owner_user_id == principal.user_id)
        rows = (await session.execute(query.order_by(
            source.c.created_at.desc(), source.c.task_id.desc(), source.c.task_kind,
        ).offset(offset).limit(limit + 1))).mappings().all()
        return {"items": [_summary(row, principal) for row in rows[:limit]],
                "next_offset": offset + limit if len(rows) > limit else None}


async def task_detail(database, application, workflow, authorization, principal, kind, task_id):
    page = await task_page(database, principal, space_id=None, kind=kind, status=None,
                           mine=False, offset=0, limit=1, task_id=task_id)
    if not page["items"]:
        raise AuthError(AUTHORIZATION_OBJECT_NOT_OWNED, 404)
    task = page["items"][0]
    if kind == "WORKFLOW":
        view = await workflow.get(principal, task_id)
        view.pop("frozen_ci_input", None)
        task["workflow"] = view
        return task
    await authorization.authorize(principal, "RUN", task_id, "READ", share_workspace_read=True)
    async with database.session() as session:
        submission = await session.get(Agent, task_id)
        config = submission.configuration
        task.update(
            configuration_revision=(config.get("agent_config") or {}).get("revision"),
            runtime_instance_id=config.get("runtime_instance_id"), input_digest=config.get("input_digest"),
        )
    task["result"] = await run_result(database, application, task_id, kind=task["runtime_kind"])
    return task


async def approval_page(database, principal, *, space_id, status, offset, limit):
    async with database.session() as session:
        await ObjectAuthorizationService.require_current_human_in_session(session, principal)
        spaces = visible_spaces(principal)
        query = select(
            Approval.approval_id, Approval.workflow_run_id, Approval.step_id, Approval.attempt,
            Approval.status, Approval.binding_digest, Approval.decided_at, Approval.decided_by,
            Approval.consumed_at, Approval.created_at, Workflow.workflow_version, Workflow.owner_user_id,
            Workflow.status.label("workflow_status"), spaces.c.id.label("space_id"), spaces.c.name.label("space_name"),
            spaces.c.role.in_(("OWNER", "ADMIN")).label("can_approve"),
            _awaiting_owner().label("awaiting_owner"),
            func.coalesce(Version.definition_snapshot["name"].astext, Workflow.workflow_id).label("title"),
        ).select_from(Approval).join(Workflow, and_(
            Workflow.workflow_run_id == Approval.workflow_run_id, Workflow.tenant_id == Approval.tenant_id,
        )).join(spaces, and_(spaces.c.id == Workflow.space_id, spaces.c.tenant_id == Workflow.tenant_id)).join(
            Version, and_(Version.workflow_id == Workflow.workflow_id, Version.workflow_version == Workflow.workflow_version),
        )
        if space_id:
            query = query.where(spaces.c.id == space_id)
        if status == "PENDING":
            query = query.where(Approval.status == status, Workflow.status == "WAITING_APPROVAL",
                                spaces.c.role.in_(("OWNER", "ADMIN")))
        elif status == "AWAITING_OWNER":
            query = query.where(_awaiting_owner())
        elif status:
            query = query.where(Approval.status == status)
        rows = (await session.execute(query.order_by(
            Approval.created_at.desc(), Approval.approval_id.desc(),
        ).offset(offset).limit(limit + 1))).mappings().all()
        return {"items": [{**row, "can_approve": row["can_approve"] and row["status"] == "PENDING"
                           and row["workflow_status"] == "WAITING_APPROVAL"} for row in rows[:limit]],
                "next_offset": offset + limit if len(rows) > limit else None}


async def run_result(database, application, run_id, *, kind=None):
    """调用方先授权。只返回有已核实终态且经原记录确认的完整输出。"""
    async with database.session() as session:
        external = await session.get(External, run_id) if kind != "NATIVE" else None
        if external is None:
            control = await session.get(Control, run_id)
            if control is None or control.state != "CLOSED" or control.terminal_sequence is None:
                return {"availability": "UNAVAILABLE", "output": None}
            terminal = await session.get(Journal, (run_id, control.terminal_sequence))
            delivery = await session.get(Feed, (run_id, control.terminal_sequence))
            if (terminal is None or terminal.event_type != _TERMINAL_EVENT or delivery is None
                    or delivery.event_id != terminal.event_id or delivery.event_type != "run.completed"
                    or delivery.payload.get("business_output_valid") is False
                    or journal_record_from_row(terminal).safe_payload.get("status") != "SUCCEEDED"):
                return {"availability": "UNAVAILABLE", "output": None}
            output = delivery.payload.get("output")
            if isinstance(output, str) and len(output) <= 65_536:
                return {"availability": "CONFIRMED", "output": output}
            if output is not None:
                return {"availability": "UNAVAILABLE", "output": None}
            # 只拼接与 Journal 一一匹配的完整正文，限制单次投影体量。
            text = Feed.payload["text"].astext
            condition = (Journal.run_id == run_id, Journal.sequence < control.terminal_sequence,
                         Journal.event_type == RuntimeEventType.OUTPUT_DELTA.value)
            join = and_(Feed.run_id == Journal.run_id, Feed.cursor == Journal.sequence,
                        Feed.event_id == Journal.event_id, Feed.event_type == "output.delta")
            found, delivered, length = (await session.execute(select(
                func.count(), func.count(text), func.sum(func.length(text)),
            ).select_from(Journal).outerjoin(Feed, join).where(*condition))).one()
            if not 0 < found <= 4096 or found != delivered or not 0 < (length or 0) <= 65_536:
                return {"availability": "UNAVAILABLE", "output": None}
            parts = await session.scalars(select(text).select_from(Journal).join(Feed, join).where(
                *condition,
            ).order_by(Journal.sequence))
            chunks = parts.all()
            if len(chunks) != found or any(chunk is None for chunk in chunks):
                return {"availability": "UNAVAILABLE", "output": None}
            return {"availability": "CONFIRMED", "output": "".join(chunks)}
    if external.terminal_status != "SUCCEEDED" or external.error_code:
        return {"availability": "UNAVAILABLE", "output": None}
    result = await application.read_external_result(run_id, mark_unknown=False)
    if result and result["status"] == "SUCCEEDED" and isinstance(result["output"], str) and len(result["output"]) <= 65_536:
        return {"availability": "CONFIRMED", "output": result["output"]}
    return {"availability": "UNAVAILABLE", "output": None, "read_error_code": result.get("read_error_code") if result else None}
