"""Stage8-WP3：外部执行、结果回调与失败分流业务闭环。"""

from __future__ import annotations

from dataclasses import asdict
import json
import uuid
from datetime import UTC, datetime
from typing import Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import null

from core.runtime import (
    ApprovalRequest,
    BudgetLedger,
    RunBudget,
    ToolExecutionError,
    ToolExecutionStatus,
    ToolSideEffectState,
    compute_invocation_binding_digest,
    create_run_context,
)
from core.runtime.tool_contract import safe_key_digest
from core.runtime.tool_governance import (
    ToolGovernanceContext,
    ToolGovernanceErrorCode,
    ToolGovernanceOutcome,
    governance_denial_message,
)
from core.stage8 import repositories as repo
from core.stage8.domain import (
    ExecutionPlan,
    ExecutionResult,
    ExternalExecutionJob,
    ExternalExecutionStatus,
    FailureEvidencePackage,
    MissionStatus,
)
from core.stage8.platforms import TicketDraft
from core.stage8.service import (
    MissionService,
    Stage8ConflictError,
    Stage8NotFoundError,
    Stage8ValidationError,
)
from core.stage8.specialists import SpecialistAgentApplicationService


class FailureClassification(str):
    PRODUCT = "PRODUCT"
    TEST_DATA = "TEST_DATA"
    ENVIRONMENT = "ENVIRONMENT"
    TOOL_CHAIN = "TOOL_CHAIN"
    INFRASTRUCTURE = "INFRASTRUCTURE"
    UNKNOWN = "UNKNOWN"


class FailureTriageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mission_id: str
    evidence: dict[str, dict]
    available_evidence: list[dict] = Field(default_factory=list)


class FailureTriageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    classification: str = Field(
        pattern="^(PRODUCT|TEST_DATA|ENVIRONMENT|TOOL_CHAIN|INFRASTRUCTURE|UNKNOWN)$"
    )
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(default_factory=list)
    root_cause_hypothesis: str
    recommended_action: str
    severity: str = Field(default="MEDIUM", pattern="^(LOW|MEDIUM|HIGH|CRITICAL)$")
    candidate_repair: dict[str, str] | None = None
    ticket_draft: TicketDraft | None = None


class Stage8ToolInvocationError(Stage8ValidationError):
    """Tool Runtime 的安全失败投影；保留副作用不确定性供业务解释。"""

    def __init__(self, safe_error_code: str, *, outcome_unknown: bool) -> None:
        super().__init__(safe_error_code)
        self.safe_error_code = safe_error_code
        self.outcome_unknown = outcome_unknown


def _job(row) -> ExternalExecutionJob:
    return ExternalExecutionJob(
        row.job_id,
        row.mission_id,
        row.execution_id,
        ExternalExecutionStatus(row.status),
        row.version,
        row.case_id,
        row.environment_id,
        row.executor_id,
        row.plan_id,
        row.attempt_no,
        row.auto_repair_count,
        row.created_at,
        row.updated_at,
        row.started_at,
        row.completed_at,
        row.result_payload,
        row.triage_payload,
    )


class FailureTriageService:
    def __init__(self, specialist: SpecialistAgentApplicationService):
        self.specialist = specialist

    async def run(self, request: FailureTriageRequest) -> FailureTriageResult:
        return await self.specialist.failure_triage(request)


class GovernedToolInvoker:
    """Stage8 到既有 Governance/Approval/ToolExecution Authority 的薄 facade。"""

    def __init__(
        self,
        registry,
        governance,
        tool_execution,
        *,
        resource_authorization,
        durable_run_control,
        durable_approval,
        owner_id: str,
    ):
        self.registry = registry
        self.governance = governance
        self.tool_execution = tool_execution
        self.resource_authorization = resource_authorization
        self.durable_run_control = durable_run_control
        self.durable_approval = durable_approval
        self.owner_id = owner_id

    async def __call__(
        self, tool_name: str, payload: dict, *, principal_agent_id: str
    ) -> dict:
        registration = self.registry.require(tool_name)
        run_id = uuid.uuid4().hex
        step_id = f"stage8-{tool_name}"
        governance_context = ToolGovernanceContext(
            principal_agent_id, run_id, step_id
        )
        authorization = self.governance.authorize_tool(
            governance_context, registration
        )
        if authorization.outcome is not ToolGovernanceOutcome.ALLOW:
            code = (
                authorization.safe_error_code
                or ToolGovernanceErrorCode.PERMISSION_DENIED.value
            )
            raise Stage8ValidationError(governance_denial_message(code))

        invocation = registration.adapter.build_invocation(
            json.dumps(payload, ensure_ascii=False)
        )
        spec = registration.adapter.spec_for(invocation)
        decision = self.governance.evaluate_invocation(
            governance_context, registration, invocation, spec
        )
        if decision.outcome is ToolGovernanceOutcome.APPROVAL_REQUIRED:
            lease = await self.durable_run_control.claim(run_id, self.owner_id)
            try:
                risk_facts = tuple(
                    fact.value if hasattr(fact, "value") else str(fact)
                    for fact in decision.risk_facts
                )
                invocation_identity_digest = safe_key_digest(invocation.invocation_id)
                assert invocation_identity_digest is not None
                binding = compute_invocation_binding_digest(
                    invocation_identity_digest=invocation_identity_digest,
                    tool_name=invocation.tool_name,
                    arguments_digest=invocation.arguments_digest,
                    idempotency_key_digest=safe_key_digest(invocation.idempotency_key),
                    resource_key_digest=safe_key_digest(invocation.resource_key),
                    risk_level=(
                        decision.risk_level.value if decision.risk_level else None
                    ),
                    risk_facts=risk_facts,
                )
                request = await self.durable_approval.create(ApprovalRequest(
                    approval_id=uuid.uuid4().hex,
                    run_id=run_id,
                    step_id=step_id,
                    invocation_id=invocation.invocation_id,
                    tool_name=tool_name,
                    invocation_identity_digest=invocation_identity_digest,
                    arguments_digest=invocation.arguments_digest,
                    idempotency_key_digest=safe_key_digest(invocation.idempotency_key),
                    resource_key_digest=safe_key_digest(invocation.resource_key),
                    risk_level=(
                        decision.risk_level.value if decision.risk_level else None
                    ),
                    risk_facts=risk_facts,
                    invocation_binding_digest=binding,
                    requested_at=datetime.now(UTC),
                ))
                return {
                    "status": "APPROVAL_REQUIRED",
                    "tool_name": tool_name,
                    "run_id": request.run_id,
                    "approval_id": request.approval_id,
                    "invocation_binding_digest": request.invocation_binding_digest,
                }
            finally:
                await self.durable_run_control.release(lease)
        if decision.outcome is not ToolGovernanceOutcome.ALLOW:
            code = (
                decision.safe_error_code
                or ToolGovernanceErrorCode.PERMISSION_DENIED.value
            )
            raise Stage8ValidationError(governance_denial_message(code))

        resource_request = self.resource_authorization.extract(invocation)
        if resource_request is not None:
            self.resource_authorization.require_authorized(resource_request)

        lease = await self.durable_run_control.claim(run_id, self.owner_id)
        run_context, _ = create_run_context(
            entry_agent_id=principal_agent_id, run_id=run_id, timeout_seconds=30
        )
        run_context.attach_durable_lease(lease)
        run_context.attach_ownership_validator(
            lambda: self.durable_run_control.assert_current(lease)
        )
        run_context.attach_budget_ledger(BudgetLedger(RunBudget(max_tool_calls=1)))
        try:
            result = await self.tool_execution.execute(
                invocation=invocation,
                adapter=registration.adapter,
                run_context=run_context,
                step_id=step_id,
            )
        finally:
            await self.durable_run_control.release(lease)
        if isinstance(result, ToolExecutionError):
            raise Stage8ToolInvocationError(
                result.safe_error_code,
                outcome_unknown=(
                    result.side_effect_state is ToolSideEffectState.UNKNOWN
                ),
            )
        if result.status is not ToolExecutionStatus.SUCCEEDED:
            raise Stage8ToolInvocationError(
                "STAGE8_TOOL_EXECUTION_FAILED", outcome_unknown=False
            )
        return json.loads(result.output.content)


class Stage8ExecutionService:
    """Stage8 业务 transaction owner；不拥有 Tool Runtime 状态。"""

    def __init__(
        self,
        database,
        *,
        tool_invoker: Callable[..., Awaitable[object]] | None = None,
        triage_service: FailureTriageService | None = None,
        mission_service: MissionService | None = None,
    ):
        self.database = database
        self.tool_invoker = tool_invoker
        self.triage_service = triage_service
        self.mission_service = mission_service or MissionService(database)

    async def build_plan(
        self,
        mission_id: str,
        *,
        case_id: str,
        environment_id: str,
        executor_id: str,
        parameters: dict[str, str] | None = None,
    ) -> ExecutionPlan:
        async with self.database.session() as session:
            mission = await repo.get_mission(session, mission_id)
            if mission is None:
                raise Stage8NotFoundError("mission not found")
            if mission.status != MissionStatus.READY_FOR_EXECUTION.value:
                raise Stage8ValidationError("mission is not ready for execution")
            plan = await repo.get_current_test_plan(session, mission_id)
            review = await repo.get_approved_test_plan_review(session, mission_id)
            if plan is None or review is None:
                raise Stage8ValidationError("approved TestPlan review is required")
        return ExecutionPlan(
            uuid.uuid4().hex,
            mission_id,
            mission.version,
            plan.subject_id,
            plan.version,
            plan.subject_digest,
            case_id,
            environment_id,
            executor_id,
            parameters or {},
        )

    async def _invoke_tool(
        self, tool_name: str, payload: dict, *, principal_agent_id: str
    ) -> object:
        assert self.tool_invoker is not None
        try:
            return await self.tool_invoker(
                tool_name, payload, principal_agent_id=principal_agent_id
            )
        except TypeError:
            # 测试 seam 兼容旧二参数 callable；生产 GovernedToolInvoker 不降级。
            if isinstance(self.tool_invoker, GovernedToolInvoker):
                raise
            return await self.tool_invoker(tool_name, payload)

    async def start_execution(self, plan: ExecutionPlan) -> ExternalExecutionJob:
        if self.tool_invoker is None:
            raise Stage8ValidationError("governed start_execution tool is not configured")
        job_id = uuid.uuid4().hex
        async with self.database.transaction() as session:
            mission = await repo.get_mission(session, plan.mission_id, for_update=True)
            current_plan = await repo.get_current_test_plan(session, plan.mission_id)
            review = await repo.get_approved_test_plan_review(session, plan.mission_id)
            if mission is None:
                raise Stage8NotFoundError("mission not found")
            if mission.status != MissionStatus.READY_FOR_EXECUTION.value:
                raise Stage8ValidationError("mission is not ready for execution")
            if mission.version != plan.mission_version:
                raise Stage8ConflictError("stale mission version")
            if current_plan is None or review is None or (
                current_plan.subject_id,
                current_plan.version,
                current_plan.subject_digest,
            ) != (
                plan.test_plan_subject_id,
                plan.test_plan_version,
                plan.test_plan_digest,
            ):
                raise Stage8ValidationError("approved TestPlan review is required")
            row = await repo.add_execution_job(session, dict(
                job_id=job_id,
                mission_id=plan.mission_id,
                execution_id=None,
                plan_id=plan.plan_id,
                case_id=plan.case_id,
                environment_id=plan.environment_id,
                executor_id=plan.executor_id,
                status=ExternalExecutionStatus.PENDING.value,
                attempt_no=1,
                auto_repair_count=0,
                plan_payload=asdict(plan),
            ))
            await self.mission_service.transition_mission(
                plan.mission_id,
                MissionStatus.EXECUTING,
                plan.mission_version,
                session=session,
            )

        try:
            raw = await self._invoke_tool(
                "stage8_start_execution",
                {
                    "case_id": plan.case_id,
                    "environment_id": plan.environment_id,
                    "executor_id": plan.executor_id,
                    "parameters": plan.parameters,
                },
                principal_agent_id="test_planning",
            )
        except Stage8ToolInvocationError as exc:
            async with self.database.transaction() as session:
                await repo.update_execution_job_by_id(session, job_id, {
                    "status": (
                        ExternalExecutionStatus.UNKNOWN.value
                        if exc.outcome_unknown
                        else ExternalExecutionStatus.CANCELLED.value
                    ),
                    "result_payload": {
                        "start_error_code": exc.safe_error_code,
                        "tool_outcome_unknown": exc.outcome_unknown,
                    },
                })
                mission = await repo.get_mission(session, plan.mission_id, for_update=True)
                if mission is not None and mission.status == MissionStatus.EXECUTING.value:
                    await self.mission_service.transition_mission(
                        plan.mission_id,
                        (
                            MissionStatus.TRIAGING
                            if exc.outcome_unknown
                            else MissionStatus.FAILED
                        ),
                        mission.version,
                        session=session,
                    )
            raise

        payload = raw.model_dump(mode="json") if isinstance(raw, BaseModel) else dict(raw)
        execution_id = payload.get("execution_id")
        if not execution_id:
            raise Stage8ValidationError("start_execution did not return execution_id")
        async with self.database.transaction() as session:
            row = await repo.get_execution_job_by_id(session, job_id, for_update=True)
            if row is None:
                raise Stage8ConflictError("execution job disappeared")
            if row.status != ExternalExecutionStatus.PENDING.value:
                raise Stage8ConflictError("execution job start state conflict")
            row = await repo.update_execution_job_by_id(session, job_id, {
                "execution_id": execution_id,
                "status": ExternalExecutionStatus.RUNNING.value,
                "started_at": datetime.now(UTC),
            })
            return _job(row)

    async def ingest_result(self, result: ExecutionResult) -> ExternalExecutionJob:
        triage_needed = False
        async with self.database.transaction() as session:
            row = await repo.get_execution_job(session, result.execution_id, for_update=True)
            if row is None:
                raise Stage8NotFoundError("execution not found")
            if row.status in {
                ExternalExecutionStatus.SUCCEEDED.value,
                ExternalExecutionStatus.FAILED.value,
                ExternalExecutionStatus.UNKNOWN.value,
                ExternalExecutionStatus.CANCELLED.value,
            }:
                return _job(row)
            if result.status not in {
                ExternalExecutionStatus.SUCCEEDED,
                ExternalExecutionStatus.FAILED,
                ExternalExecutionStatus.UNKNOWN,
            }:
                raise Stage8ValidationError("result status must be terminal")
            completed_at = result.completed_at or datetime.now(UTC)
            result_payload = {
                "execution_id": result.execution_id,
                "status": result.status.value,
                "actual_result": result.actual_result,
                "expected_result": result.expected_result,
                "failure_signature": result.failure_signature,
                "logs": result.logs,
                "completed_at": completed_at.isoformat(),
            }
            row = await repo.update_execution_job(session, result.execution_id, {
                "status": result.status.value,
                "result_payload": result_payload,
                "completed_at": completed_at,
            })
            mission = await repo.get_mission(session, row.mission_id, for_update=True)
            target = (
                MissionStatus.COMPLETED
                if result.status is ExternalExecutionStatus.SUCCEEDED
                else MissionStatus.TRIAGING
                if result.status is ExternalExecutionStatus.FAILED
                else None
            )
            if target is not None and mission.status == MissionStatus.EXECUTING.value:
                await self.mission_service.transition_mission(
                    mission.mission_id, target, mission.version, session=session
                )
            triage_needed = result.status is ExternalExecutionStatus.FAILED
            job = _job(row)
        if triage_needed and self.triage_service is not None:
            await self.triage(job)
            async with self.database.session() as session:
                refreshed = await repo.get_execution_job(session, result.execution_id)
            if refreshed is None:
                raise Stage8ConflictError("execution job disappeared after triage")
            return _job(refreshed)
        return job

    async def triage_execution(self, execution_id: str) -> FailureTriageResult:
        async with self.database.session() as session:
            row = await repo.get_execution_job(session, execution_id)
            if row is None:
                raise Stage8NotFoundError("execution not found")
            job = _job(row)
        return await self.triage(job)

    async def triage(self, job: ExternalExecutionJob) -> FailureTriageResult:
        if self.triage_service is None:
            raise Stage8ValidationError("failure triage service is not configured")
        if job.execution_id is None or job.status is not ExternalExecutionStatus.FAILED:
            raise Stage8ValidationError("only failed execution can be triaged")
        async with self.database.transaction() as session:
            claimed = await repo.claim_execution_triage(session, job.execution_id)
            if claimed is None:
                current = await repo.get_execution_job(session, job.execution_id, for_update=True)
                payload = current.triage_payload or {}
                if payload.get("state") == "COMPLETED":
                    return FailureTriageResult.model_validate(payload["result"])
                raise Stage8ConflictError("failure triage is already running")

        try:
            async with self.database.session() as session:
                mission = await repo.get_mission(session, job.mission_id)
            if mission is None:
                raise Stage8NotFoundError("mission not found")
            plan = claimed.plan_payload
            result = job.result or {}
            expected_result = result.get("expected_result")
            logs = list(result.get("logs") or [])
            evidence = {
                "EXEC_RESULT": result,
                "CASE_EXPECTED": {
                    "case_id": job.case_id,
                    "expected_result": expected_result,
                },
                "ENVIRONMENT": {"environment_id": job.environment_id},
                "EXECUTOR": {"executor_id": job.executor_id},
                "FEATURE_BINDING": {"feature_id": mission.feature_id},
                "TEST_PLAN_BINDING": {
                    "subject_id": plan["test_plan_subject_id"],
                    "version": int(plan["test_plan_version"]),
                    "digest": plan["test_plan_digest"],
                },
                **{
                    f"LOG_{index + 1}": {"line": line}
                    for index, line in enumerate(logs)
                },
            }
            package = FailureEvidencePackage(
                mission_id=job.mission_id,
                execution_id=job.execution_id,
                feature_id=mission.feature_id,
                test_plan_subject_id=plan["test_plan_subject_id"],
                test_plan_version=int(plan["test_plan_version"]),
                test_plan_digest=plan["test_plan_digest"],
                case_id=job.case_id,
                environment_id=job.environment_id,
                executor_id=job.executor_id,
                expected_result=expected_result,
                actual_result=result.get("actual_result", ""),
                logs=logs,
                failure_signature=result.get("failure_signature"),
                evidence=evidence,
            )
            request = FailureTriageRequest(
                mission_id=job.mission_id,
                evidence=package.evidence,
                available_evidence=[
                    {"evidence_id": evidence_id, **value}
                    for evidence_id, value in evidence.items()
                ],
            )
            triage = await self.triage_service.run(request)
            triage_payload: dict = {
                "state": "COMPLETED",
                "result": triage.model_dump(mode="json"),
            }
            if triage.classification == FailureClassification.PRODUCT:
                if triage.ticket_draft is None:
                    raise Stage8ValidationError("PRODUCT triage requires ticket_draft")
                if self.tool_invoker is None:
                    raise Stage8ValidationError(
                        "governed create_ticket tool is not configured"
                    )
                triage_payload["ticket_request"] = await self._invoke_tool(
                    "stage8_create_ticket",
                    triage.ticket_draft.model_dump(mode="json"),
                    principal_agent_id="failure_triage",
                )
            async with self.database.transaction() as session:
                await repo.update_execution_job(
                    session, job.execution_id, {"triage_payload": triage_payload}
                )
            return triage
        except Exception:
            async with self.database.transaction() as session:
                current = await repo.get_execution_job(
                    session, job.execution_id, for_update=True
                )
                if current is not None and (
                    current.triage_payload or {}
                ).get("state") == "RUNNING":
                    await repo.update_execution_job(
                        session, job.execution_id, {"triage_payload": null()}
                    )
            raise
