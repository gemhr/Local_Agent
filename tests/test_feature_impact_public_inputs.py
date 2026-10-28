"""公开实验输入/规则/因果的小范围验收，不读取独立评分参考。"""
from copy import deepcopy
import json
from pathlib import Path

from core.feature_impact.knowledge import digest, read, search
from core.feature_impact.quality import evaluate
from core.feature_impact.resource_simulation import execute

FIXTURES = Path(__file__).parent / 'fixtures/feature_impact_public'


def inputs():
    return {code: json.loads((FIXTURES / f'{code}.json').read_text(encoding='utf-8')) for code in ('T2', 'T3', 'T5', 'T6')}


def test_public_inputs_freeze_all_visible_sources_without_answers():
    forbidden = {'coverage_truth', 'ground_truth', 'support_sources', 'counter_sources', 'class', 'verdict', 'internal_trace'}
    def keys(value):
        if isinstance(value, dict):
            return set(value).union(*(keys(v) for v in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(v) for v in value))
        return set()
    digests = set()
    for value in inputs().values():
        task = value['snapshot']
        assert not forbidden.intersection(keys(task))
        assert digest({k:v for k,v in task.items() if k != 'input_digest'}) == task['input_digest']
        assert digest(task['manifest']) == task['corpus_digest']
        assert len(search(task, '')) == len(task['documents']) <= 20
        for doc in task['documents']:
            assert read(task, doc['source_ref']) == doc
        digests.add(task['input_digest'])
    assert len(digests) == 4


def test_frozen_rule_retains_indirect_terms_uncertainty_and_reuse():
    expected = {'T2': [('MUST_TEST','PARTIAL'),('REASONABLE_CHECK','NONE')],
                'T3': [('MUST_TEST','PARTIAL'),('REASONABLE_CHECK','UNKNOWN')],
                'T5': [('INSUFFICIENT_EVIDENCE','UNKNOWN'),('INSUFFICIENT_EVIDENCE','UNKNOWN')],
                'T6': [('MUST_TEST','FULL'),('MUST_TEST','PARTIAL')]}
    for code, value in inputs().items():
        before = deepcopy(value)
        body = dict(input=value, project_id='public', run_id='transport', attempt_id='attempt', request_id='request', case_id=code, frozen_input_digest=digest(value))
        output = evaluate(body)
        findings = output['report']['findings']
        assert [(f['coverage_class'],f['existing_coverage']) for f in findings] == expected[code]
        assert value == before
        assert output['source_record']['rule_version'] == 'dependency-lifecycle.v1'
        assert set(output['source_record']['read_sources']) == {d['source_ref'] for d in value['snapshot']['documents']}
        if code == 'T5':
            assert all(f['uncertainties'] and f['clarification_questions'] and not f['recommended_tests'] for f in findings)
        if code == 'T6':
            assert not findings[0]['recommended_tests']
            assert '复用' in findings[0]['rationale']
            assert '观察点' in findings[1]['recommended_tests'][0]['coverage_gap']


def test_public_case_sequences_follow_existing_causal_model():
    # 仅验证公开已有用例的因果，不把运行结果传给规则或 Go 评分。
    tasks = inputs()
    summary = next(d for d in tasks['T6']['snapshot']['documents'] if d['source_ref'] == 'T6:case-summary-reuse:v1')
    full = summary['facts']['operations']
    assert execute('NEW', full)[0]['verdict'] == 'FAIL'
    assert execute('OLD', full)[0]['verdict'] == 'PASS'
    copied = deepcopy(full)
    copied[1]['mode'] = 'COPY'
    assert execute('NEW', copied)[0]['verdict'] == 'PASS'
    early = next(d for d in tasks['T2']['snapshot']['documents'] if d['kind'] == 'CASE')['facts']['operations']
    assert execute('NEW', early)[0]['verdict'] == 'PASS'
