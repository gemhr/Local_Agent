"""公开接口条件的有限覆盖分析；只消费说明书与历史步骤，不读取评价参考。"""
from typing import Literal
from pydantic import Field
from core.feature_impact.reports import Strict, Finding as BaseFinding
from core.ci_simulation.daas_assets import Test, Ref
from core.feature_impact.knowledge import read

CONTRACT = "interface-coverage-analysis.v1"
METHOD = "1.2.0"
RULE_VERSION = "interface-conditions.v1"


class PlanningTest(Strict):
    test_kind: Literal["PLANNING_ONLY"]
    test_id: str = Field(min_length=1)
    preconditions: str = Field(min_length=1)
    condition_codes: list[str] = Field(min_length=1)
    event_order: list[str] = Field(min_length=1)
    observation: str = Field(min_length=1)
    expected: str = Field(min_length=1)
    historical_cases: list[str]
    coverage_gap: str


class Finding(BaseFinding):
    recommendation: Literal["ADD", "REUSE", "CLARIFY"]
    target_version: Ref
    path: Literal["VALUE_RANGE", "HOT_RECONFIG", "COLD_START", "RECOVERY"]
    condition_codes: list[str]
    missing_condition_codes: list[str]
    existing_case_refs: list[str]
    clarification_keys: list[str]
    event_order: list[str]
    observation_code: str
    recommended_tests: list[Test | PlanningTest]


class Report(Strict):
    schema_version: Literal["feature-impact-report.v3"]
    findings: list[Finding] = Field(min_length=1, max_length=20)


def method(snapshot):
    return METHOD if snapshot.get("analysis_contract") == CONTRACT else "1.1.0"


def report_schema(snapshot):
    return "feature-impact-report.v3" if method(snapshot) == METHOD else "feature-impact-report.v2" if snapshot.get("analysis_contract") == "interface-impact.v1" else "feature-impact-report.v1"


def rule_report(task):
    docs = [read(task, d["source_ref"]) for d in task["documents"]]
    def ref(d):
        return {k:d[k] for k in ("source_ref", "version", "chunk", "content_digest")} | {"basis":"EVIDENCE_READ"}
    if any(d["kind"] == "INTERFACE_CATALOG" for d in docs):
        from core.feature_impact.interface_impact import rule_report as range_rule
        value, _ = range_rule(task)
        findings = value["findings"]
        for f in findings:
            f.update(recommendation="ADD", target_version=dict(id="sampling-control",version="2"), path="VALUE_RANGE",
                condition_codes=f["trigger_conditions"], missing_condition_codes=["GAIN_2"], existing_case_refs=[d["source_ref"] for d in docs if d["kind"]=="CASE"],
                clarification_keys=[],event_order=["CONFIGURE","SAMPLE","OBSERVE"],observation_code="SAMPLED_OUTPUT")
    else:
        aliases = task["condition_aliases"]
        def codes(values):
            return [aliases.get(v,v) for v in values]
        requirements = [d for d in docs if d["kind"]=="REQUIREMENT"]
        cases = [d for d in docs if d["kind"]=="CASE"]
        findings = []
        for requirement in requirements:
            facts = requirement["facts"]
            # 路径相同且版本相符才有资格声称覆盖。标题和标签不参与证明。
            related = [d for d in cases if d["facts"].get("path")==facts["path"] and d["facts"].get("target_version")==facts["target_version"]]
            conditions = codes(facts["conditions"])
            order = codes(facts["event_order"])
            unknown = [k for k,v in facts.get("configuration_semantics",{}).items() if v is None]
            best = None
            missing = list(conditions)
            for case in related:
                actual = case["facts"]
                actual_conditions, steps = codes(actual["conditions"]), codes(actual["steps"])
                absent = [v for v in conditions if v not in actual_conditions]
                cursor = 0
                for event in order:
                    try: cursor = steps.index(event,cursor)+1
                    except ValueError:
                        absent.append(event)
                        break
                if actual.get("observation")!=facts["observation"]: absent.append(facts["observation"])
                if actual.get("expected")!=facts["expected"]: absent.append("EXPECTED_NEW_CONFIG")
                if best is None or len(absent)<len(missing): best,missing=case,list(dict.fromkeys(absent))
            full = best is not None and not missing and not unknown
            action = "CLARIFY" if unknown else "REUSE" if full else "ADD"
            tests=[]
            if action=="ADD":
                tests=[dict(test_kind="PLANNING_ONLY",test_id="requirement-"+facts["path"].lower(),preconditions="共同满足全部条件后执行所列事件顺序；需后续实现的测试条件",
                    condition_codes=conditions,event_order=order,observation=facts["observation"],expected=facts["expected"],historical_cases=[d["source_ref"] for d in related],coverage_gap="历史实际步骤缺少："+", ".join(missing))]
            findings.append(dict(finding_id="path-"+facts["path"].lower(),historical_feature_id=facts["target_version"]["id"],impact_mechanism="CONFIGURATION_MESSAGE_TIMING",
                trigger_conditions=conditions,coverage_class="INSUFFICIENT_EVIDENCE" if unknown else "REASONABLE_CHECK" if full else "MUST_TEST",
                rationale="对照说明书正文/脚注与历史实际步骤；完整条件按合取和事件顺序判断。",supporting_evidence=[ref(requirement)]+[ref(d) for d in docs if d["kind"]=="TERMINOLOGY"]+[ref(d) for d in related],counter_evidence=[],
                uncertainties=unknown,existing_coverage="UNKNOWN" if unknown else "FULL" if full else "PARTIAL" if related else "NONE",recommended_tests=tests,
                clarification_questions=[{"CONFIG_RETAINED":"恢复时是否保留恢复前配置？", "EFFECTIVE_AT":"配置在何时重新成为有效配置？", "OBSERVED_MESSAGE":"观察的是恢复后首条还是稳定后的消息？"}.get(k,"请明确配置语义："+k) for k in unknown],
                recommendation=action,target_version=facts["target_version"],path=facts["path"],condition_codes=conditions,missing_condition_codes=[] if unknown else missing,
                existing_case_refs=[best["source_ref"]] if full else [d["source_ref"] for d in related],clarification_keys=unknown,event_order=order,observation_code=facts["observation"]))
    return dict(schema_version="feature-impact-report.v3",findings=findings),dict(read_sources=[d["source_ref"] for d in docs],rule_version=RULE_VERSION,input_digest=task["input_digest"])
