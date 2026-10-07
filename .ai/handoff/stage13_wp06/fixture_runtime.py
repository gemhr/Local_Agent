"""TEST_SCOPE / CONTROLLED_PASS_FIXTURE：真实 Core，脚本 Provider，无外网请求。"""
import asyncio
import json
import sys
from core.persistence.database import Database, DatabaseConfig
from tests.test_stage13_wp04_triage import assembly, payload, output
from core.stage13.contracts import sha256

async def main():
    request=json.loads(sys.stdin.read())
    database=Database(DatabaseConfig(url="postgresql+asyncpg://postgres@127.0.0.1:55433/stage13_wp06_fixture_test"))
    p=request.get("input") or payload()
    if "input" not in request:
        evidence=p["visible_evidence"][0]
        evidence["content"]=json.dumps({"component":"checkout","visible_change_refs":["change-1"],"error_excerpt":"CONTROLLED_PASS_FIXTURE test assertion mismatch"})
        evidence["digest"]=sha256(evidence["content"].encode())
        p["EvidenceRefs"][0]["digest"]=evidence["digest"]
    service,calls,client=await assembly(database,[json.dumps(output(p),ensure_ascii=False)])
    try:
        manifest=service.manifests["ci_triage_baseline"]
        if request.get("run_id"):
            response=await service.evaluation_execute(run_id=request["run_id"],agent_id="ci_triage_baseline",query=json.dumps(p,ensure_ascii=False,separators=(",",":")),timeout_seconds=120,expected_subject_manifest=manifest)
            result={"response":response,"scripted_provider_calls":len(calls),"real_model_calls":0}
        else:
            result={"manifest":manifest,"input":p,"classification":"TEST_SCOPE / CONTROLLED_PASS_FIXTURE"}
        print(json.dumps(result,ensure_ascii=False))
    finally:
        await service.services.close(timeout=20)
        await client.aclose()
        await database.close()

asyncio.run(main())
