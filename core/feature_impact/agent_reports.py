"""从 AgentCore 持久化事实导出 AGENT 报告，不接受远端 receipt 或报告正文。"""
from sqlalchemy import select
from core.persistence.models import ObjectOwnershipRow, RuntimeLoopActionRow
from core.feature_impact.models import TaskRow, ReportRow, InvestigationRow
from core.feature_impact.knowledge import digest, read
from core.feature_impact.reports import validate
from core.feature_impact.service import BusinessError
from core.runtime.memory_scope import MemoryAccessContext
from core.runtime.snapshot_serialization import text_digest
from core.agent_platform.ci_diagnosis_tools import strict_object


async def require_binding_owner(session, binding, task):
    from core.feature_impact.tools import AGENT_ID
    access = MemoryAccessContext(task.tenant_id, str(task.owner_user_id), AGENT_ID, binding.session_id)
    for kind, identity in (('RUN', binding.run_id), ('CONVERSATION', access.conversation_id)):
        owner = await session.get(ObjectOwnershipRow, (kind, identity))
        if owner is None or owner.tenant_id != task.tenant_id or owner.owner_user_id != task.owner_user_id:
            raise ValueError('AGENT_RUN_OWNER_INVALID')


def validated_agent_report(binding, image, actions, task):
    from core.feature_impact.agent import validate_result
    from core.feature_impact.tools import AGENT_ID
    raw, source = validate_result(binding, image, actions, task.snapshot)
    report = validate(strict_object(raw), task.snapshot, kind='AGENT', reads=source['read_sources'],
                      task_inputs=source['task_input_sources'])
    source.update(protocol='feature-impact-agent-source.v1', task_id=task.task_id,
        task_version=task.snapshot['task_version'], input_digest=task.snapshot['input_digest'],
        corpus_digest=task.snapshot['corpus_digest'], session_id=binding.session_id,
        owner_user_id=str(task.owner_user_id), tenant_id=task.tenant_id, agent_id=AGENT_ID, agent_version='1.1.0',
        final_status='DELIVERED', raw_output=raw, raw_output_digest=text_digest(raw), report_digest=digest(report))
    return raw, report, source


async def evaluate_agent_report(service, repository, principal, body):
    """SERVICE 只能按同 tenant 的确切 report/run 引用重验，不得枚举或提交可信来源。"""
    from core.auth import require_scope
    require_scope(principal, 'localagent:feature-impact:evaluate')
    value = body['input']
    selector = value.get('report')
    if (value['candidate_kind'] != 'AGENT' or value['candidate_version'] != '1.1.0'
            or not isinstance(selector, dict) or set(selector) != {'report_id', 'run_id'}
            or any(not isinstance(v, str) or not v for v in selector.values())
            or digest(value) != body['frozen_input_digest']):
        raise BusinessError('AGENT_REPORT_SELECTOR_INVALID', 422)
    snapshot = value['snapshot']
    if (digest({k:v for k,v in snapshot.items() if k != 'input_digest'}) != snapshot['input_digest']
            or digest(snapshot['manifest']) != snapshot['corpus_digest']):
        raise BusinessError('FROZEN_INPUT_DIGEST_INVALID', 422)
    for doc in snapshot['documents']:
        read(snapshot, doc['source_ref'])
    async with service.database.session() as session:
        row = await session.get(ReportRow, selector['report_id'])
        task = await session.get(TaskRow, value['task_id'])
        binding = await session.get(InvestigationRow, selector['run_id'])
        if (row is None or task is None or binding is None or task.tenant_id != principal.tenant_id
                or row.task_id != task.task_id or binding.task_id != task.task_id or row.rejection
                or row.envelope.get('candidate_kind') != 'AGENT' or row.envelope.get('run_id') != binding.run_id
                or row.envelope.get('candidate_version') != value['candidate_version'] or task.snapshot != snapshot):
            raise BusinessError('AGENT_REPORT_BINDING_INVALID', 422)
        try:
            await require_binding_owner(session, binding, task)
            image = await repository.load(binding.run_id, session=session)
            if image is None:
                raise ValueError('AGENT_EXECUTION_MISSING')
            actions = (await session.execute(select(RuntimeLoopActionRow).where(RuntimeLoopActionRow.run_id == binding.run_id))).scalars().all()
            raw, report, source = validated_agent_report(binding, image, actions, task)
            if (raw != row.raw_output or report != row.payload or source != row.source_record
                    or digest(report) != row.envelope['report_digest']
                    or row.envelope['input_digest'] != snapshot['input_digest']
                    or row.envelope['corpus_digest'] != snapshot['corpus_digest']
                    or row.envelope['task_version'] != snapshot['task_version']):
                raise ValueError('AGENT_SEALED_REPORT_CHANGED')
        except (ValueError, TypeError, KeyError, AttributeError):
            raise BusinessError('AGENT_EXECUTION_EVIDENCE_INVALID', 422) from None
        # 不增删任何 finding 或测试。来源只增加当前确切封存报告 identity。
        source = {**source, 'report_id': row.report_id}
        envelope = {k: row.envelope[k] for k in ('candidate_kind','candidate_version','run_id','task_id',
            'task_version','input_digest','corpus_digest','report_digest')}
        return dict(protocol='feature-impact-output.v1', **{k:body[k] for k in ('project_id','run_id','attempt_id','request_id','case_id','frozen_input_digest')},
                    envelope=envelope, report=report, source_record=source)
