"""公开Source值域向量；与Go共享文件，不连接PG或模型。"""
from copy import deepcopy
from pathlib import Path
from uuid import uuid4
import json
from core.ci_simulation.execution import initial_state,execute
from core.incidents.evaluation_sources import semantic_digest


def public_source_values():
    execution_id=str(uuid4())
    actual=execute(initial_state('report_pipeline_failure'),execution_id)
    entries={e['kind']:e['content'] for e in actual['evidence_manifest']}
    rows=[]
    for kind,changes,valid in [
        ('ENVIRONMENT_RECORD',{},True),
        ('ENVIRONMENT_RECORD',dict(parser_version=None,decoder_table_version=None,parser_supported_field_revision=None,decoder_supported_field_revision=None,missing_reason='工具安装与能力记录未采集'),True),
        ('ENVIRONMENT_RECORD',dict(missing_reason=None,feature_flags={}),True),
        ('ENVIRONMENT_RECORD',dict(software_version='unknown-product'),False),
        ('ENVIRONMENT_RECORD',dict(buffer_behavior='unapproved'),False),
        ('ENVIRONMENT_RECORD',dict(missing_reason='other'),False),
        ('ENVIRONMENT_RECORD',dict(environment_id='bad id'),False),
        ('ENVIRONMENT_RECORD',dict(parser_version='解析器'),False),
        ('ENVIRONMENT_RECORD',dict(ip='192.0.2.not-an-ip'),False),
        ('CASE_REQUIREMENTS',{},True),
        ('CASE_REQUIREMENTS',dict(input_contract='tlv',buffer_requirement='legacy',compressed_transport=False),True),
        ('CASE_REQUIREMENTS',dict(input_contract=None,buffer_requirement=None,compressed_transport=None),True),
        ('CASE_REQUIREMENTS',dict(comparison_rule_id='unapproved'),False),
        ('CASE_REQUIREMENTS',dict(input_contract='shell'),False),
        ('CASE_REQUIREMENTS',dict(buffer_requirement='unknown'),False),
        ('CASE_REQUIREMENTS',dict(golden_version='bad version'),False),
        ('CASE_REQUIREMENTS',dict(case_id='x'*129),False),
        ('CASE_REQUIREMENTS',dict(input_revision=True),False),
    ]:
        content={**deepcopy(entries[kind]),**changes}
        try:digest=semantic_digest(kind,content,execution_id);accepted=True
        except ValueError:digest=None;accepted=False
        assert accepted is valid,(kind,changes)
        rows.append(dict(kind=kind,execution_id=execution_id,content=content,accepted=valid,semantic_digest=digest))
    return dict(schema_version='public-ci-source-values.v1',rows=rows)


def test_python_source_value_contract():
    path=Path(__file__).resolve().parents[2]/'AgentEvalOps/go-backend/internal/agentquality/testdata/ci_source_values.json'
    for row in json.loads(path.read_text(encoding='utf-8'))['rows']:
        try:actual=semantic_digest(row['kind'],row['content'],row['execution_id']);accepted=True
        except ValueError:actual=None;accepted=False
        assert accepted is row['accepted']
        assert actual==row['semantic_digest']
