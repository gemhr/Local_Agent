"""TEST_SCOPE 真实交付 endpoint/OS crash/replay/race；禁止外部业务系统。"""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4
import asyncpg
import httpx
from core.persistence.database import Database, DatabaseConfig
from core.stage13.delivery import DeliveryService, EvalOpsAuthorizationClient, delivery_identity

OUT=Path(os.environ["WP06_EVIDENCE_DIR"])
HERE=Path(__file__).parent
DB=os.environ.get("WP06_FIXTURE_DATABASE","stage13_wp06_fixture_test")
assert DB.startswith("stage13_wp06_") and DB.endswith("_test")

def save(name,v):
    (OUT/(name+".json")).write_text(json.dumps(v,ensure_ascii=False,sort_keys=True,indent=2,default=str),encoding="utf8")

async def dbconnect():
    return await asyncpg.connect(host="127.0.0.1",port=55433,user="postgres",database=DB)

async def child():
    q=json.loads(sys.stdin.read());database=Database(DatabaseConfig(url=f"postgresql+asyncpg://postgres@127.0.0.1:55433/{DB}"))
    async with httpx.AsyncClient(trust_env=False) as client:
        authority=EvalOpsAuthorizationClient(client,os.environ["LOCAL_AGENT_STAGE13_EVALOPS_URL"],os.environ["LOCAL_AGENT_STAGE13_DELIVERY_PROJECT"],os.environ["LOCAL_AGENT_STAGE13_EVALOPS_KEY"])
        async def fault(point):
            if os.environ.get("WP06_FAULT")==point:os._exit(73)
            if os.environ.get("WP06_FAULT")=="response_loss" and point=="after_sink_commit":raise OSError("controlled response loss")
        service=DeliveryService(database,authority,os.environ["LOCAL_AGENT_STAGE13_SCOPE"],os.environ["LOCAL_AGENT_STAGE13_DELIVERY_PROJECT"],q["binding"]["destination"]["id"],fault=fault)
        try:print(json.dumps(await service.deliver(q)))
        finally:await database.close()

async def launch_child(q,fault=None):
    env=dict(os.environ)
    env["WP06_FAULT"]=fault or ""
    p=await asyncio.create_subprocess_exec(sys.executable,str(__file__),"worker",cwd=str(HERE.parents[2]),env=env,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
    stdout,stderr=await p.communicate(json.dumps(q).encode())
    if p.returncode not in (0,73):raise RuntimeError(stderr.decode("utf8"))
    return {"pid":p.pid,"exit_code":p.returncode,"receipt":json.loads(stdout) if stdout else None}

async def main():
    binding=json.loads(sys.stdin.read())
    c=await dbconnect()
    async with httpx.AsyncClient(trust_env=False) as client:
        authority=EvalOpsAuthorizationClient(client,os.environ["LOCAL_AGENT_STAGE13_EVALOPS_URL"],os.environ["LOCAL_AGENT_STAGE13_DELIVERY_PROJECT"],os.environ["LOCAL_AGENT_STAGE13_EVALOPS_KEY"])
        async def request_for(destination):
            b=deepcopy(binding);b["destination"]["id"]=destination
            a=await authority.verify(b)
            assert a["authorization_status"]=="AUTHORIZED"
            return {"protocol_version":"stage13.delivery.v1","delivery_id":delivery_identity(b),"binding":b,"gate_authorization":a}
        q=await request_for("CONTROLLED_PASS_FIXTURE")
        env=dict(os.environ);env.update({"LOCAL_AGENT_ENVIRONMENT_PROFILE":"TEST","LOCAL_AGENT_STAGE13_DELIVERY_ENABLED":"1","LOCAL_AGENT_STAGE13_SERVICE_TOKEN":uuid4().hex,"LOCAL_AGENT_STAGE13_DESTINATION_ID":"CONTROLLED_PASS_FIXTURE","LOCAL_AGENT_DATABASE_URL":f"postgresql+asyncpg://postgres@127.0.0.1:55433/{DB}","PYTHONIOENCODING":"utf-8"})
        log=(OUT/"controlled-pass-endpoint.log").open("w",encoding="utf8")
        endpoint=subprocess.Popen([sys.executable,"-m","uvicorn","core.stage13.delivery_http:app","--host","127.0.0.1","--port","55442"],cwd=HERE.parents[2],env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            for _ in range(100):
                if endpoint.poll() is not None:raise RuntimeError("ENDPOINT_EXITED")
                try:
                    r=await client.get("http://127.0.0.1:55442/openapi.json")
                    if r.status_code==200:break
                except httpx.HTTPError:pass
                await asyncio.sleep(.1)
            headers={"Authorization":"Bearer "+env["LOCAL_AGENT_STAGE13_SERVICE_TOKEN"]}
            path="http://127.0.0.1:55442/api/runtime/stage13/delivery/v1"
            first=await client.post(path,json=q,headers=headers);first.raise_for_status();receipt=first.json();assert receipt["delivery_status"]=="DELIVERED"
            second=await client.post(path,json=q,headers=headers);second.raise_for_status();assert receipt==second.json()
            assert await c.fetchval("SELECT count(*) FROM stage13_controlled_delivery_sink WHERE delivery_id=$1",q["delivery_id"])==1
            save("controlled-pass-delivery",{"scope":"TEST_SCOPE","fixture":"CONTROLLED_PASS_FIXTURE","sink_writes":1,"receipt":receipt,"replay_equal":True,"http_status":first.status_code})
            save("idempotency",{"same_gate_outcome_destination":True,"main_sink_writes":1,"same_receipt":True})
        finally:
            endpoint.terminate();endpoint.wait(timeout=15);log.close()
            save("controlled-pass-process",{"pid":endpoint.pid,"exit_code":endpoint.returncode,"terminal":True})
        crashes=[]
        receipts=[receipt]
        for point in ("before_sink","after_sink_commit","after_local_receipt"):
            q=await request_for("CONTROLLED_PASS_FIXTURE-"+point)
            crashed=await launch_child(q,point);assert crashed["exit_code"]==73
            # 只过期当前 TEST_SCOPE 新 delivery 行，不修改 Runtime/历史 EvalOps truth。
            await c.execute("UPDATE stage13_deliveries SET lease_until=clock_timestamp()-interval '1 second' WHERE delivery_id=$1 AND receipt IS NULL",q["delivery_id"])
            replay=await launch_child(q);assert replay["receipt"]["delivery_status"]=="DELIVERED"
            assert await c.fetchval("SELECT count(*) FROM stage13_controlled_delivery_sink WHERE delivery_id=$1",q["delivery_id"])==1
            crashes.append({"fault_point":point,"crashed_process":crashed,"replay_process":replay,"sink_writes":1,"lease_test_acceleration":point!="after_local_receipt"})
            receipts.append(replay["receipt"])
        save("crash-replay",crashes)
        print("3 actual process crash/replay windows passed",flush=True)
        q=await request_for("CONTROLLED_PASS_FIXTURE-response-loss")
        lost=await launch_child(q,"response_loss");assert lost["receipt"]["delivery_status"]=="OUTCOME_UNKNOWN"
        replay=await launch_child(q);assert replay["receipt"]["reconciliation_status"]=="FOUND_EXISTING"
        assert await c.fetchval("SELECT count(*) FROM stage13_controlled_delivery_sink WHERE delivery_id=$1",q["delivery_id"])==1
        save("response-loss-recovery",{"lost":lost,"recovered":replay,"lookup_existing_before_write":True,"sink_writes":1})
        save("outcome-unknown",{"before_reconciliation":lost["receipt"],"after_reconciliation":replay["receipt"],"no_false_delivered":True})
        receipts.append(replay["receipt"])
        races=[]
        for count in (2,4):
            q=await request_for(f"CONTROLLED_PASS_FIXTURE-race-{count}")
            results=await asyncio.gather(*(launch_child(q) for _ in range(count)))
            assert all(v["exit_code"]==0 for v in results)
            assert any(v["receipt"]["delivery_status"]=="DELIVERED" for v in results)
            replay=await launch_child(q);assert replay["receipt"]["delivery_status"]=="DELIVERED"
            epoch=await c.fetchval("SELECT epoch FROM stage13_deliveries WHERE delivery_id=$1",q["delivery_id"])
            assert epoch==1
            assert await c.fetchval("SELECT count(*) FROM stage13_controlled_delivery_sink WHERE delivery_id=$1",q["delivery_id"])==1
            races.append({"workers":count,"processes":results,"publication_owner_count":epoch,"sink_writes":1,"replay":replay})
            receipts.append(replay["receipt"])
        save("concurrency",races);save("delivery-receipts",receipts)
        sinks=await c.fetch("SELECT payload FROM stage13_controlled_delivery_sink ORDER BY delivery_id")
        for row in sinks:
            raw=row["payload"]
            assert not any(v in raw for v in ("hidden-root-","ExpectedFailureCategory","ExpectedTicketDecision","GroundTruth","critical_evaluator_label","\"GT\""))
        save("gt-isolation",{"scope":"TEST_SCOPE","sink_rows_scanned":len(sinks),"hidden_gt_absent":True,"full_journal_copied":False})
        print("controlled PASS endpoint, response loss, 2/4 OS workers and GT isolation passed",flush=True)
    await c.close()

asyncio.run(child() if len(sys.argv)>1 and sys.argv[1]=="worker" else main())
