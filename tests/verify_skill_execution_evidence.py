"""离线复核保留的 Skill 执行记录；不调用模型、工具或写数据库。"""
import argparse
import asyncio
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace as NS

from core.runtime.execution_aggregate import canonical_payload_digest as digest
from core.runtime.model_invocation import ModelInvocationRouter
from core.runtime.tool_contract import canonical_json_digest
from core.agent_platform.ci_diagnosis import validate_report

RUN = "c0ae1c50-6595-464d-86a8-65bef4828664"
TABLES = ("runtime_run_executions", "runtime_step_executions", "runtime_loop_frames",
          "runtime_loop_iterations", "runtime_loop_actions", "runtime_model_invocations",
          "runtime_tool_invocations", "runtime_context_snapshots", "incident_investigations")


async def export_records(path):
    """仅恢复已确认的专用实例，重复读/只读事务；没有迁移或 fixture。"""
    import asyncpg
    connection = await asyncpg.connect("postgresql://postgres@127.0.0.1:55438/localagent_skills_wp2_test")
    try:
        await connection.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
        data = {"source": {"database": "127.0.0.1:55438/localagent_skills_wp2_test",
                "data_directory": "TEMP/localagent-skills-wp2-pg", "run_id": RUN,
                "mode": "repeatable_read/read_only"}, "records": {}}
        async with connection.transaction(isolation="repeatable_read", readonly=True):
            assert await connection.fetchval("show transaction_read_only") == "on"
            assert await connection.fetchval("select current_database()") == "localagent_skills_wp2_test"
            directory = await connection.fetchval("show data_directory")
            assert Path(directory).name == "localagent-skills-wp2-pg"
            data["source"]["server_data_directory"] = directory
            for table in TABLES:
                data["records"][table] = [dict(row) for row in await connection.fetch(
                    "select * from " + table + " where run_id=$1", RUN)]
            investigation, = data["records"]["incident_investigations"]
            incident = dict(await connection.fetchrow("select * from incidents where incident_id=$1", investigation["incident_id"]))
            execution = dict(await connection.fetchrow("select * from ci_executions where execution_id=$1", incident["execution_id"]))
            data["records"]["incidents"] = [incident]
            data["records"]["ci_executions"] = [execution]
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    finally:
        await connection.close()


def verify(path, historical):
    data = json.loads(path.read_text(encoding="utf-8"))
    old = json.loads(historical.read_text(encoding="utf-8"))
    records = data["records"]
    root, = records["runtime_run_executions"]
    frame, = records["runtime_loop_frames"]
    step, = records["runtime_step_executions"]
    investigation, = records["incident_investigations"]
    incident, = records["incidents"]
    execution, = records["ci_executions"]
    assert root["run_id"] == RUN == investigation["run_id"] == old["run_id"]
    assert frame["frame_id"] == old["frame_id"] and root["status"] == "SUCCEEDED"
    iterations = sorted(records["runtime_loop_iterations"], key=lambda i: i["iteration_no"])
    models = records["runtime_model_invocations"]
    snapshots = records["runtime_context_snapshots"]
    tools = records["runtime_tool_invocations"]
    actions = records["runtime_loop_actions"]
    request_links = []
    for iteration in iterations:
        matches = [m for m in models if m["run_id"] == RUN and m["loop_frame_id"] == frame["frame_id"]
            and m["step_id"] == frame["step_id"] and m["loop_iteration_no"] == iteration["iteration_no"]
            and m["attempt_number"] == iteration["accepted_model_attempt"]
            and m["model_attempt_number"] == iteration["accepted_model_attempt_number"]
            and m["state"] == "COMPLETED" and m["purpose"] == "DECISION"]
        model, = matches
        snapshot, = [s for s in snapshots if s["snapshot_id"] == model["context_snapshot_id"]
            and s["frame_id"] == frame["frame_id"] and s["run_id"] == RUN
            and s["snapshot_digest"] == model["context_snapshot_digest"]]
        payload = snapshot["payload"]
        assert digest(payload) == snapshot["snapshot_digest"]
        assert digest(payload["messages"]) == payload["messages_digest"]
        assert digest(payload["options"]) == payload["options_digest"]
        assert ModelInvocationRouter._durable_model_request_digest(payload["messages"],
            max_tokens=payload["output_reserve"], generation_options=payload["options"]) == model["request_digest"]
        assert digest(model["result_binding"]) == model["result_digest"]
        capture, = [c for c in old["request_links"] if c["iteration"] == iteration["iteration_no"]]
        assert capture["snapshot_id"] == snapshot["snapshot_id"]
        assert capture["request_digest"] == digest(payload["messages"])
        if iteration["observation"] is not None:
            assert digest(iteration["observation"]) == iteration["observation_digest"]
        request_links.append(dict(iteration=iteration["iteration_no"], snapshot_id=snapshot["snapshot_id"],
            attempt=model["attempt_number"], model_attempt=model["model_attempt_number"],
            messages_digest=payload["messages_digest"], full_request_digest=model["request_digest"],
            provider_kind=model["provider_kind"]))

    returned = []
    for action in actions:
        tool, = [t for t in tools if t["invocation_id"] == action["invocation_id"] and t["run_id"] == RUN]
        iteration, = [i for i in iterations if i["iteration_no"] == action["iteration_no"]]
        model, = [m for m in models if m["loop_iteration_no"] == action["iteration_no"]
            and m["attempt_number"] == iteration["accepted_model_attempt"]
            and m["model_attempt_number"] == iteration["accepted_model_attempt_number"]]
        call, = [c for c in model["result_binding"]["native_tool_calls"] if c["provider_tool_call_id"] == action["protocol_call_id"]]
        recipe = action["recipe"]
        assert recipe["tool_name"] == tool["tool_name"] == call["tool_name"]
        assert json.loads(call["arguments_json"]) == recipe["arguments"]
        assert canonical_json_digest(recipe["arguments"]) == tool["arguments_digest"]
        assert digest(recipe) == action["recipe_digest"]
        assert tool["state"] == "COMMITTED" and tool["committed_result"]["status"] == "SUCCEEDED"
        assert canonical_json_digest(tool["committed_result"]) == tool["committed_result_digest"] == action["source_digest"]
        projection = {"outcome": "SUCCEEDED", "content": tool["committed_result"]["output"]["content"]}
        assert projection == action["result_projection"] and digest(projection) == action["result_digest"]
        assert any(r["result"] == projection for r in iteration["observation"]["results"])
        later = []
        for link in request_links:
            if link["iteration"] <= action["iteration_no"]:
                continue
            snapshot, = [s for s in snapshots if s["snapshot_id"] == link["snapshot_id"]]
            if any(m.get("role") == "tool" and m.get("tool_call_id") == action["protocol_call_id"]
                   and json.loads(m["content"]) == projection for m in snapshot["payload"]["messages"]):
                later.append(link["iteration"])
        assert later
        content = json.loads(projection["content"])
        returned.append(dict(tool=tool["tool_name"], arguments=json.loads(call["arguments_json"]),
            call_id=action["protocol_call_id"], invocation_id=tool["invocation_id"],
            iteration=action["iteration_no"], result_digest=tool["committed_result_digest"],
            first_later_request=min(later), content=content))

    skill, = [r for r in returned if r["tool"] == "load_skill"]
    reference, = [r for r in returned if r["tool"] == "read_skill_resource"]
    assert skill["arguments"] == {"name": "ci-failure-investigation"}
    assert reference["arguments"] == {"name": "ci-failure-investigation", "relative_path": "references/failure-evidence-guide.md"}
    identities = frame["pinned_config"]["skills"]
    assert set(identities) == {"ci-failure-investigation", "ci-environment-check"}
    for entry in (skill, reference):
        content = entry["content"]
        assert content["frame_id"] == frame["frame_id"] and content["agent_id"] == frame["execution_agent_id"]
        assert sha256(content["content"].encode("utf-8")).hexdigest() == content["sha256"] == identities[content["name"]]["files"][content["relative_path"]]
    first_model, = [m for m in models if m["loop_iteration_no"] == 1]
    first_snapshot, = [s for s in snapshots if s["snapshot_id"] == first_model["context_snapshot_id"]]
    first = "\n".join(m.get("content") or "" for m in first_snapshot["payload"]["messages"])
    for name, identity in identities.items():
        assert name in first and identity["description"] in first
    assert "feature-impact-analysis" not in first
    assert skill["content"]["content"] not in first and reference["content"]["content"] not in first
    context, = [r for r in returned if r["tool"] == "ci_get_execution_context"]
    assert context["content"]["execution_id"] == execution["execution_id"]
    assert context["content"]["incident_id"] == incident["incident_id"] == investigation["incident_id"]
    raw = step["typed_result_payload"]["content"]
    final, = [m for m in models if m["loop_iteration_no"] == iterations[-1]["iteration_no"]]
    assert raw == final["result_binding"]["output"]
    image = NS(root=NS(**root), steps=[NS(**step)], loop_frames=[NS(**frame)],
        loop_iterations=[NS(**i) for i in iterations], tool_invocations=[NS(**t) for t in tools])
    report = validate_report(raw, NS(**investigation), NS(**incident), NS(**execution), image, [NS(**a) for a in actions])
    facts = {e["kind"]: e["content"] for e in execution["evidence_manifest"] if e["availability"] == "AVAILABLE"}
    return dict(run_id=RUN, frame_id=frame["frame_id"], input_sha256=sha256(path.read_bytes()).hexdigest(),
        status="VERIFIED_OFFLINE", evidence={f"E{i}": "VERIFIED_OFFLINE" for i in range(1, 11)},
        request_links=request_links, tools=[{k:v for k,v in r.items() if k != "content"} for r in returned],
        report_revalidation="PASS (本轮离线重校验，不是原始持久化校验回执)", report=report,
        original_projection_status=investigation["projection_status"], supporting_facts=facts,
        provenance_limit="数据库导出/原 Capture 与源码支持原 Adapter 调用；不是 Provider 签名回执。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("records", type=Path)
    parser.add_argument("historical", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--export", action="store_true", help="从已确认专用库只读导出；默认纯离线")
    args = parser.parse_args()
    if args.export:
        asyncio.run(export_records(args.records))
    result = verify(args.records, args.historical)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(result["status"], result["run_id"], len(result["request_links"]), "requests", len(result["tools"]), "actions")
