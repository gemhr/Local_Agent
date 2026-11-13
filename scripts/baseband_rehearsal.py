"""经完整后端 HUMAN HTTP 入口调查三个公开样本；不生成或读取 EO GT。"""
import json
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from uuid import uuid4
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def persisted_failure(sources):
    from dataclasses import replace
    from sqlalchemy import select
    from tests.conftest import test_database_url
    from core.settings import Settings
    from core.persistence.database import Database, DatabaseConfig
    from core.persistence.repositories.execution import DurableExecutionRepository
    from core.persistence.models import RuntimeEventJournalRow
    database = Database(DatabaseConfig.from_settings(replace(Settings.load(), database_url=test_database_url())))
    try:
        facts = []
        for source in sources:
            run_id = source["links"]["run_id"]
            image = await DurableExecutionRepository(database).load(run_id)
            if image is None:
                facts.append(dict(run_id=run_id, status="NOT_PERSISTED", model_invocations=0, committed_tools=0))
                continue
            async with database.session() as session:
                events = (await session.scalars(select(RuntimeEventJournalRow).where(RuntimeEventJournalRow.run_id == run_id,
                    RuntimeEventJournalRow.event_type == "ERROR"))).all()
            facts.append(dict(run_id=run_id, status=image.root.status, stop_reason=image.root.stop_reason,
                model_invocations=len(image.models), committed_tools=sum(t.state == "COMMITTED" for t in image.tool_invocations),
                observed_batches=sum(i.state == "OBSERVED" for i in image.loop_iterations),
                final_delivered=any((f.final_delivery_receipt or {}).get("status") == "DELIVERED" for f in image.loop_frames),
                request_counting_reliability="UNAVAILABLE", budget_limits=image.root.budget_totals,
                safe_error_codes=[json.loads(e.safe_payload).get("safe_error_code") for e in events]))
        return facts
    finally:
        await database.dispose()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    child = subprocess.Popen([sys.executable, "-u", str(ROOT / "scripts/baseband_rehearsal_server.py")], cwd=ROOT,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    errors = []
    threading.Thread(target=lambda: [errors.append(line) for line in child.stderr], daemon=True).start()
    joint = os.getenv("BASEBAND_INPUT_VERSION") == "v2"
    path = ROOT / (".ai/handoff/stage21_baseband_agents/oracle_real_attempt.json" if joint else ".ai/handoff/stage21_baseband_agents/baseband_real_attempt.json")
    evidence = json.loads(path.read_text(encoding="utf-8")) if joint and os.getenv("BASEBAND_SOURCE_REPLAY") else dict(real_agent="NOT_EXECUTED", human_http="NOT_EXECUTED", sources=[])
    try:
        live = None
        for line in child.stdout:
            if line.startswith("BASEBAND_PRIVATE:"):
                live = json.loads(line.split(":", 1)[1]); break
        if live is None:
            evidence["blocker"] = "BASEBAND_BACKEND_START_FAILED"
            # 不输出可能含有配置路径/凭据的原始异常。
            evidence["startup_errors"] = [line.strip() for line in errors if "Error:" in line and "postgres" not in line.lower() and "password" not in line.lower()][-3:]
            return
        evidence["origins"] = live["sources"]
        path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        base = live["base_url"]
        threading.Thread(target=lambda: [None for _ in child.stdout], daemon=True).start()
        headers = {"Authorization": "Bearer " + live["human_token"]}
        for source in live["sources"]:
            only = os.getenv("BASEBAND_CASE_KEYS", "").split(",")
            if only != [""] and source["case_key"] not in only:
                continue
            if source["case_key"] in os.getenv("BASEBAND_FRESH_CASE_KEYS", "").split(","):
                response = requests.post(base + "/api/ci/scenarios", json=dict(scenario_key=source["case_key"], request_id=str(uuid4())), headers=headers, timeout=15)
                response.raise_for_status()
                instance = response.json()
                response = requests.post(base + f'/api/ci/scenarios/{instance["instance_id"]}/executions', json=dict(request_id=(fresh_request := str(uuid4()))), headers=headers, timeout=15)
                response.raise_for_status(); fresh = response.json()
                source = dict(source, incident_id=fresh["incident_id"], execution_id=fresh["execution_id"], instance_id=instance["instance_id"], execution_request_id=fresh_request)
                evidence["origins"] = [source if x["case_key"]==source["case_key"] else x for x in evidence["origins"]]
            response = requests.post(base + f'/api/ci/scenarios/{source["instance_id"]}/executions', json={"request_id": source["execution_request_id"]}, headers=headers, timeout=15)
            if response.status_code != 200 and response.status_code != 201:
                evidence["blocker"] = dict(status=response.status_code, response=response.json())
            response.raise_for_status()
            execution = response.json()
            response = requests.post(base + f'/api/ci/incidents/{source["incident_id"]}/investigations', json={"request_id": str(uuid4())}, headers=headers, timeout=15)
            response.raise_for_status(); links = response.json()
            deadline = time.monotonic() + 200
            while True:
                response = requests.get(base + links["report_url"], headers=headers, timeout=15)
                response.raise_for_status(); report = response.json()
                if report["investigation_status"] in {"COMPLETED", "FAILED", "UNCONFIRMED"} or time.monotonic() >= deadline:
                    break
                time.sleep(.5)
            item = dict(case_key=source["case_key"], origin=source, execution=execution, links=links, result=report)
            previous = next((i for i in evidence["sources"] if i["case_key"] == source["case_key"]), None)
            if previous:
                evidence.setdefault("previous_attempts", []).append(previous)
                evidence["sources"].remove(previous)
            evidence["sources"].append(item)
            path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
            print(source["case_key"], report["investigation_status"], report.get("safe_error_code"), flush=True)
            if report["investigation_status"] != "COMPLETED":
                evidence["runtime_errors"] = [line.strip() for line in errors if any(marker in line for marker in ("Error:", "Exception:", "CONTEXT_BUDGET", "RuntimeError")) and "password" not in line.lower() and "api_key" not in line.lower()][-5:]
                continue
        evidence["human_http"] = "PASS"
        evidence["real_agent"] = "PASS" if len(evidence["sources"]) == (4 if joint else 3) and all(s["result"]["investigation_status"] == "COMPLETED" for s in evidence["sources"]) else "FAIL"
        if evidence.get("human_regression", {}).get("status") == "PASS":
            return
        source = next(s for s in live["sources"] if s["case_key"] == ("oracle_reference_scope" if joint else "baseband_input_mismatch"))
        prefix = base + f'/api/ci/incidents/{source["incident_id"]}'
        def call(path, body=None):
            response = requests.get(prefix + path, headers=headers, timeout=15) if body is None else requests.post(prefix + path, json=body, headers=headers, timeout=15)
            response.raise_for_status(); return response.json()
        before = call("")
        action = call("/actions", dict(request_id=str(uuid4()), action_kind="SELECT_BASEBAND_REFERENCE" if joint else "ALIGN_BASEBAND_INPUT", parameters={"reference_id":"integer-reference", "version":"2"} if joint else {"rule_version":"1"}))
        confirmed = call(f'/actions/{action["action_id"]}/confirm', dict(confirmed=True, preview_digest=action["preview_digest"]))
        single = call("/verifications", dict(request_id=str(uuid4()), mode="SINGLE", case_ids=["integer-primary" if joint else "sampling-primary"], expected_state_revision=2))
        group = call("/verifications", dict(request_id=str(uuid4()), mode="GROUP", case_ids=["integer-primary", "integer-control"] if joint else ["sampling-primary", "sampling-control"], expected_state_revision=2))
        closed = call("/close", dict(request_id=str(uuid4()), expected_state_revision=2, action_id=action["action_id"],
            single_verification_id=single["verification_id"], group_verification_id=group["verification_id"],
            root_cause="参考版本适用范围不匹配" if joint else "DDT/KDT 采样率不一致", fix_confirmation="HUMAN 明确选择适用参考并复测" if joint else "HUMAN 确认采样率对齐", regression_confirmed=True))
        after = call("")
        assert before["execution"] == after["execution"] and before["evidence"] == after["evidence"]
        assert single["outcome"] == group["outcome"] == "PASS"
        evidence["human_regression"] = dict(status="PASS", action=action, confirmed=confirmed,
            single=single, group=group, closed=closed, original_unchanged=True)
    finally:
        if child.stdin:
            child.stdin.close()
        try:
            child.wait(timeout=45)
        except subprocess.TimeoutExpired:
            child.terminate(); child.wait(timeout=15)
        evidence["runtime_facts"] = asyncio.run(persisted_failure(evidence["sources"])) if evidence["sources"] else []
        path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({k: v for k, v in evidence.items() if k not in {"sources", "human_regression", "previous_attempts", "origins"}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
