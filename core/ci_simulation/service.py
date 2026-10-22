"""调用方事务内的场景创建与执行操作。"""
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from core.ci_simulation.execution import execute, initial_state, case_catalog
from core.ci_simulation.models import ScenarioInstanceRow, CIExecutionRow


class CIError(ValueError):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


async def create_instance(session, principal, key, request_id):
    values = dict(instance_id=str(uuid4()), tenant_id=principal.tenant_id, owner_user_id=principal.user_id,
        request_id=request_id, scenario_key=key, template_version="2", state_revision=1,
        current_state=initial_state(key), created_at=datetime.now(timezone.utc))
    inserted = (await session.execute(insert(ScenarioInstanceRow).values(**values).on_conflict_do_nothing().returning(ScenarioInstanceRow.instance_id))).scalar_one_or_none()
    row = (await session.execute(select(ScenarioInstanceRow).where(ScenarioInstanceRow.tenant_id == principal.tenant_id,
        ScenarioInstanceRow.owner_user_id == principal.user_id, ScenarioInstanceRow.request_id == request_id))).scalar_one()
    if row.scenario_key != key:
        raise CIError("CI_REQUEST_CONFLICT", 409)
    return row, inserted is not None


async def execute_instance(session, principal, instance_id, request_id, *, case_id=None, expected_state_revision=None):
    row = (await session.execute(select(ScenarioInstanceRow).where(ScenarioInstanceRow.instance_id == instance_id,
        ScenarioInstanceRow.owner_user_id == principal.user_id, ScenarioInstanceRow.tenant_id == principal.tenant_id).with_for_update())).scalar_one_or_none()
    if row is None:
        raise CIError("CI_NOT_FOUND", 404)
    old = (await session.execute(select(CIExecutionRow).where(CIExecutionRow.instance_id == instance_id,
        CIExecutionRow.request_id == request_id))).scalar_one_or_none()
    if old:
        if ((case_id is not None and old.case_id != case_id)
                or (expected_state_revision is not None and old.state_revision != expected_state_revision)):
            raise CIError("CI_REQUEST_CONFLICT", 409)
        return old, False
    if expected_state_revision is not None and row.state_revision != expected_state_revision:
        raise CIError("CI_STATE_STALE", 409)
    if case_id is not None and case_id not in {c["case_id"] for c in case_catalog(row.current_state)}:
        raise CIError("CI_CASE_UNKNOWN", 422)
    result = execute(row.current_state, str(uuid4()), case_id)
    state = result["execution_snapshot"]
    execution = CIExecutionRow(execution_id=result["summary"]["execution_id"], instance_id=instance_id,
        request_id=request_id, environment_id=state["environment"]["environment_id"],
        case_id=state["test_case"]["case_id"], feature_id=state["test_case"]["feature_id"], state_revision=row.state_revision, **result)
    session.add(execution)
    await session.flush()
    return execution, True


async def persist_nightly_case(session, batch, version, slot):
    """仅供已授权夜间事务使用；归属来自冻结计划，实际 actor 由版次记录。"""
    instance = ScenarioInstanceRow(instance_id=str(uuid4()), tenant_id=batch.tenant_id,
        owner_user_id=version.owner_user_id, request_id="nightly:" + version.version_execution_id + ":" + slot["case_id"],
        scenario_key="nightly-case", template_version="1", state_revision=1,
        current_state=slot["initial_state"], created_at=datetime.now(timezone.utc))
    session.add(instance)
    await session.flush()
    result = execute(instance.current_state, str(uuid4()))
    execution = CIExecutionRow(execution_id=result["summary"]["execution_id"], instance_id=instance.instance_id,
        request_id="nightly-original", environment_id=version.environment_id, case_id=slot["case_id"],
        feature_id=result["execution_snapshot"]["test_case"]["feature_id"], state_revision=1,
        nightly_version_execution_id=version.version_execution_id, business_completed_at=batch.simulated_now, **result)
    session.add(execution)
    await session.flush()
    return execution
