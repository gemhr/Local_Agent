"""受控 HUMAN 业务评测入口；复用原 Receipt、Run 和业务授权。"""
import json
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, StrictStr, StrictInt
from fastapi import HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select

from core.auth import ObjectAuthorizationService
from core.agent_platform.application import ExecutionRequest
from core.agent_platform.ci_status import AGENT_ID as STATUS_AGENT, freeze_route, OUTPUT_SCHEMA
from core.incidents.service import AGENT_ID
from core.incidents.models import InvestigationRow
from core.persistence.models import RuntimeLoopActionRow
from core.persistence.repositories.agent_quality import AgentQualityReceiptRepository
from core.runtime.agent_loop.quality_contract import canonical_bytes, catalog_digest, parse_quality_json
from core.runtime.agent_loop.contracts import LoopSuspended
from core.runtime.memory_scope import MemoryAccessContext
from core.runtime.business_output_schema import schema_matches
from core.runtime.adaptive_execution_evidence import build_evidence, strategy

PROTOCOL = "adaptive-execution-evaluate.v1"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BusinessInput(StrictModel):
    slice: Literal["STATUS", "DIAGNOSIS"]
    incident_id: UUID
    execution_id: UUID
    execution_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")


class FrozenCase(StrictModel):
    case_version: StrictStr
    case_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    input: BusinessInput


class Configuration(StrictModel):
    version: Literal["adaptive-evaluation-config.v1"]
    project_id: UUID
    principal_id: UUID
    tenant_id: StrictStr
    cases: dict[str, FrozenCase] = Field(min_length=1)


class EvaluationRequest(StrictModel):
    protocol_version: Literal["adaptive-execution-evaluate.v1"]
    project_id: UUID
    evaluation_run_id: UUID
    attempt_id: UUID
    request_id: UUID
    case_id: UUID
    case_version: StrictStr
    case_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    input_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    group: Literal["BASELINE", "CANDIDATE_A", "CANDIDATE_B"]
    policy_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    timeout_milliseconds: StrictInt = Field(ge=1, le=300000)
    input: BusinessInput


class AdaptiveEvaluationService:
    def __init__(self, config, incidents, application, investigator, repository):
        self.config, self.incidents, self.application, self.investigator, self.repository = config, incidents, application, investigator, repository
        self.receipts = AgentQualityReceiptRepository(incidents.database)
        self.authorization = ObjectAuthorizationService(incidents.database)

    def authorize(self, principal, project_id):
        if (principal.principal_kind != "HUMAN" or principal.user_id != self.config.principal_id
                or principal.tenant_id != self.config.tenant_id or project_id != self.config.project_id):
            raise HTTPException(403, "ADAPTIVE_EVALUATION_FORBIDDEN")

    async def capture(self, principal, run_id):
        receipt = await self.receipts.read(run_id)
        if (receipt is None or receipt.execution_manifest.get("protocol_version") != PROTOCOL
                or receipt.principal_id != str(principal.user_id) or receipt.tenant_id != principal.tenant_id):
            raise HTTPException(404, "ADAPTIVE_RECEIPT_MISSING")
        self.authorize(principal, UUID(receipt.evaluation_project_id))
        if receipt.sealed_response:
            await self.authorization._require_existing_binding(principal, "RUN", run_id)
            return receipt.sealed_response
        image = await self.repository.load(run_id)
        if image is None or image.root.status not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return None
        await self.authorization._require_existing_binding(principal, "RUN", run_id)
        frozen = receipt.execution_manifest
        body = None
        if image.root.status == "SUCCEEDED":
            succeeded = [s for s in image.steps if s.status == "SUCCEEDED"]
            if len(succeeded) == 1:
                body = (succeeded[0].typed_result_payload or {}).get("content")
        validation, outcome = "NOT_EVALUATED", "NOT_AVAILABLE"
        if frozen["slice"] == "DIAGNOSIS":
            async with self.incidents.database.session() as session:
                row = await session.scalar(select(InvestigationRow).where(InvestigationRow.run_id == run_id))
            row, _ = await self.investigator.project_if_terminal(row.investigation_id)
            validation = "ACCEPTED" if row.projection_status == "READY" else "REJECTED"
            outcome = (row.report_payload or {}).get("diagnosis_status", "REJECTED")
            if validation != "ACCEPTED":
                body = None
        elif body is not None:
            try:
                value = parse_quality_json(body.encode("utf-8"))
            except ValueError:
                value = None
            validation = "ACCEPTED" if schema_matches(OUTPUT_SCHEMA, value) else "REJECTED"
            outcome = "STATUS" if validation == "ACCEPTED" else "REJECTED"
            if validation != "ACCEPTED":
                body = None
        delivered = None
        if image.root.execution_policy == "fixed-plan.v1":
            feed = self.application._chat_service._coordinated_runtime_factory.services.client_event_feed
            events = await feed.read_after(run_id, 0, 200)
            delivered = any(e.event_type == "run.completed" and e.payload.get("business_output_valid") is True for e in events)
            if image.root.status == "SUCCEEDED" and validation == "ACCEPTED" and not delivered:
                return None
        async with self.incidents.database.session() as session:
            actions = tuple((await session.scalars(select(RuntimeLoopActionRow).where(RuntimeLoopActionRow.run_id == run_id)
                .order_by(RuntimeLoopActionRow.frame_id, RuntimeLoopActionRow.iteration_no, RuntimeLoopActionRow.ordinal))).all())
        evidence = build_evidence(image, receipt.correlation, frozen["strategy"],
            dict(slice=frozen["slice"], outcome=outcome, validation=validation, output=body,
                runtime_delivered=delivered,
                runtime_error=(image.loop_frames[0].stop_code if image.loop_frames else None)), actions)
        async with self.incidents.database.transaction() as session:
            return await self.receipts.seal_in_transaction(session, run_id, evidence)

    async def execute(self, principal, request):
        self.authorize(principal, request.project_id)
        frozen = self.config.cases.get(str(request.case_id))
        policy = strategy(request.group)
        business = request.input.model_dump(mode="json")
        if (frozen is None or frozen.model_dump(mode="json") != dict(case_version=request.case_version,
                case_digest=request.case_digest, dataset_digest=request.dataset_digest, input=business)
                or catalog_digest(business) != request.input_digest or request.policy_digest != policy["digest"]):
            raise HTTPException(422, "ADAPTIVE_FROZEN_INPUT_INVALID")
        detail = await self.incidents.execution_detail(principal, str(request.input.incident_id), str(request.input.execution_id))
        if catalog_digest(dict(execution_id=detail["execution_id"], state_revision=detail["state_revision"], summary=detail["summary"])) != request.input.execution_digest:
            raise HTTPException(409, "ADAPTIVE_EXECUTION_CHANGED")
        correlation = request.model_dump(mode="json", exclude={"input", "protocol_version", "group", "policy_digest", "timeout_milliseconds"})
        correlation["run_id"] = str(request.attempt_id)
        correlation["evaluation_project_id"] = correlation.pop("project_id")
        run_id = correlation["run_id"]
        winner, _ = await self.receipts.admit(correlation=correlation, principal=principal,
            manifest=dict(protocol_version=PROTOCOL, slice=request.input.slice, strategy=policy, business_input=business,
                timeout_milliseconds=request.timeout_milliseconds), profile=dict(version=PROTOCOL))
        if not winner:
            return await self.capture(principal, run_id)
        if request.input.slice == "STATUS":
            query = dict(task_type="CI_EXECUTION_STATUS", incident_id=str(request.input.incident_id), execution_id=str(request.input.execution_id))
            execution = ExecutionRequest(agent_id=STATUS_AGENT, input=json.dumps(query), run_id=run_id, timeout_seconds=request.timeout_milliseconds / 1000)
            self.application._resolve_request(execution)
            route = await freeze_route(self.incidents, principal, query, policy["query_mode"])
            await self.authorization.bind_new(principal, "RUN", run_id)
            try:
                result = await self.application.execute(execution, adaptive_route=route)
                if await self.repository.load(run_id) is None:
                    raise HTTPException(503, result.error_code or "ADAPTIVE_ADMISSION_FAILED")
            except LoopSuspended:
                return None
        else:
            # 使用原调查 admit；关联 verification 查询不能冒充原调查执行。
            async with self.incidents.database.session() as session:
                incident = await self.incidents.owned_incident(session, principal, str(request.input.incident_id))
            if incident.execution_id != str(request.input.execution_id):
                raise HTTPException(422, "ADAPTIVE_INVESTIGATION_BINDING_INVALID")
            row, _ = await self.incidents.admit(principal, incident.incident_id, str(request.request_id), run_id=run_id, session_id=run_id)
            access = MemoryAccessContext.from_principal(principal, AGENT_ID, row.session_id)
            await self.authorization.bind_new(principal, "RUN", run_id)
            await self.authorization.bind_new(principal, "CONVERSATION", access.conversation_id)
            try:
                await self.application.execute_evaluation(ExecutionRequest(agent_id=AGENT_ID, input="调查该冻结 CI 执行失败，使用授权工具取证。",
                    run_id=run_id, session_id=row.session_id, timeout_seconds=request.timeout_milliseconds / 1000),
                    execution_policy="durable-agent-loop.v2", adaptive_stop_enabled=policy["diagnosis_stop"],
                    loop_correlation={k: correlation[k] for k in ("evaluation_run_id", "attempt_id", "request_id")}, memory_access_context=access)
            except LoopSuspended:
                return None
        return await self.capture(principal, run_id)


def register(app):
    def service(request):
        result = getattr(request.app.state, "adaptive_evaluation_service", None)
        if result is None:
            raise HTTPException(503, "ADAPTIVE_EVALUATION_DISABLED")
        return result

    @app.post("/api/runtime/evaluation-execute/adaptive/v1")
    async def execute(payload: EvaluationRequest, request: Request):
        result = await service(request).execute(request.state.principal, payload)
        if result is None:
            return Response(canonical_bytes(dict(protocol_version=PROTOCOL, run_id=str(payload.attempt_id), status="PENDING")), status_code=202, media_type="application/json")
        return Response(result, media_type="application/json")

    @app.get("/api/runtime/runs/{run_id}/adaptive-evidence/v1")
    async def read(run_id: UUID, request: Request):
        result = await service(request).capture(request.state.principal, str(run_id))
        if result is None:
            return Response(status_code=202)
        return Response(result, media_type="application/json")
