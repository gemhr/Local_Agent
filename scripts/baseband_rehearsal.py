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
    evidence = dict(real_agent="NOT_EXECUTED", human_http="NOT_EXECUTED", sources=[])
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
        base = live["base_url"]
        threading.Thread(target=lambda: [None for _ in child.stdout], daemon=True).start()
        headers = {"Authorization": "Bearer " + live["human_token"]}
        for source in live["sources"]:
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
            evidence["sources"].append(dict(case_key=source["case_key"], execution=execution, links=links, result=report))
            print(source["case_key"], report["investigation_status"], report.get("safe_error_code"), flush=True)
            if report["investigation_status"] != "COMPLETED":
                evidence["runtime_errors"] = [line.strip() for line in errors if any(marker in line for marker in ("Error:", "Exception:", "CONTEXT_BUDGET", "RuntimeError")) and "password" not in line.lower() and "api_key" not in line.lower()][-5:]
                break
        evidence["human_http"] = "PASS"
        evidence["real_agent"] = "PASS" if len(evidence["sources"]) == 3 and all(s["result"]["investigation_status"] == "COMPLETED" for s in evidence["sources"]) else "FAIL"
        source = live["sources"][0]
        prefix = base + f'/api/ci/incidents/{source["incident_id"]}'
        def call(path, body=None):
            response = requests.get(prefix + path, headers=headers, timeout=15) if body is None else requests.post(prefix + path, json=body, headers=headers, timeout=15)
            response.raise_for_status(); return response.json()
        before = call("")
        action = call("/actions", dict(request_id=str(uuid4()), action_kind="ALIGN_BASEBAND_INPUT", parameters={"rule_version":"1"}))
        confirmed = call(f'/actions/{action["action_id"]}/confirm', dict(confirmed=True, preview_digest=action["preview_digest"]))
        single = call("/verifications", dict(request_id=str(uuid4()), mode="SINGLE", case_ids=["sampling-primary"], expected_state_revision=2))
        group = call("/verifications", dict(request_id=str(uuid4()), mode="GROUP", case_ids=["sampling-primary", "sampling-control"], expected_state_revision=2))
        closed = call("/close", dict(request_id=str(uuid4()), expected_state_revision=2, action_id=action["action_id"],
            single_verification_id=single["verification_id"], group_verification_id=group["verification_id"],
            root_cause="DDT/KDT 采样率不一致", fix_confirmation="HUMAN 确认采样率对齐", regression_confirmed=True))
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
        path = ROOT / ".ai/handoff/stage21_baseband_agents/baseband_real_attempt.json"
        path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({k: v for k, v in evidence.items() if k not in {"sources", "human_regression"}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
