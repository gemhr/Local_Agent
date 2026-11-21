"""有界公开实验驱动；凭据只走私有内存管道，所有尝试独立留档。"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
EVIDENCE=ROOT/".ai/handoff/stage21_baseband_agents"


def environment():
    from sqlalchemy.engine import make_url
    for name in (".env",".env.test"):
        path=ROOT/name
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                    key,value=line.split("=",1);os.environ.setdefault(key.strip(),value.strip().strip('"').strip("'"))
    from tests._pg_fixtures import test_database_url
    name=json.loads((EVIDENCE/"feature_database.json").read_text())["database_name"]
    url=make_url(test_database_url()).set(database=name).render_as_string(hide_password=False)
    os.environ.update(LOCAL_AGENT_TEST_DATABASE_URL=url,LOCAL_AGENT_DATABASE_URL=url,LOCAL_AGENT_ENVIRONMENT_PROFILE="TEST",
        LOCAL_AGENT_REMOTE_VERIFY_TLS="1",LOCAL_AGENT_REMOTE_ENABLE_THINKING="0",LOCAL_AGENT_FEATURE_COVERAGE_EXPERIMENT="1",FEATURE_COVERAGE_SERVER="1",PYTHONIOENCODING="utf-8")
    os.environ.setdefault("LOCAL_AGENT_REMOTE_API_BASE_URL","https://api.deepseek.com")


async def run_facts(run_id):
    from core.persistence.database import Database,DatabaseConfig
    from core.settings import Settings
    from core.persistence.models import RuntimeRunExecutionRow,RuntimeModelInvocationRow,DurableToolInvocationRow
    from sqlalchemy import select
    db=Database(DatabaseConfig.from_settings(Settings.load()))
    try:
        async with db.session() as session:
            root=await session.get(RuntimeRunExecutionRow,run_id)
            models=(await session.scalars(select(RuntimeModelInvocationRow).where(RuntimeModelInvocationRow.run_id==run_id))).all()
            tools=(await session.scalars(select(DurableToolInvocationRow).where(DurableToolInvocationRow.run_id==run_id))).all()
            return dict(status=root.status if root else "NOT_PERSISTED",stop_reason=root.stop_reason if root else None,
                budget_totals=root.budget_totals if root else None,budget_consumed=root.budget_consumed if root else None,
                models=[dict(state=m.state,purpose=m.purpose,safe_error=m.safe_error,provider_kind=m.provider_kind,profile_identity=m.profile_identity,
                    request_assessment=m.request_assessment,usage_evidence=m.usage_evidence,cost_evidence=m.cost_evidence) for m in models],
                tools=[dict(tool_name=t.tool_name,state=t.state) for t in tools])
    finally:await db.dispose()


def main():
    import requests
    parser=argparse.ArgumentParser();parser.add_argument("--phase",choices=["development","formal"],default="development");parser.add_argument("--freeze")
    args=parser.parse_args();environment()
    if args.phase=="formal" and (not args.freeze or not Path(args.freeze).is_file()):raise RuntimeError("FORMAL_FREEZE_REQUIRED")
    if args.phase=="formal":
        import hashlib
        freeze=json.loads(Path(args.freeze).read_text(encoding="utf-8"))
        for name,expected in freeze["source_sha256"].items():
            if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=expected:raise RuntimeError("FORMAL_SOURCE_CHANGED:"+name)
    batch=EVIDENCE/("feature_"+args.phase+"_"+uuid4().hex+".json")
    result=dict(phase=args.phase,attempts=[],freeze=json.loads(Path(args.freeze).read_text(encoding="utf-8")) if args.freeze else None)
    child=subprocess.Popen([sys.executable,"-u",str(ROOT/"scripts/coverage_execution_server.py")],cwd=ROOT,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding="utf-8")
    errors=[]
    def drain():
        for line in child.stderr:
            # 仅保存受控本地错误日志，不回显认证或HTTP请求正文。
            errors.append(line)
    threading.Thread(target=drain,daemon=True).start()
    try:
        live=None
        for line in child.stdout:
            if line.startswith("COVERAGE_PRIVATE:"):live=json.loads(line.split(":",1)[1]);break
        if live is None:raise RuntimeError("FEATURE_BACKEND_START_FAILED")
        threading.Thread(target=lambda:[None for _ in child.stdout],daemon=True).start()
        headers={"Authorization":"Bearer "+live["human_token"]}
        def call(path,body=None):
            r=requests.get(live["base_url"]+path,headers=headers,timeout=30) if body is None else requests.post(live["base_url"]+path,headers=headers,json=body,timeout=30)
            if r.status_code not in (200,201):raise RuntimeError("FEATURE_HTTP_FAILED:"+str(r.status_code)+":"+r.text[:160])
            return r.json()
        names=["interface-range-analysis"] if args.phase=="development" else ["interface-range-analysis","interface-message-analysis","interface-recovery-analysis"]
        for name in names:
            code=name+("-dev" if args.phase=="development" else "")
            attempt=dict(public_task=code);result["attempts"].append(attempt)
            # 两侧业务Task分开；Agent Session不含RULE报告或评审结果。
            rule_task=call("/api/feature-impact/public-experiment-tasks",dict(request_id=str(uuid4()),public_task=code))
            rule=call(f'/api/feature-impact/tasks/{rule_task["task_id"]}/rule-reports',dict(request_id=str(uuid4())))
            task=call("/api/feature-impact/public-experiment-tasks",dict(request_id=str(uuid4()),public_task=code))
            attempt.update(rule_task=rule_task,rule_report=rule,agent_task=task)
            run=call(f'/api/feature-impact/tasks/{task["task_id"]}/investigations',dict(request_id=str(uuid4())))
            attempt["run"]=run;end=time.monotonic()+210
            while time.monotonic()<end:
                state=call(f'/api/feature-impact/tasks/{task["task_id"]}')
                reports=[r for r in state["reports"] if r.get("run_id")==run["run_id"]]
                if reports:
                    attempt["agent_report"]=call('/api/feature-impact/reports/'+reports[0]["report_id"]);break
                time.sleep(.5)
            attempt["runtime"]=asyncio.run(run_facts(run["run_id"]))
            batch.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
            print(json.dumps(dict(public_task=code,run_id=run["run_id"],status=attempt["runtime"]["status"],stop_reason=attempt["runtime"]["stop_reason"],model_invocations=len(attempt["runtime"]["models"]),report_status=attempt.get("agent_report",{}).get("status")),ensure_ascii=False),flush=True)
    finally:
        child.stdin.close()
        try:child.wait(timeout=40)
        except subprocess.TimeoutExpired:child.terminate();child.wait(timeout=10)
        batch.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        batch.with_suffix(".log").write_text("".join(errors),encoding="utf-8")
    print("EVIDENCE="+str(batch),flush=True)


if __name__=="__main__":main()
