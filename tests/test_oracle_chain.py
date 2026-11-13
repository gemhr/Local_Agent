"""Oracle 参数因果、采样边界和缺失状态的非唯一解释。"""
from copy import deepcopy
from types import SimpleNamespace as NS
import pytest
from core.ci_simulation import oracle_chain as chain
from core.incidents.joint_diagnosis import rule_report, validate_sources
from core.incidents.contracts import CIError
from core.ci_simulation.actions import preview, apply_preview


def test_oracle_reference_and_output_causality():
    state=chain.initial_state("oracle_reference_scope")
    first=chain.execute(state,"same")
    assert first["outcome"]=="FAIL"
    assert all(s["status"]=="EXECUTED" for s in first["summary"]["stages"])
    change=preview(state,"SELECT_BASEBAND_REFERENCE",None,dict(reference_id="integer-reference",version="2"))
    after=apply_preview(state,"SELECT_BASEBAND_REFERENCE",None,dict(reference_id="integer-reference",version="2"),change)
    assert chain.execute(after,"renamed")["outcome"]=="PASS"
    assert first["outcome"]=="FAIL" and state["baseband"]["reference"]["version"]=="1"
    after["baseband"]["implementation"]["offset"]=2
    assert chain.execute(after,"PASS-in-name")["outcome"]=="FAIL"
    after["test_case"]["case_id"]="unrelated"
    assert chain.execute(after,"different")["outcome"]=="FAIL"
    with pytest.raises(CIError): preview(state,"ALIGN_BASEBAND_INPUT",None,dict(rule_version="1"))


def test_daas_sampling_and_partial_capture_ambiguity():
    state=chain.initial_state("oracle_application_delay")
    v=state["baseband"]
    assert chain.simulate(v)["outcome"]=="FAIL"
    v["daas"]["events"][-1]["tick"]=10
    assert chain.simulate(v)["effective"]["config_id"]=="config-old"
    v["daas"]["events"][-1]["tick"]=9
    assert chain.simulate(v)["outcome"]=="PASS"
    v["daas"]["events"][2]["status"]="REJECTED"
    assert chain.simulate(v)["effective"]["config_id"]=="config-old"
    v["daas"]["events"][2]["status"]="ACCEPTED"
    v["daas"]["events"][-1]["config_id"]="unrelated"
    assert chain.simulate(v)["effective"]["config_id"]=="config-old"
    left=chain.initial_state("oracle_partial_capture")
    right=deepcopy(left)
    right["baseband"]["implementation"]["offset"]=0
    right["baseband"]["daas"]["events"][-1]["tick"]=4
    assert chain.simulate(left["baseband"])["effective"]!=chain.simulate(right["baseband"])["effective"]
    def visible(s):
        result=chain.execute(s,"same-execution")
        projection={e["kind"]:e["content"] for e in result["evidence_manifest"]}
        projection["CI_SUMMARY"].pop("completed_at")
        projection["CI_SUMMARY"].pop("input_digest") # 不可逆身份摘要不暴露状态字段
        return projection
    assert visible(left)==visible(right)
    shown=deepcopy(left);shown["baseband"]["capture"]["applied_state"]=True
    assert chain.simulate(shown["baseband"])["actual"]==chain.simulate(left["baseband"])["actual"]
    execution=NS(execution_id="execution",**chain.execute(left,"execution"))
    report=rule_report("incident",execution)
    assert report["decidability"]=="INSUFFICIENT_EVIDENCE" and len(report["candidate_findings"])==2
    seen={e["evidence_id"]:dict(incident_id="incident",**e) for e in execution.evidence_manifest}
    row=NS(agent_id="baseband_test_investigator",agent_version="1.1.0")
    assert validate_sources(report,row,NS(incident_id="incident"),execution,seen)==report
    report["candidate_findings"][1].update(mechanism="CONFIG_APPLIED_AFTER_SAMPLE",finding_status="CONFIRMED",failure_relation="SUPPORTED")
    with pytest.raises(CIError):validate_sources(report,row,NS(incident_id="incident"),execution,seen)
