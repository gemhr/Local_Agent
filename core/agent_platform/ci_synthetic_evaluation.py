"""独立 CI 合成评测适配；旧 Quality v1 Wire 不变。"""
import json
from pathlib import Path
from uuid import UUID
from pydantic import BaseModel, ConfigDict, StrictStr
from fastapi import Request, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from core.auth import EVALUATION_EXECUTE_SCOPE, CI_SYNTHETIC_EVALUATE_SCOPE, require_scope
from core.agent_platform.application import ExecutionRequest, AgentApplicationError
from core.agent_platform.quality_configuration import load_configuration, check_agent
from core.incidents.evaluation import frozen_input, prepare
from core.incidents.models import SyntheticEvaluationBindingRow as Binding, InvestigationRow
from core.runtime.agent_loop.quality_contract import QualityEvaluationRequest, canonical_bytes, catalog_digest, parse_quality_json
from core.runtime.agent_loop.contracts import LoopSuspended
from core.runtime.memory_scope import MemoryAccessContext
from core.runtime.admission import RuntimeAdmissionRejectedError

PROTOCOL = 'ci-synthetic-evaluation-execute.v1'
QUERY = '请调查固定的公开合成 CI 失败，使用授权工具取证。'


class FrozenCI(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    case_id: StrictStr
    case_version: StrictStr
    case_digest: StrictStr
    template_id: StrictStr
    template_version: StrictStr
    input_digest: StrictStr


class CIRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    contract_version: StrictStr
    quality: QualityEvaluationRequest
    frozen_ci: FrozenCI
    candidate_manifest_digest: StrictStr
    mode: StrictStr


def validate_request(payload, settings, principal, definition, selection):
    require_scope(principal,EVALUATION_EXECUTE_SCOPE)
    require_scope(principal,CI_SYNTHETIC_EVALUATE_SCOPE)
    config=parse_quality_json(Path(settings.quality_evaluation_configuration_path).read_bytes())['ci_synthetic']
    if set(config) - {'tenant_id','frozen_ci','attempts','request_counting_exception'} or not {'tenant_id','frozen_ci','attempts'} <= set(config):
        raise ValueError('CI_SYNTHETIC_CONFIGURATION_INVALID')
    q=payload.quality
    pin=definition.business_options.get('ci_prompt_selection',{})
    frozen=payload.frozen_ci.model_dump()
    if (payload.contract_version!=PROTOCOL or principal.principal_kind!='SERVICE'
            or str(principal.user_id)!=settings.quality_evaluation_service_principal_id
            or principal.tenant_id!=config['tenant_id']
            or str(q.evaluation_project_id)!=settings.quality_evaluation_project_id
            or q.agent_id!='ci_incident_investigator' or q.run_id!=q.attempt_id or q.query!=QUERY
            or frozen!=config['frozen_ci'] or any(frozen[k]!=v for k,v in frozen_input().items())
            or (frozen['case_id'],frozen['case_version'],frozen['case_digest'])!=(str(q.case_id),q.case_version,q.case_digest)
            or payload.mode not in {'BASELINE','CANDIDATE'} or pin.get('mode')!=payload.mode
            or pin.get('manifest_digest')!=payload.candidate_manifest_digest
            or config['attempts'].get(payload.mode)!=str(q.attempt_id)):
        raise ValueError('CI_SYNTHETIC_PIN_INVALID')
    from core.runtime.request_counting_admission import match_request
    if config.get('request_counting_exception') is not None and selection is None:
        raise ValueError('E2E_ADMISSION_STARTUP_CONFIGURATION_REQUIRED')
    if selection is not None:
        from core.runtime.request_counting_admission import compile_exception
        if compile_exception(config.get('request_counting_exception')) != selection:
            raise ValueError('E2E_ADMISSION_CONFIGURATION_CHANGED')
    return match_request(selection,payload,principal,definition)


def register(app, *, settings, trusted, repository, wire, require_service, bind_run, authorize_existing, final_answer_capture):
    async def finish(request,run):
        principal=trusted(request)
        require_scope(principal,CI_SYNTHETIC_EVALUATE_SCOPE)
        receipts,execution=repository(request)
        async with execution.database.session() as session:
            binding=await session.get(Binding,run)
            if (binding is None or binding.tenant_id!=principal.tenant_id or binding.principal_id!=principal.user_id
                    or binding.project_id!=settings.quality_evaluation_project_id):
                raise HTTPException(404,'CI_SYNTHETIC_BINDING_MISSING')
            payload=binding.payload
            sealed=binding.sidecar
        receipt=await receipts.read(run)
        await authorize_existing(request,execution,receipt)
        if sealed:
            return wire(sealed)
        investigator=request.app.state.ci_investigation_service
        await investigator.project_if_terminal(payload['investigation_id'])
        quality=receipt.sealed_response or await receipts.capture(execution,run)
        if quality is None:
            return wire(canonical_bytes({'contract_version':PROTOCOL,'run_id':run,'status':'PENDING','reconciliation':'QUERY_ONLY_NO_REEXECUTE'}),202)
        receipt=await receipts.read(run)
        image=await execution.load(run)
        if (image is None or image.root.run_id!=run or image.root.resume_input.get('session_id')!=payload['session_id']
                or image.root.resume_input.get('resolved_agent_id')!=payload['agent_id']
                or image.root.resume_input.get('agent_version')!=payload['agent_version']):
            raise HTTPException(409,'CI_SYNTHETIC_ROOT_BINDING_INVALID')
        models=[]
        for row in image.models:
            key=f'model:{row.step_id}:{row.attempt_number}:{row.model_attempt_number}'
            proof=receipt.evaluation_capture.get(key,{}).get('ci_prompt_binding')
            if proof is not None and proof['model_request_digest']==row.request_digest:
                models.append(dict(model_ref=key,frame_ref=row.loop_frame_id,run_id=run,
                    model_state=row.state,proof=proof))
        async with execution.database.transaction() as session:
            investigation=await session.get(InvestigationRow,payload['investigation_id'])
            b=await session.get(Binding,run,with_for_update=True)
            if b.sidecar is None:
                sidecar=dict(schema_version='ci-synthetic-evidence.v1',binding=payload,models=models,
                    quality_digest=receipt.sealed_digest,report_digest=investigation.runtime_result_digest,
                    runtime_status=image.root.status)
                b.sidecar=canonical_bytes(dict(contract_version=PROTOCOL,quality=json.loads(quality),
                    sidecar=sidecar,sidecar_digest=catalog_digest(sidecar))).decode('utf-8')
            return wire(b.sidecar)

    @app.post('/api/runtime/evaluation-execute/ci-synthetic/v1')
    async def execute(payload:CIRequest,request:Request):
        q=payload.quality
        principal=trusted(request,str(q.evaluation_project_id))
        require_scope(principal,CI_SYNTHETIC_EVALUATE_SCOPE)
        raw=await request.body()
        if len(raw)>512*1024:
            raise HTTPException(413,'CI_SYNTHETIC_REQUEST_LIMIT')
        service=require_service()
        try:
            parse_quality_json(raw)
            manifest,profile=load_configuration(settings.quality_evaluation_configuration_path)
            probe=ExecutionRequest(agent_id=q.agent_id,input=QUERY)
            _,definition,_,_,_=service._resolve_request(probe)
            pin=check_agent(manifest,definition)
            exception=getattr(request.app.state,'request_counting_exception',None)
            admission=validate_request(payload,settings,principal,definition,exception)
            if (pin['execution_policy']!='durable-agent-loop.v2' or q.execution_manifest_digest!=catalog_digest(manifest)
                    or (q.capture_profile_id,q.capture_profile_version,q.capture_profile_digest)!=(profile['id'],profile['version'],catalog_digest(profile))):
                raise ValueError('CI_SYNTHETIC_QUALITY_PIN_INVALID')
        except (ValueError,KeyError,OSError,AgentApplicationError):
            raise HTTPException(422,'CI_SYNTHETIC_CONFIGURATION_OR_PIN_INVALID') from None
        supervisor=getattr(request.app.state,'run_execution_supervisor',None)
        if supervisor is None:
            raise HTTPException(503,'RUNTIME_NOT_READY')
        reserved=await supervisor.acquire_slot()
        try:
            receipts,execution=repository(request)
            try:
                with service._chat_service.admission_gate.admission():
                    winner,binding=await prepare(request.app.state.incident_service,principal,payload,definition,manifest,profile,
                        request_counting_exception=admission)
            except RuntimeAdmissionRejectedError:
                raise HTTPException(503,'RUNTIME_SHUTTING_DOWN') from None
            except ValueError:
                raise HTTPException(409,'CI_SYNTHETIC_PREPARE_CONFLICT') from None
            if not winner:
                return await finish(request,str(q.run_id))
            await bind_run(request,run_id=str(q.run_id),agent_id=q.agent_id,session_id=binding['session_id'])
            access=MemoryAccessContext.from_principal(principal,agent_id=q.agent_id,session_id=binding['session_id'])
            execution_request=ExecutionRequest(agent_id=q.agent_id,input=QUERY,run_id=str(q.run_id),
                session_id=binding['session_id'],timeout_seconds=q.timeout_milliseconds/1000)
            try:
                result=await service.execute_evaluation(execution_request,execution_policy=pin['execution_policy'],
                    loop_correlation={'evaluation_run_id':str(q.evaluation_run_id),'attempt_id':str(q.attempt_id),'request_id':str(q.request_id)},
                    memory_access_context=access)
            except LoopSuspended:
                return await finish(request,str(q.run_id))
            status,_,answer=final_answer_capture(output=result.output,result=result)
            await receipts.capture(execution,str(q.run_id),final_answer=answer if status=='COMPLETE' else None)
            return await finish(request,str(q.run_id))

        finally:
            supervisor.release_slot(reserved)

    @app.get('/api/runtime/runs/{run_id}/ci-synthetic-evidence/v1')
    async def read(run_id:str,request:Request):
        try: UUID(run_id)
        except ValueError: raise HTTPException(422,'INVALID_RUN_ID') from None
        return await finish(request,run_id)
