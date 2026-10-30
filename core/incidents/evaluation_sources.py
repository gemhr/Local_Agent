"""CI Owner 的本次持久来源导出；不读取或生成 EO Ground Truth。"""
import json
from copy import deepcopy
from core.runtime.agent_loop.quality_contract import catalog_digest, canonical_bytes
from core.runtime.execution_aggregate import canonical_payload_digest as ref_digest
from core.runtime.tool_contract import canonical_json_digest, thaw_json
from core.runtime.snapshot_serialization import sha256_digest
from core.agent_platform.ci_diagnosis_tools import TOOL_NAMES
from typing import Literal
from core.incidents.evaluation_input import Strict, Environment, TestCase, Revision, ID

SEMANTICS = 'ci-diagnosis-source-semantics.v1'
KINDS = ('PARSER_LOG','ENVIRONMENT_RECORD','CASE_REQUIREMENTS','COMPARISON_LOG','AUTOMATION_LOG')

class ParserSource(Strict):
    field_revision: Revision
    parser_supported_field_revision: Revision
    decoder_supported_field_revision: Revision
    parser_version: ID
    decoder_table_version: ID
    status: Literal['EXECUTED','SKIPPED']
    skipped_reason: str | None
    error: str | None
    parsed_fields: dict[Literal['report'], Literal['decoded']] | None

class EnvironmentSource(Environment):
    parser_version: ID | None
    decoder_table_version: ID | None
    parser_supported_field_revision: Revision | None
    decoder_supported_field_revision: Revision | None
    missing_reason: Literal['工具安装与能力记录未采集'] | None = None

class ComparisonSource(Strict):
    caller: Literal['COMPARE']
    callee: Literal['PARSER']
    golden_comparison_performed: bool
    outcome: Literal['PASS','FAIL']
    error: str | None
    skipped_reason: str | None

class LimitedComparisonSource(Strict):
    caller: Literal['COMPARE']
    outcome: Literal['PASS','FAIL']
    detail: Literal['比较阶段未获得可用结果；调用详细记录未采集']

class StageSource(Strict):
    stage: Literal['PRECHECK','PRODUCT','PARSER','COMPARISON']
    status: Literal['EXECUTED','SKIPPED']
    error: str | None

class ReleaseSource(Strict):
    required_tlv_revision: Revision
    buffer_behavior: Literal['compatible','compact','legacy']

class AutomationSource(Strict):
    caller: Literal['AUTOMATION']
    callee: Literal['COMPARE']
    outcome: Literal['PASS','FAIL']
    stages: list[StageSource]
    container_type: Literal['simulated-compressed-report']
    product_release: ReleaseSource

def semantic_digest(kind, content, execution_id):
    if kind not in KINDS or not isinstance(content,dict):
        raise ValueError('CI_SOURCE_KIND_INVALID')
    expected = dict(execution_id=execution_id,artifact_id=execution_id+':report',call_id=execution_id+':compare')
    if any(content.get(k)!=v for k,v in expected.items()):
        raise ValueError('CI_SOURCE_ANCHOR_INVALID')
    body={k:v for k,v in content.items() if k not in expected}
    model={'PARSER_LOG':ParserSource,'ENVIRONMENT_RECORD':EnvironmentSource,'CASE_REQUIREMENTS':TestCase,
        'COMPARISON_LOG':LimitedComparisonSource if 'detail' in body else ComparisonSource,
        'AUTOMATION_LOG':AutomationSource}[kind]
    model.model_validate(body)
    if kind=='PARSER_LOG' and body['parsed_fields'] not in (None,{'report':'decoded'}):
        raise ValueError('CI_SOURCE_PARSER_FIELDS_INVALID')
    if kind=='AUTOMATION_LOG' and [s['stage'] for s in body['stages']]!=['PRECHECK','PRODUCT','PARSER','COMPARISON']:
        raise ValueError('CI_SOURCE_STAGES_INVALID')
    # 仅校验，不用Pydantic重编码或删除null/默认值。
    return catalog_digest(body)

def build_source_binding(binding, execution, image, actions):
    """调用者已按Binding原Scope读取Owner记录；首次Sidecar封存事务使用。"""
    frozen=binding['frozen_ci']; visible=frozen['visible_input']; state=deepcopy(visible['initial_state'])
    state['report']['report_artifact_id']=execution.execution_id+':report'
    if (binding['run_id']!=image.root.run_id or binding['execution_id']!=execution.execution_id
            or state!=execution.execution_snapshot or execution.state_revision!=1):
        raise ValueError('CI_SOURCE_SNAPSHOT_INVALID')
    if sha256_digest(sorted(execution.evidence_manifest,key=lambda e:e['evidence_id']))!=execution.evidence_digest:
        raise ValueError('CI_SOURCE_MANIFEST_INVALID')
    for entry in execution.evidence_manifest:
        if entry['kind'] in KINDS and entry['availability']=='AVAILABLE':
            semantic_digest(entry['kind'],entry['content'],execution.execution_id)
    invocations={t.invocation_id:t for t in image.tool_invocations}
    exported=[]
    for action in actions:
        tool=invocations.get(action.invocation_id)
        if tool is None or tool.tool_name not in TOOL_NAMES or tool.state!='COMMITTED':
            continue
        result=tool.committed_result
        if (not isinstance(result,dict) or canonical_json_digest(result)!=tool.committed_result_digest
                or action.source_digest!=tool.committed_result_digest or action.result_projection is None
                or ref_digest(action.result_projection)!=action.result_digest):
            raise ValueError('CI_SOURCE_TOOL_BINDING_INVALID')
        raw=json.dumps(thaw_json(result),ensure_ascii=True,sort_keys=True,separators=(',',':'),allow_nan=False)
        content=(result.get('output') or {}).get('content')
        if not isinstance(content,str) or len(content.encode('utf-8'))>16384 or len(raw.encode('utf-8'))>131072:
            raise ValueError('CI_SOURCE_TOOL_LIMIT')
        recipe=action.recipe or {}
        if (action.run_id!=binding['run_id'] or tool.run_id!=binding['run_id']
                or ref_digest(recipe)!=action.recipe_digest or recipe.get('tool_name')!=tool.tool_name
                or canonical_json_digest(recipe.get('arguments'))!=tool.arguments_digest):
            raise ValueError('CI_SOURCE_ARGUMENTS_INVALID')
        exported.append(dict(invocation_ref=ref_digest(tool.invocation_id),action_ref=ref_digest(action.action_id),
            frame_ref=ref_digest(action.frame_id),iteration=action.iteration_no,ordinal=action.ordinal,
            call_ref=ref_digest(action.protocol_call_id),tool_name=tool.tool_name,arguments=recipe['arguments'],
            arguments_digest=tool.arguments_digest,result_canonical_bytes=raw,result_digest=tool.committed_result_digest))
    if not exported or len(exported)>8:
        raise ValueError('CI_SOURCE_TOOL_LIMIT')
    result=dict(schema_version='ci-runtime-source-binding.v1',source_semantics_version=SEMANTICS,
        quality_correlation=binding['quality_correlation'],plan_id=frozen['plan_id'],plan_version=frozen['plan_version'],
        plan_digest=binding['plan_digest'],mode=binding['mode'],input_digest=frozen['input_digest'],
        state_digest=visible['state_digest'],source_ref=frozen['source_ref'],state_revision=execution.state_revision,
        execution_snapshot=execution.execution_snapshot,summary=execution.summary,
        evidence_manifest=execution.evidence_manifest,evidence_digest=execution.evidence_digest,tools=exported)
    if len(canonical_bytes(result))>768*1024:
        raise ValueError('CI_SOURCE_BINDING_LIMIT')
    return result
