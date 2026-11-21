"""SERVICE 对已经执行的覆盖工作流作只读来源重验，不生成计划或执行。"""
from typing import Literal
from pydantic import Field
from core.incidents.contracts import StrictDTO, CIError
from core.incidents.baseband_quality import SCOPE
from core.auth import require_scope
from core.feature_impact.knowledge import digest


class Selector(StrictDTO):
    task_id: str
    report_id: str
    plan_id: str
    test_id: str
    instance_id: str
    execution_id: str
    incident_id: str
    diagnosis_kind: Literal["RULE"]


class Input(StrictDTO):
    schema_version: Literal["coverage-execution-input.v1"]
    task: dict
    executor_ref: Literal["local-integer-executor@1"]


class Evaluate(StrictDTO):
    protocol: Literal["coverage-execution-request.v1"]
    project_id: str
    run_id: str
    attempt_id: str
    request_id: str
    case_id: str
    frozen_input_digest: str = Field(pattern="^[0-9a-f]{64}$")
    input: Input
    selector: Selector


class InputV2(Input):
    schema_version: Literal["coverage-execution-input.v2"]


class EvaluateV2(Evaluate):
    protocol: Literal["coverage-execution-request.v2"]
    input: InputV2


async def evaluate(service,config,principal,body,repository=None):
    from core.incidents.models import IncidentRow
    from core.ci_simulation.models import CIExecutionRow
    from core.ci_simulation.coverage_execution import approved
    from core.ci_simulation.daas_assets import build
    from core.incidents.joint_diagnosis import rule_report
    from core.feature_impact.service import BusinessError
    require_scope(principal,SCOPE)
    q=body.selector
    grant=next((g for g in config.grants if g.project_id==body.project_id and g.service_user_id==str(principal.user_id)
        and g.tenant_id==principal.tenant_id and q.incident_id in g.incident_ids),None)
    if principal.principal_kind!="SERVICE" or grant is None: raise CIError("COVERAGE_SOURCE_DENIED",403)
    inputs=body.input.model_dump()
    if digest(inputs)!=body.frozen_input_digest: raise CIError("COVERAGE_INPUT_INVALID",422)
    async with service.database.session() as session:
        incident=await session.get(IncidentRow,q.incident_id)
        execution=await session.get(CIExecutionRow,q.execution_id)
        if (incident is None or execution is None or incident.execution_id!=execution.execution_id or execution.instance_id!=q.instance_id
                or incident.tenant_id!=grant.tenant_id or str(incident.owner_user_id)!=grant.owner_user_id):
            raise CIError("COVERAGE_SOURCE_DENIED",403)
        try: task,report,plan,test,source=await approved(session,principal,q.plan_id,q.test_id,trusted_owner=(grant.tenant_id,grant.owner_user_id))
        except BusinessError as exc: raise CIError(exc.code,exc.status) from None
        state=execution.execution_snapshot
        expected=build(task.snapshot,test,inputs["executor_ref"])
        source["asset_digest"]=expected["case_asset"]["asset_digest"]
        trigger=state.get("trigger",{});distribution=state.get("distribution",{})
        stable={k:v for k,v in trigger.items() if k not in {"triggered_at","intent_digest","derivation"}}
        if (q.task_id!=task.task_id or q.report_id!=report.report_id or task.snapshot!=inputs["task"] or task.exposed_at is None
                or state.get("workflow_source")!=source or state.get("case_asset")!=expected["case_asset"]
                or state.get("baseband")!=expected["baseband"] or execution.state_revision!=1
                or trigger.get("request_id")!=execution.request_id or trigger.get("case_id")!=execution.case_id
                or trigger.get("state_revision")!=execution.state_revision or trigger.get("actor_kind")!="HUMAN"
                or trigger.get("actor_user_id")!=grant.owner_user_id or trigger.get("tenant_id")!=grant.tenant_id
                or trigger.get("asset_digest")!=source["asset_digest"] or trigger.get("executor_ref")!=inputs["executor_ref"]
                or trigger.get("verification_ref") is not None or digest(stable)!=trigger.get("intent_digest")
                or distribution.get("mode")!="LOCAL_SIMULATED" or distribution.get("target_ref")!=inputs["executor_ref"]
                or distribution.get("actor_user_id")!=grant.owner_user_id or distribution.get("asset_digest")!=source["asset_digest"]
                or distribution.get("distribution_digest")!=digest({k:v for k,v in distribution.items() if k!="distribution_digest"})):
            raise CIError("COVERAGE_BINDING_INVALID",422)
        feature_source=None
        feature_model_calls=0
        if body.protocol=="coverage-execution-request.v2":
            from core.feature_impact.planning_quality import sealed
            from core.persistence.models import RuntimeModelInvocationRow
            from sqlalchemy import select,func
            if report.envelope["candidate_kind"]!="AGENT" or report.envelope["candidate_version"]!="1.2.0" or repository is None:
                raise CIError("COVERAGE_AGENT_SOURCE_INVALID",422)
            _,_,feature_source=await sealed(session,repository,principal,task.snapshot,dict(task_id=task.task_id,report_id=report.report_id,run_id=report.envelope["run_id"]),"AGENT","1.2.0")
            feature_model_calls=await session.scalar(select(func.count()).select_from(RuntimeModelInvocationRow).where(RuntimeModelInvocationRow.run_id==report.envelope["run_id"]))
        elif report.envelope["candidate_kind"] not in {"RULE","HUMAN"} or task.snapshot.get("analysis_contract")!="interface-impact.v1":
            raise CIError("COVERAGE_SOURCE_VERSION_INVALID",422)
        diagnosis=rule_report(incident.incident_id,execution)
        joint_source=dict(candidate_kind="RULE",incident_id=incident.incident_id,execution_id=execution.execution_id,
            input_digest=execution.summary["input_digest"],evidence_digest=execution.evidence_digest,agent_id=None,agent_version=None,
            investigation_id=None,run_id=None,session_id=None,final_status=None,raw_output_digest=None,model_calls=0,committed_tools=0,observed_batches=0,
            observed_reads=[],rule_version="baseband-rule.v2")
        correlations={k:getattr(body,k) for k in ("project_id","run_id","attempt_id","request_id","case_id")}
        joint=dict(protocol="baseband-quality-output.v2",**correlations,frozen_input_digest=execution.summary["input_digest"],input=state["baseband"],
            source=joint_source,report=diagnosis,report_digest=digest(diagnosis),evidence=execution.evidence_manifest)
        business=dict(task_id=task.task_id,task_snapshot=task.snapshot,exposed_at=task.exposed_at.isoformat(),report_envelope=report.envelope,
            report=report.payload,report_source=report.source_record,plan=plan.payload,review_request_id=plan.request_id,test=test,
            instance_id=q.instance_id,workflow_source=source,case_asset=state["case_asset"],recipe_digest=state["recipe_digest"],
            distribution=distribution,trigger=trigger,execution=dict(execution_id=execution.execution_id,instance_id=execution.instance_id,incident_id=incident.incident_id,
                request_id=execution.request_id,state_revision=execution.state_revision,outcome=execution.outcome,summary=execution.summary),joint=joint,
            model_calls=0,planning_kind=report.envelope["candidate_kind"],diagnosis_kind="RULE")
        if feature_source is not None:
            business.update(feature_agent_source=feature_source,model_calls=feature_model_calls)
        return dict(protocol="coverage-execution-output.v2" if feature_source is not None else "coverage-execution-output.v1",**correlations,input=inputs,frozen_input_digest=body.frozen_input_digest,
            business=business,business_digest=digest(business))
