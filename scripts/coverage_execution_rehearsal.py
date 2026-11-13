"""HUMAN HTTP 覆盖工作流；不读 EO GT、不调用模型、不把准备当执行。"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from uuid import uuid4
import requests

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


async def database_facts():
    from sqlalchemy import select,func
    from dataclasses import replace
    from tests._pg_fixtures import test_database_url
    from core.settings import Settings
    from core.persistence.database import Database,DatabaseConfig
    from core.ci_simulation.models import CIExecutionRow
    from core.incidents.models import IncidentRow
    from core.persistence.models import RuntimeModelInvocationRow
    db=Database(DatabaseConfig.from_settings(replace(Settings.load(),database_url=test_database_url())))
    try:
        async with db.session() as session:
            return {name:await session.scalar(select(func.count()).select_from(model)) for name,model in (("ci",CIExecutionRow),("incidents",IncidentRow),("models",RuntimeModelInvocationRow))}
    finally:await db.dispose()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    child=subprocess.Popen([sys.executable,"-u",str(ROOT/"scripts/coverage_execution_server.py")],cwd=ROOT,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding="utf-8")
    threading.Thread(target=lambda:[None for _ in child.stderr],daemon=True).start()
    evidence={"model_calls":0,"incremental_agent_value":"NOT_VALIDATED"}
    path=ROOT/".ai/handoff/stage21_baseband_agents/coverage_http_workflow.json"
    try:
        live=None
        for line in child.stdout:
            if line.startswith("COVERAGE_PRIVATE:"):live=json.loads(line.split(":",1)[1]);break
        if not live:raise RuntimeError("COVERAGE_BACKEND_START_FAILED")
        threading.Thread(target=lambda:[None for _ in child.stdout],daemon=True).start()
        base=live["base_url"];headers={"Authorization":"Bearer "+live["human_token"]}
        def call(url,body=None,expected=(200,201)):
            response=requests.get(base+url,headers=headers,timeout=15) if body is None else requests.post(base+url,json=body,headers=headers,timeout=15)
            if response.status_code not in expected:raise RuntimeError("HTTP_BUSINESS_FAILED:"+str(response.status_code)+":"+response.text[:200])
            return response.json()
        before=asyncio.run(database_facts())
        task=call("/api/feature-impact/public-experiment-tasks",dict(request_id=str(uuid4()),public_task="sampling-interface"))
        report=call(f'/api/feature-impact/tasks/{task["task_id"]}/rule-reports',dict(request_id=str(uuid4())))
        assert report["status"]=="SEALED"
        plan=call(f'/api/feature-impact/reports/{report["report_id"]}/reviews',dict(request_id=str(uuid4()),report_version=report["report_version"],report_digest=report["report_digest"],
            decisions=[dict(finding_id=f["finding_id"],decision="ACCEPT",reason="HUMAN 核对新版接口和历史 gain=1 缺口") for f in report["report"]["findings"]]))
        body=dict(request_id=str(uuid4()),plan_digest=plan["plan_digest"],test_id=plan["tests"][0]["test_id"],executor_ref="local-integer-executor@1")
        url=f'/api/feature-impact/coverage-plans/{plan["plan_id"]}/case-assets'
        asset=call(url,body);assert asset==call(url,body)
        assert asset["execution_status"]=="NOT_EXECUTED" and asset["executions"]==[]
        assert not call(f'/api/feature-impact/tasks/{task["task_id"]}')["exposed"]
        after_prepare=asyncio.run(database_facts())
        assert after_prepare==before
        evidence.update(task=task,report=report,plan=plan,prepared=asset,prepare_retry_same=True,counts_before=before,counts_after_prepare=after_prepare,prepared_ci_delta=after_prepare["ci"]-before["ci"],prepared_incident_delta=after_prepare["incidents"]-before["incidents"])
        path.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding="utf-8")
        execution_url=f'/api/ci/scenarios/{asset["instance_id"]}/executions'
        intent=dict(request_id=str(uuid4()),case_id="integer-primary",expected_state_revision=1,asset_digest=asset["asset_digest"])
        denied=call(execution_url,{k:v for k,v in intent.items() if k!="asset_digest"},expected=(409,))
        executed=call(execution_url,intent);retry=call(execution_url,intent)
        assert executed["execution_id"]==retry["execution_id"]
        conflict=call(execution_url,intent|dict(case_id="integer-control"),expected=(409,))
        exposed=call(f'/api/feature-impact/tasks/{task["task_id"]}')
        late=call(f'/api/feature-impact/tasks/{task["task_id"]}/rule-reports',dict(request_id=str(uuid4())),expected=(409,))
        assets=call(url);assert len(assets["items"])==1 and len(assets["items"][0]["executions"])==1
        incident=call('/api/ci/incidents/'+executed["incident_id"])
        assert incident["diagnosis_source"]=="RULE" and incident["model_execution"]=="NOT_EXECUTED"
        after_execute=asyncio.run(database_facts())
        assert after_execute["ci"]-before["ci"]==after_execute["incidents"]-before["incidents"]==1 and after_execute["models"]==0
        evidence["counts_after_execute"]=after_execute
        selector=dict(task_id=task["task_id"],report_id=report["report_id"],plan_id=plan["plan_id"],test_id=body["test_id"],instance_id=asset["instance_id"],execution_id=executed["execution_id"],incident_id=executed["incident_id"],diagnosis_kind="RULE")
        evidence.update(selector=selector,execution=executed,execution_retry_same=True,direct_ci_denied=denied,different_intent_denied=conflict,planning_after_execution_denied=late,
            task_exposed=exposed["exposed"],assets_reloaded=assets,incident=incident,workflow="PASS")
    finally:
        child.stdin.close()
        try:child.wait(timeout=45)
        except subprocess.TimeoutExpired:child.terminate();child.wait(timeout=15)
        path.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(dict(workflow=evidence.get("workflow"),selector=evidence.get("selector"),model_calls=0),ensure_ascii=False))


if __name__=="__main__":main()
