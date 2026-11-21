"""独立规划评价只读封存来源；共同输入与候选选择信息分开。"""
from typing import Literal
from uuid import UUID
from sqlalchemy import select
from core.feature_impact.reports import Strict
from core.feature_impact.models import TaskRow, ReportRow, InvestigationRow
from core.feature_impact.coverage_analysis import CONTRACT, METHOD, RULE_VERSION
from core.feature_impact.knowledge import digest, read
from core.feature_impact.service import BusinessError


class Input(Strict):
    schema_version: Literal["feature-impact-input.v2"]
    snapshot: dict


class Selector(Strict):
    task_id: str
    report_id: str
    run_id: str | None


class Request(Strict):
    protocol: Literal["feature-impact-request.v2"]
    project_id: UUID
    run_id: UUID
    attempt_id: UUID
    request_id: UUID
    case_id: str
    frozen_input_digest: str
    input: Input
    candidate_kind: Literal["RULE", "AGENT"]
    candidate_version: str
    selector: Selector


async def sealed(session, repository, principal, snapshot, selector, kind, version):
    """调用者不能用报告正文或自报模型回执代替原库事实。"""
    from core.feature_impact.reports import rule_report
    from core.feature_impact.agent_reports import require_binding_owner, validated_agent_report
    from core.persistence.models import RuntimeLoopActionRow
    if (snapshot.get("analysis_contract")!=CONTRACT
            or digest({k:v for k,v in snapshot.items() if k!="input_digest"})!=snapshot.get("input_digest")
            or digest(snapshot["manifest"])!=snapshot["corpus_digest"]):
        raise BusinessError("FROZEN_INPUT_DIGEST_INVALID",422)
    for doc in snapshot["documents"]: read(snapshot,doc["source_ref"])
    task=await session.get(TaskRow,selector["task_id"])
    row=await session.get(ReportRow,selector["report_id"])
    if (principal.principal_kind!="SERVICE" or task is None or row is None or task.tenant_id!=principal.tenant_id
            or row.task_id!=task.task_id or task.snapshot!=snapshot
            or row.envelope.get("candidate_kind")!=kind or row.envelope.get("candidate_version")!=version
            or row.envelope.get("run_id")!=selector["run_id"]
            or row.envelope.get("input_digest")!=snapshot["input_digest"]
            or row.envelope.get("corpus_digest")!=snapshot["corpus_digest"]
            or row.envelope.get("task_version")!=snapshot["task_version"]):
        raise BusinessError("PLANNING_REPORT_BINDING_INVALID",422)
    if kind=="RULE":
        if row.rejection:raise BusinessError("PLANNING_REPORT_BINDING_INVALID",422)
        if version!=RULE_VERSION or selector["run_id"] is not None: raise BusinessError("CANDIDATE_VERSION_INVALID",422)
        report,source=rule_report(task.snapshot)
    else:
        if version!=METHOD: raise BusinessError("CANDIDATE_VERSION_INVALID",422)
        binding=await session.get(InvestigationRow,selector["run_id"])
        if binding is None or binding.task_id!=task.task_id: raise BusinessError("AGENT_REPORT_BINDING_INVALID",422)
        await require_binding_owner(session,binding,task)
        image=await repository.load(binding.run_id,session=session)
        if image is None: raise BusinessError("AGENT_EXECUTION_EVIDENCE_INVALID",422)
        actions=(await session.scalars(select(RuntimeLoopActionRow).where(RuntimeLoopActionRow.run_id==binding.run_id))).all()
        if row.rejection:
            from core.feature_impact.agent_reports import authenticated_rejected_planning
            raw,report,source=authenticated_rejected_planning(binding,image,actions,task)
            if raw!=row.raw_output or digest(raw)!=row.envelope["report_digest"]:
                raise BusinessError("AGENT_SEALED_REPORT_CHANGED",422)
            source["rejected_report"]=dict(status="REJECTED",rejection=row.rejection,sealed_raw_digest=row.envelope["report_digest"])
            return row,report,{**source,"report_id":row.report_id}
        raw,report,source=validated_agent_report(binding,image,actions,task)
        if raw!=row.raw_output: raise BusinessError("AGENT_SEALED_REPORT_CHANGED",422)
    if report!=row.payload or source!=row.source_record or digest(report)!=row.envelope["report_digest"]:
        raise BusinessError("PLANNING_SEALED_REPORT_CHANGED",422)
    return row,report,{**source,"report_id":row.report_id}


async def evaluate(service,repository,principal,body):
    from core.auth import require_scope
    require_scope(principal,"localagent:feature-impact:evaluate")
    if digest(body["input"])!=body["frozen_input_digest"]: raise BusinessError("FROZEN_INPUT_DIGEST_INVALID",422)
    async with service.database.session() as session:
        row,report,source=await sealed(session,repository,principal,body["input"]["snapshot"],body["selector"],body["candidate_kind"],body["candidate_version"])
        envelope={k:row.envelope[k] for k in ("candidate_kind","candidate_version","run_id","task_id","task_version","input_digest","corpus_digest","report_digest")}
        if row.rejection: envelope["report_digest"]=digest(report)
        return dict(protocol="feature-impact-output.v2",**{k:body[k] for k in ("project_id","run_id","attempt_id","request_id","case_id","frozen_input_digest")},envelope=envelope,report=report,source_record=source)
