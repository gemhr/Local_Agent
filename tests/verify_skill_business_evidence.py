"""离线复核三个合成业务 Run 的持久内容及真实 Adapter 捕获。"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace as NS
from core.runtime.execution_aggregate import canonical_payload_digest as digest
from core.runtime.model_invocation import ModelInvocationRouter
from core.runtime.tool_contract import canonical_json_digest


def verify(case,entry):
    r=entry["records"]
    root,=r["runtime_run_executions"]
    frame,=r["runtime_loop_frames"]
    step,=r["runtime_step_executions"]
    assert root["run_id"]==entry["run_id"] and root["status"]=="SUCCEEDED"
    snapshots=r["runtime_context_snapshots"]
    models=r["runtime_model_invocations"]
    iterations=r["runtime_loop_iterations"]
    for capture in entry["requests"]:
        s,=[s for s in snapshots if s["snapshot_id"]==capture["snapshot_id"]]
        p=s["payload"]
        assert digest(p)==s["snapshot_digest"]
        assert capture["messages"]==p["messages"] and digest(capture["messages"])==capture["messages_digest"]==p["messages_digest"]
        m,=[m for m in models if m["context_snapshot_id"]==s["snapshot_id"] and m["state"]=="COMPLETED" and m["loop_frame_id"]==frame["frame_id"]]
        i,=[i for i in iterations if i["iteration_no"]==m["loop_iteration_no"]]
        assert (i["accepted_model_attempt"],i["accepted_model_attempt_number"])==(m["attempt_number"],m["model_attempt_number"])
        assert m["provider_kind"]=="deepseek" and digest(m["result_binding"])==m["result_digest"]
        assert ModelInvocationRouter._durable_model_request_digest(capture["messages"],max_tokens=capture["max_tokens"],generation_options=capture["options"])==m["request_digest"]
    first="\n".join(m.get("content") or "" for m in entry["requests"][0]["messages"])
    assert frame["pinned_config"]["skills"]
    for name,value in frame["pinned_config"]["skills"].items():
        assert name in first and value["description"] in first
    seen=set()
    for a in r["runtime_loop_actions"]:
        assert a["frame_id"]==frame["frame_id"] and a["run_id"]==root["run_id"]
        t,=[t for t in r["runtime_tool_invocations"] if t["invocation_id"]==a["invocation_id"]]
        i,=[i for i in iterations if i["iteration_no"]==a["iteration_no"]]
        m,=[m for m in models if m["loop_iteration_no"]==i["iteration_no"] and m["attempt_number"]==i["accepted_model_attempt"] and m["model_attempt_number"]==i["accepted_model_attempt_number"]]
        call,=[c for c in m["result_binding"]["native_tool_calls"] if c["provider_tool_call_id"]==a["protocol_call_id"]]
        assert json.loads(call["arguments_json"])==a["recipe"]["arguments"]
        assert t["tool_name"]==call["tool_name"]==a["recipe"]["tool_name"]
        assert canonical_json_digest(a["recipe"]["arguments"])==t["arguments_digest"]
        assert t["state"]=="COMMITTED" and t["committed_result"]["status"]=="SUCCEEDED"
        assert canonical_json_digest(t["committed_result"])==t["committed_result_digest"]==a["source_digest"]
        projection={"outcome":"SUCCEEDED","content":t["committed_result"]["output"]["content"]}
        assert projection==a["result_projection"] and digest(projection)==a["result_digest"]
        assert digest(i["observation"])==i["observation_digest"] and any(v["result"]==projection for v in i["observation"]["results"])
        assert any(s["payload"]["source_cursor"]>=i["observation_cursor"] and any(msg.get("tool_call_id")==a["protocol_call_id"] and json.loads(msg["content"])==projection for msg in s["payload"]["messages"] if msg.get("role")=="tool") for s in snapshots)
        seen.add(t["tool_name"])
        if t["tool_name"] in {"load_skill","read_skill_resource"}:
            value=json.loads(projection["content"])
            assert value["content"] not in first
            assert sha256(value["content"].encode("utf-8")).hexdigest()==value["sha256"]==frame["pinned_config"]["skills"][value["name"]]["files"][value["relative_path"]]
            if t["tool_name"]=="load_skill":
                assert value["name"]==("ci-environment-check" if case=="C2" else "feature-impact-analysis")
    final,=[m for m in models if m["loop_iteration_no"]==max(i["iteration_no"] for i in iterations)]
    raw=step["typed_result_payload"]["content"]
    assert final["result_binding"]["output"]==raw
    if "final_output" in entry: assert raw==entry["final_output"]
    image=NS(root=NS(**root),steps=[NS(**step)],loop_frames=[NS(**frame)],loop_iterations=[NS(**i) for i in iterations],tool_invocations=[NS(**t) for t in r["runtime_tool_invocations"]])
    actions=[NS(**a) for a in r["runtime_loop_actions"]]
    if case=="C2":
        from core.agent_platform.ci_diagnosis import validate_report
        inputs=entry["ci_inputs"]
        report=validate_report(raw,NS(**inputs["investigation"]),NS(**inputs["incident"]),NS(**inputs["execution"]),image,actions)
        assert {"ci_get_execution_context","ci_read_execution_evidence","load_skill","read_skill_resource"}<=seen
    elif case=="C3":
        from core.feature_impact.agent_reports import validated_agent_report
        inputs=entry["feature_inputs"]
        report=validated_agent_report(NS(**inputs["binding"]),image,actions,NS(**inputs["task"]))[1]
        assert {"search_feature_history","read_feature_evidence","load_skill","read_skill_resource"}<=seen
        assert any(f["counter_evidence"] for f in report["findings"])
        assert any(f["recommended_tests"] for f in report["findings"])
        assert any(x["rejection"] is None for x in entry["business_reports"])
    else:
        assert not seen and not r["runtime_tool_invocations"]
        assert "切片" in raw or "slice" in raw.lower()
        report=None
    if report is not None: assert report==entry["validated_report"]
    return dict(case=case,run_id=root["run_id"],frame_id=frame["frame_id"],requests=len(models),tools=len(seen),status="VERIFIED_OFFLINE")


def audit_feature_sources(entry):
    """只读复核逐引用来源，独立于操作列表是否能通过整份报告校验。"""
    from core.feature_impact.agent import validate_result
    from core.feature_impact.reports import Report
    from core.feature_impact.knowledge import read
    records=entry["records"]
    image=NS(root=NS(**records["runtime_run_executions"][0]),
        **{name:[NS(**row) for row in records[table]] for name,table in {
            "steps":"runtime_step_executions","loop_frames":"runtime_loop_frames",
            "loop_iterations":"runtime_loop_iterations","tool_invocations":"runtime_tool_invocations"}.items()})
    inputs=entry["feature_inputs"]
    raw,source=validate_result(NS(**inputs["binding"]),image,[NS(**a) for a in records["runtime_loop_actions"]],inputs["task"]["snapshot"])
    report=Report.model_validate(json.loads(raw))
    refs=[]
    for finding in report.findings:
        for field in ("supporting_evidence","counter_evidence"):
            for ref in getattr(finding,field):
                doc=read(inputs["task"]["snapshot"],ref.source_ref)
                valid=all(getattr(ref,k)==doc[k] for k in ("version","chunk","content_digest")) and ref.source_ref in source["read_sources" if ref.basis=="EVIDENCE_READ" else "task_input_sources"]
                refs.append(dict(finding=finding.finding_id,field=field,source_ref=ref.source_ref,basis=ref.basis,valid=valid))
    return dict(schema="PASS",read_sources=source["read_sources"],references=refs,read_proof="PASS" if all(r["valid"] for r in refs) else "FAIL")


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence",type=Path)
    parser.add_argument("--case",choices=("C2","C3","C4"))
    parser.add_argument("--source-audit",action="store_true",help="同时逐条复核 C3 引用来源")
    args=parser.parse_args()
    data=json.loads(args.evidence.read_text(encoding="utf-8"))
    if args.source_audit:
        print(json.dumps(audit_feature_sources(data["C3"][-1]),ensure_ascii=False))
    failed=False
    for case in ([args.case] if args.case else ("C2","C3","C4")):
        try:
            result=verify(case,data[case][-1])
        except (AssertionError,ValueError,KeyError) as exc:
            failed=True
            result=dict(case=case,status="FAILED",error=type(exc).__name__,detail=str(exc))
        print(json.dumps(result,ensure_ascii=False))
    raise SystemExit(1 if failed else 0)
