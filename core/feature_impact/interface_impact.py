"""接口值域变更的事前覆盖规划，不读取执行或评价结果。"""
from core.feature_impact.knowledge import document, digest, read
from core.feature_impact.reports import Finding as BaseFinding, Reference, Strict
from core.ci_simulation.daas_assets import Test, validate_test, catalog, definition
from pydantic import Field
from typing import Literal

RULE_VERSION="interface-coverage.v1"


class Finding(BaseFinding):
    recommended_tests: list[Test]


class Report(Strict):
    schema_version: Literal["feature-impact-report.v2"]
    findings: list[Finding] = Field(min_length=1,max_length=30)


def development_input():
    ref=lambda name,v:dict(id=name,version=str(v))
    c=dict(features=[],interfaces=[],tags=[],messages=[],testcases=[])
    for v in (1,2):
        f,i,t,m,case=(ref(n,v) for n in ("sampling-control","l2-management","sampling-gain","sample-config","sampling-case"))
        c["features"].append(dict(ref=f,interface_ref=i))
        c["interfaces"].append(dict(ref=i))
        c["tags"].append(dict(ref=t,interface_ref=i,type="INTEGER",number=17,length=1,allowed_values=[1] if v==1 else [1,2]))
        c["messages"].append(dict(ref=m,interface_ref=i,tags=[t],required_tags=[t]))
        c["testcases"].append(dict(ref=case,feature_ref=f,message_ref=m))
    docs=[document("interface:catalog", "INTERFACE_CATALOG","合成采样接口版本定义","公开有限合成定义，不是真实 DAAS 协议或入库接口。",dict(catalog=c)),
        document("interface:change", "DESIGN","采样增益值域扩展","新 Tag/消息支持 gain=1/2；旧版仅 gain=1。需要补充新增值的配置应用及输出观察。",dict(old_tag=ref("sampling-gain",1),new_tag=ref("sampling-gain",2),target_feature=ref("sampling-control",2))),
        document("interface:history", "CASE","旧采样用例覆盖","旧消息和用例只验证 gain=1，不构成新增值的覆盖证明。",dict(testcase_ref=ref("sampling-case",1),message_ref=ref("sample-config",1),tested_gains=[1]))]
    manifest=[{k:d[k] for k in ("source_ref","version","chunk","content_digest")} for d in docs]
    task=dict(analysis_contract="interface-impact.v1",task_version=1,title="合成采样接口新增值域覆盖",feature_id="sampling-control",
        planning_cutoff="2026-10-02T00:00:00Z",known_impacts=[],documents=docs,manifest=manifest,corpus_digest=digest(manifest),development_exposed=True)
    task["input_digest"]=digest(task)
    return task


def rule_report(task):
    docs=[read(task,d["source_ref"]) for d in task["documents"]]
    c=catalog(task);change=next(d["facts"] for d in docs if d["kind"]=="DESIGN")
    old=definition(c,"tags",change["old_tag"]);new=definition(c,"tags",change["new_tag"])
    feature=definition(c,"features",change["target_feature"])
    message=next(m for m in c["messages"] if m["interface_ref"]==feature["interface_ref"] and m["tags"]==[new["ref"]])
    case=next(t for t in c["testcases"] if t["feature_ref"]==feature["ref"] and t["message_ref"]==message["ref"])
    history=[d for d in docs if d["kind"]=="CASE"]
    tested={g for d in history for g in d["facts"]["tested_gains"]}
    expanded=set(new["allowed_values"])-set(old["allowed_values"])
    missing=sorted(expanded-tested)
    suggestions=[]
    for gain in missing:
        suggestions.append(dict(test_kind="DAAS_CASE",test_id="gain-coverage-"+str(gain),preconditions="选择获准的新版本配置",observation="采样实际应用状态与输出比较",
            expected="请求配置在采样前生效，输出符合适用参考",historical_cases=[d["source_ref"] for d in history],coverage_gap="旧用例未覆盖新增合法值",
            feature_ref=feature["ref"],interface_ref=feature["interface_ref"],tag_ref=new["ref"],message_ref=message["ref"],testcase_ref=case["ref"],
            case_configuration=dict(gain=gain,sample_rate=30,stimulus=7,software_version="baseband-2.0",environment_condition="nominal",reference_version="2")))
    refs=[dict(**{k:d[k] for k in ("source_ref","version","chunk","content_digest")},basis="EVIDENCE_READ") for d in docs]
    report=dict(schema_version="feature-impact-report.v2",findings=[dict(finding_id="sampling-range",historical_feature_id=feature["ref"]["id"],
        impact_mechanism="INTERFACE_VALUE_RANGE_EXPANSION",trigger_conditions=["MESSAGE_REFERENCES_NEW_TAG","GAIN_RANGE_EXPANDED"],coverage_class="MUST_TEST" if missing else "REASONABLE_CHECK",
        rationale="根据冻结版本依赖与实际历史值域补充覆盖；不是已发生执行故障的预测。",supporting_evidence=refs,counter_evidence=[refs[-1]],uncertainties=[],
        existing_coverage="PARTIAL" if missing and tested else "FULL" if not missing else "NONE",recommended_tests=suggestions,clarification_questions=[])])
    return report,dict(read_sources=[d["source_ref"] for d in docs],rule_version=RULE_VERSION,input_digest=task["input_digest"])
