"""Stage8-WP3：外部执行、结果回调与失败分流业务闭环。"""

from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
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
    ApprovalStatus,
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
    EnvironmentRequirements,
    GeneratedCaseArtifact,
    ExecutionResult,
    ExternalExecutionJob,
    ExternalExecutionStatus,
    FailureEvidencePackage,
    MissionStatus,
    ResourceUnavailable,
)
from core.stage8.platforms import SearchEnvironmentsRequest, TicketDraft
from core.stage8.observation import ExecutionResultParser
from core.stage8.execution_list import ExecutionListBuilder
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
                approval_id = uuid.uuid4().hex
                request = await self.durable_approval.create(ApprovalRequest(
                    approval_id=approval_id,
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
                # Approval stores only safe digests.  Prepare the same immutable
                # invocation in Tool Runtime so continuation can resume it.
                durable_invocation = getattr(self.tool_execution, "durable_invocation_service", None)
                if durable_invocation is not None:
                    await durable_invocation.prepare(
                        lease=lease, step_id=step_id, invocation=invocation,
                        tool_name=spec.tool_name, approval_id=request.approval_id,
                        invocation_binding_digest=request.invocation_binding_digest,
                    )
                return {
                    "status": "APPROVAL_REQUIRED",
                    "tool_name": tool_name,
                    "run_id": request.run_id,
                    "approval_id": request.approval_id,
                    "invocation_binding_digest": request.invocation_binding_digest,
                    "invocation_id": invocation.invocation_id,
                    "step_id": step_id,
                    "request_snapshot": dict(payload),
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

    async def resume_approved(self, continuation):
        """Resume exactly the invocation bound to the durable approval."""
        approval = await self.durable_approval.get(continuation.approval_id)
        if approval is None or (await self.durable_approval.status(continuation.approval_id)) is not ApprovalStatus.APPROVED:
            raise Stage8ValidationError("ticket approval is not approved")
        if approval.invocation_id != continuation.tool_invocation_id or approval.invocation_binding_digest != continuation.invocation_binding_digest:
            raise Stage8ValidationError("ticket approval binding mismatch")
        if approval.tool_name != "stage8_create_ticket":
            raise Stage8ValidationError("ticket continuation requires stage8_create_ticket")
        registration = self.registry.require(approval.tool_name)
        invocation = registration.adapter.build_invocation(
            json.dumps(continuation.request_snapshot, ensure_ascii=False)
        )
        invocation = replace(invocation, invocation_id=continuation.tool_invocation_id)
        binding = compute_invocation_binding_digest(
            invocation_identity_digest=safe_key_digest(invocation.invocation_id),
            tool_name=invocation.tool_name,
            arguments_digest=invocation.arguments_digest,
            idempotency_key_digest=safe_key_digest(invocation.idempotency_key),
            resource_key_digest=safe_key_digest(invocation.resource_key),
            risk_level=approval.risk_level,
            risk_facts=approval.risk_facts,
        )
        if binding != continuation.invocation_binding_digest:
            raise Stage8ValidationError("ticket request snapshot binding mismatch")
        durable_invocations = getattr(self.tool_execution, "durable_invocation_service", None)
        if durable_invocations is None:
            raise Stage8ValidationError("durable tool invocation service is not configured")
        durable_invocation = await durable_invocations.get(continuation.tool_invocation_id)
        if (
            durable_invocation is None
            or durable_invocation.approval_id != approval.approval_id
            or durable_invocation.run_id != approval.run_id
            or durable_invocation.step_id != approval.step_id
            or durable_invocation.tool_name != approval.tool_name
            or durable_invocation.invocation_binding_digest != approval.invocation_binding_digest
        ):
            raise Stage8ValidationError("durable tool invocation binding mismatch")
        if durable_invocation.state.value != "PREPARED":
            raise Stage8ToolInvocationError(
                "TOOL_INVOCATION_NOT_RESUMABLE", outcome_unknown=True
            )
        lease = await self.durable_run_control.claim(approval.run_id, self.owner_id)
        try:
            context, _ = create_run_context(run_id=approval.run_id, entry_agent_id="failure_triage", timeout_seconds=30)
            context.attach_durable_lease(lease)
            context.attach_ownership_validator(lambda: self.durable_run_control.assert_current(lease))
            context.attach_budget_ledger(BudgetLedger(RunBudget(max_tool_calls=1)))
            claim = await self.durable_approval.claim_execution(
                lease=lease, approval_id=approval.approval_id,
                invocation_binding_digest=approval.invocation_binding_digest,
            )
            result = await self.tool_execution.execute(
                invocation=invocation, adapter=registration.adapter,
                run_context=context, step_id=approval.step_id,
                durable_approval_id=approval.approval_id,
                durable_execution_claim_id=claim.claim_id,
                durable_binding_digest=approval.invocation_binding_digest,
            )
        finally:
            await self.durable_run_control.release(lease)
        if isinstance(result, ToolExecutionError):
            raise Stage8ToolInvocationError(result.safe_error_code, outcome_unknown=result.side_effect_state is ToolSideEffectState.UNKNOWN)
        return json.loads(result.output.content)


def _execution_request_digest(
    mission_id: str,
    artifact_id: str,
    parameters: dict[str, str],
) -> str:
    payload = {
        "mission_id": mission_id,
        "generated_case_artifact_id": artifact_id,
        "parameters": parameters,
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _environment_status(payload: dict) -> str | None:
    return payload.get("status") or payload.get("availability")


def _environment_matches(
    payload: dict,
    requirements: EnvironmentRequirements,
) -> bool:
    return (
        (requirements.version is None or payload.get("version") == requirements.version)
        and (
            requirements.network_type is None
            or payload.get("network_type") == requirements.network_type
        )
        and (
            requirements.hardware_type is None
            or payload.get("board") == requirements.hardware_type
        )
        and set(requirements.required_capabilities).issubset(
            set(payload.get("capabilities") or ())
        )
        and set(requirements.feature_flags).issubset(
            set(payload.get("feature_flags") or ())
        )
    )


def _execution_plan_from_payload(payload: dict) -> ExecutionPlan:
    values = dict(payload)
    values["environment_requirements"] = EnvironmentRequirements(
        **values.get("environment_requirements", {})
    )
    return ExecutionPlan(**values)


class Stage8ExecutionService:
    """Stage8 业务 transaction owner；不拥有 Tool Runtime 状态。"""

    def __init__(
        self,
        database,
        *,
        tool_invoker: Callable[..., Awaitable[object]] | None = None,
        triage_service: FailureTriageService | None = None,
        mission_service: MissionService | None = None,
        execution_list_builder: ExecutionListBuilder | None = None,
        ticket_continuation_service=None,
    ):
        self.database = database
        self.tool_invoker = tool_invoker
        self.triage_service = triage_service
        self.mission_service = mission_service or MissionService(database)
        self.execution_list_builder = execution_list_builder or ExecutionListBuilder()
        self.ticket_continuation_service = ticket_continuation_service

    async def _plan_from_artifact(
        self,
        mission_id: str,
        artifact_id: str,
        parameters: dict[str, str],
        execution_request_digest: str,
    ):
        async with self.database.session() as session:
            mission = await repo.get_mission(session, mission_id)
            current_plan = await repo.get_current_test_plan(session, mission_id)
            review = await repo.get_approved_test_plan_review(session, mission_id)
            artifact = await repo.get_generated_case_artifact_by_id(session, artifact_id)
            if mission is None:
                raise Stage8NotFoundError("mission not found")
            if mission.status not in {
                MissionStatus.READY_FOR_EXECUTION.value,
                MissionStatus.WAITING_FOR_RESOURCE.value,
            }:
                raise Stage8ValidationError("mission is not ready for execution")
            if artifact is None or artifact.mission_id != mission_id:
                raise Stage8NotFoundError("generated case artifact not found")
            binding = (artifact.test_plan_subject_id, artifact.test_plan_version, artifact.test_plan_digest)
            if current_plan is None or review is None or (current_plan.subject_id, current_plan.version, current_plan.subject_digest) != binding or (review.subject_id, review.subject_version, review.subject_digest) != binding:
                raise Stage8ValidationError("generated case artifact does not belong to current approved TestPlan")
            requirements_payload = current_plan.payload.get("environment_requirements", {})
            if isinstance(requirements_payload, list):
                requirements_payload = {"required_capabilities": requirements_payload}
            try:
                requirements = EnvironmentRequirements(
                    version=requirements_payload.get("version"),
                    network_type=requirements_payload.get("network_type"),
                    hardware_type=requirements_payload.get("hardware_type"),
                    required_capabilities=tuple(requirements_payload.get("required_capabilities", ())),
                    feature_flags=tuple(requirements_payload.get("feature_flags", ())),
                )
            except (AttributeError, TypeError, ValueError) as exc:
                raise Stage8ValidationError("invalid environment requirements") from exc
            mission_version = mission.version
            feature = GeneratedCaseArtifact(
                artifact.artifact_id, artifact.mission_id, artifact.test_plan_subject_id,
                artifact.test_plan_version, artifact.test_plan_digest, artifact.scenario_id,
                artifact.provider_case_id, artifact.case_path, artifact.status, artifact.created_at,
            )
        if requirements.is_empty:
            return await self._resource_unavailable(
                mission_id, requirements, 0, "RESOURCE_REQUIREMENTS_UNSPECIFIED"
            )
        query = SearchEnvironmentsRequest(
            version=requirements.version, network_type=requirements.network_type,
            hardware_type=requirements.hardware_type,
            required_capabilities=list(requirements.required_capabilities),
            feature_flags=list(requirements.feature_flags),
        )
        candidates = await self._invoke_tool("stage8_search_environments", query.model_dump(mode="json"), principal_agent_id="test_planning")
        if isinstance(candidates, BaseModel):
            candidates = candidates.model_dump(mode="json")
        if isinstance(candidates, dict):
            candidates = candidates.get("root", candidates.get("items", []))
        candidates = sorted(
            (item for item in (candidates or []) if _environment_matches(item, requirements)),
            key=lambda item: item["environment_id"],
        )
        free = [item for item in candidates if _environment_status(item) == "FREE"]
        if not free:
            busy = sum(1 for item in candidates if _environment_status(item) != "FREE")
            return await self._resource_unavailable(
                mission_id, requirements, busy, "no compatible FREE environment"
            )
        return [
            (feature, requirements, mission_version, item, parameters, execution_request_digest)
            for item in free
        ]

    async def _resource_unavailable(
        self,
        mission_id: str,
        requirements: EnvironmentRequirements,
        busy_count: int,
        reason: str,
    ) -> ResourceUnavailable:
        async with self.database.transaction() as session:
            current = await repo.get_mission(session, mission_id, for_update=True)
            if current and current.status == MissionStatus.READY_FOR_EXECUTION.value:
                await self.mission_service.transition_mission(
                    mission_id,
                    MissionStatus.WAITING_FOR_RESOURCE,
                    current.version,
                    session=session,
                )
        return ResourceUnavailable(
            mission_id,
            "WAITING_FOR_RESOURCE",
            requirements.required_capabilities,
            busy_count,
            reason,
        )

    async def _validate_execution_plan_binding(self, plan: ExecutionPlan) -> None:
        async with self.database.session() as session:
            artifact = await repo.get_generated_case_artifact_by_id(
                session, plan.generated_case_artifact_id
            )
            current_plan = await repo.get_current_test_plan(session, plan.mission_id)
            review = await repo.get_approved_test_plan_review(session, plan.mission_id)
        binding = (
            plan.test_plan_subject_id,
            plan.test_plan_version,
            plan.test_plan_digest,
        )
        if (
            artifact is None
            or artifact.mission_id != plan.mission_id
            or (
                artifact.test_plan_subject_id,
                artifact.test_plan_version,
                artifact.test_plan_digest,
            ) != binding
            or current_plan is None
            or (
                current_plan.subject_id,
                current_plan.version,
                current_plan.subject_digest,
            ) != binding
            or review is None
            or (
                review.subject_id,
                review.subject_version,
                review.subject_digest,
            ) != binding
        ):
            raise Stage8ValidationError(
                "generated case artifact does not belong to current approved TestPlan"
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

    async def start_execution(
        self,
        mission_id: str,
        *,
        generated_case_artifact_id: str,
        parameters: dict[str, str] | None = None,
    ) -> ExternalExecutionJob | ResourceUnavailable:
        if self.tool_invoker is None:
            raise Stage8ValidationError("governed start_execution tool is not configured")
        canonical_parameters = dict(parameters or {})
        execution_request_digest = _execution_request_digest(
            mission_id, generated_case_artifact_id, canonical_parameters
        )
        existing_pending = None
        async with self.database.session() as session:
            existing_jobs = await repo.list_execution_jobs(session, mission_id)
        for existing in existing_jobs:
            if existing.plan_payload.get("execution_request_digest") == execution_request_digest:
                existing_plan = _execution_plan_from_payload(existing.plan_payload)
                await self._validate_execution_plan_binding(existing_plan)
                if existing.status in {
                    ExternalExecutionStatus.RUNNING.value,
                    ExternalExecutionStatus.UNKNOWN.value,
                }:
                    return _job(existing)
                if existing.status == ExternalExecutionStatus.PENDING.value:
                    existing_pending = existing
                    break

        if existing_pending is not None:
            plan = _execution_plan_from_payload(existing_pending.plan_payload)
            job_id = existing_pending.job_id
        else:
            candidates = await self._plan_from_artifact(
                mission_id,
                generated_case_artifact_id,
                canonical_parameters,
                execution_request_digest,
            )
            if isinstance(candidates, ResourceUnavailable):
                return candidates
            # Candidate loop is bounded by this single search result.
            for artifact, requirements, mission_version, candidate, params, request_digest in candidates:
                current = await self._invoke_tool("stage8_get_environment", {"environment_id": candidate["environment_id"]}, principal_agent_id="test_planning")
                current_payload = current.model_dump(mode="json") if isinstance(current, BaseModel) else dict(current)
                if (
                    current_payload.get("environment_id") != candidate["environment_id"]
                    or not current_payload.get("ip")
                    or _environment_status(current_payload) != "FREE"
                    or not _environment_matches(current_payload, requirements)
                ):
                    continue
                execution_list_ref = self.execution_list_builder.build(
                    mission_id, artifact, request_digest
                )
                plan = ExecutionPlan(
                    uuid.uuid4().hex,
                    mission_id,
                    mission_version,
                    artifact.test_plan_subject_id,
                    artifact.test_plan_version,
                    artifact.test_plan_digest,
                    artifact.artifact_id,
                    artifact.provider_case_id,
                    artifact.case_path,
                    current_payload["environment_id"],
                    current_payload.get("ip", ""),
                    execution_list_ref,
                    request_digest,
                    "EXECUTOR-001",
                    requirements,
                    params,
                )
                break
            else:
                return await self._resource_unavailable(
                    mission_id,
                    candidates[0][1],
                    len(candidates),
                    "all candidates became busy before execution",
                )
            job_id = uuid.uuid4().hex
            async with self.database.transaction() as session:
                mission = await repo.get_mission(session, plan.mission_id, for_update=True)
                current_plan = await repo.get_current_test_plan(session, plan.mission_id)
                review = await repo.get_approved_test_plan_review(session, plan.mission_id)
                if mission is None:
                    raise Stage8NotFoundError("mission not found")
                concurrent = next((
                    row
                    for row in await repo.list_execution_jobs(session, plan.mission_id)
                    if row.plan_payload.get("execution_request_digest")
                    == execution_request_digest
                    and row.status in {
                        ExternalExecutionStatus.PENDING.value,
                        ExternalExecutionStatus.RUNNING.value,
                        ExternalExecutionStatus.UNKNOWN.value,
                    }
                ), None)
                if concurrent is not None:
                    plan = _execution_plan_from_payload(concurrent.plan_payload)
                    job_id = concurrent.job_id
                artifact = await repo.get_generated_case_artifact_by_id(
                    session, plan.generated_case_artifact_id
                )
                binding = (
                    plan.test_plan_subject_id,
                    plan.test_plan_version,
                    plan.test_plan_digest,
                )
                if (
                    artifact is None
                    or artifact.mission_id != plan.mission_id
                    or (
                        artifact.test_plan_subject_id,
                        artifact.test_plan_version,
                        artifact.test_plan_digest,
                    ) != binding
                    or current_plan is None
                    or (
                        current_plan.subject_id,
                        current_plan.version,
                        current_plan.subject_digest,
                    ) != binding
                    or review is None
                ):
                    raise Stage8ValidationError(
                        "generated case artifact does not belong to current approved TestPlan"
                    )
                if concurrent is not None and concurrent.status in {
                    ExternalExecutionStatus.RUNNING.value,
                    ExternalExecutionStatus.UNKNOWN.value,
                }:
                    return _job(concurrent)
                if mission.status == MissionStatus.WAITING_FOR_RESOURCE.value:
                    mission = await self.mission_service.transition_mission(
                        plan.mission_id,
                        MissionStatus.READY_FOR_EXECUTION,
                        mission.version,
                        session=session,
                    )
                    plan = replace(plan, mission_version=mission.version)
                if mission.status != MissionStatus.READY_FOR_EXECUTION.value:
                    raise Stage8ValidationError("mission is not ready for execution")
                if mission.version != plan.mission_version:
                    raise Stage8ConflictError("stale mission version")
                if concurrent is None:
                    await repo.add_execution_job(session, dict(
                        job_id=job_id,
                        mission_id=plan.mission_id,
                        execution_id=None,
                        plan_id=plan.plan_id,
                        case_id=plan.provider_case_id,
                        environment_id=plan.environment_id,
                        executor_id=plan.executor_id,
                        status=ExternalExecutionStatus.PENDING.value,
                        attempt_no=1,
                        auto_repair_count=0,
                        plan_payload=asdict(plan),
                    ))

        try:
            raw = await self._invoke_tool(
                "stage8_start_execution",
                {
                    "provider_case_id": plan.provider_case_id,
                    "case_path": plan.case_path,
                    "environment_id": plan.environment_id,
                    "environment_ip": plan.environment_ip,
                    "execution_list_ref": plan.execution_list_ref,
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
            raise
        except Exception:
            async with self.database.transaction() as session:
                await repo.update_execution_job_by_id(session, job_id, {
                    "status": ExternalExecutionStatus.UNKNOWN.value,
                    "result_payload": {
                        "start_error_code": "STAGE8_START_OUTCOME_UNKNOWN",
                        "tool_outcome_unknown": True,
                    },
                })
            raise

        payload = raw.model_dump(mode="json") if isinstance(raw, BaseModel) else dict(raw)
        execution_id = payload.get("execution_id")
        if not execution_id:
            async with self.database.transaction() as session:
                await repo.update_execution_job_by_id(session, job_id, {
                    "status": ExternalExecutionStatus.UNKNOWN.value,
                    "result_payload": {
                        "start_error_code": "STAGE8_EXECUTION_ID_MISSING",
                        "tool_outcome_unknown": True,
                    },
                })
            raise Stage8ValidationError("start_execution did not return execution_id")
        async with self.database.transaction() as session:
            row = await repo.get_execution_job_by_id(session, job_id, for_update=True)
            if row is None:
                raise Stage8ConflictError("execution job disappeared")
            if row.status == ExternalExecutionStatus.RUNNING.value:
                return _job(row)
            if row.status != ExternalExecutionStatus.PENDING.value:
                raise Stage8ConflictError("execution job start state conflict")
            row = await repo.update_execution_job_by_id(session, job_id, {
                "execution_id": execution_id,
                "status": ExternalExecutionStatus.RUNNING.value,
                "started_at": datetime.now(UTC),
            })
            mission = await repo.get_mission(session, plan.mission_id, for_update=True)
            if mission is None:
                raise Stage8NotFoundError("mission not found")
            if mission.status == MissionStatus.READY_FOR_EXECUTION.value:
                await self.mission_service.transition_mission(
                    plan.mission_id,
                    MissionStatus.EXECUTING,
                    mission.version,
                    session=session,
                )
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
                "error_code": result.error_code,
                "error_message": result.error_message,
                "failed_step": result.failed_step,
                "result_location": result.result_location,
                "log_excerpt": result.log_excerpt,
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

    async def observe_once(self, job_id: str) -> ExternalExecutionJob:
        """从 provider 取得一次真实观察；终态交给 canonical ingest_result。"""
        async with self.database.session() as session:
            row = await repo.get_execution_job_by_id(session, job_id)
            if row is None:
                raise Stage8NotFoundError("execution job not found")
            job = _job(row)
        if job.status in {
            ExternalExecutionStatus.SUCCEEDED,
            ExternalExecutionStatus.FAILED,
            ExternalExecutionStatus.UNKNOWN,
            ExternalExecutionStatus.CANCELLED,
        }:
            return job
        if job.execution_id is None:
            return job
        if self.tool_invoker is None:
            raise Stage8ValidationError("execution observation is not configured")

        status_payload = await self._invoke_tool(
            "stage8_get_execution_status",
            {"execution_id": job.execution_id},
            principal_agent_id="execution_observer",
        )
        if status_payload.get("execution_id") != job.execution_id:
            raise Stage8ValidationError("execution status identity mismatch")
        provider_status = str(status_payload.get("status", "")).upper()
        if provider_status in {"PENDING", "RUNNING"}:
            return job
        if provider_status not in {"COMPLETED", "SUCCEEDED", "FAILED"}:
            return job

        result_payload = await self._invoke_tool(
            "stage8_get_execution_result",
            {"execution_id": job.execution_id, "max_lines": 1000},
            principal_agent_id="execution_observer",
        )
        if result_payload.get("execution_id") != job.execution_id:
            raise Stage8ValidationError("execution result identity mismatch")
        if not result_payload.get("ready", False) or not result_payload.get("result_location"):
            return job
        normalized = ExecutionResultParser().parse(
            job.execution_id,
            list(result_payload.get("lines") or []),
            result_location=result_payload["result_location"],
            log_excerpt=result_payload.get("log_excerpt"),
        )
        if normalized is None:
            return job
        return await self.ingest_result(normalized.to_execution_result(job.execution_id))

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
            if not logs and result.get("log_excerpt"):
                logs = str(result["log_excerpt"]).splitlines()
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
                ticket_request = await self._invoke_tool(
                    "stage8_create_ticket",
                    triage.ticket_draft.model_dump(mode="json"),
                    principal_agent_id="failure_triage",
                )
                triage_payload["ticket_request"] = ticket_request
                if self.ticket_continuation_service is not None and ticket_request.get("status") == "APPROVAL_REQUIRED":
                    required = {"approval_id", "invocation_id", "invocation_binding_digest", "request_snapshot"}
                    if required.issubset(ticket_request):
                        continuation = await self.ticket_continuation_service.create_from_approval(
                            mission_id=job.mission_id, execution_job_id=job.job_id,
                            triage_id=f"triage:{job.execution_id}",
                            ticket_draft_id=f"ticket-draft:{job.execution_id}",
                            draft=ticket_request["request_snapshot"], approval=ticket_request,
                        )
                        triage_payload["ticket_continuation_id"] = continuation.continuation_id
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
