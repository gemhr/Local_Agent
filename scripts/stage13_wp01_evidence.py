"""WP01 controlled evidence：隔离 PG + 完整模型计数，绝不声称 real-time soak。"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
import os
from pathlib import Path
import sys
import threading
import time

import psutil
from sqlalchemy import text
from sqlalchemy.engine import make_url

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.persistence.database import Database, DatabaseConfig
from core.stage13.contracts import (
    RemoteRequest,
    WorkloadConfig,
    canonical_bytes,
    sha256,
)
from core.stage13.provider import ControlledCIProvider, ControlledProviderOperator
from core.stage13.store import ProviderStore
from core.stage13.workload import Stage13Workload


async def collect(output: Path, namespace: str):
    url = os.environ["LOCAL_AGENT_TEST_DATABASE_URL"]
    if not make_url(url).database.endswith("_test"):
        raise RuntimeError("TEST_DATABASE_REQUIRED")
    output.mkdir(parents=True, exist_ok=True)
    peak = [psutil.Process().memory_info().rss]
    stop = threading.Event()

    def sample_memory():
        process = psutil.Process()
        while not stop.wait(0.01):
            peak[0] = max(peak[0], process.memory_info().rss)

    sampler = threading.Thread(target=sample_memory, daemon=True)
    sampler.start()
    database = Database(DatabaseConfig(url=url, pool_size=8, max_overflow=0))
    try:
        generated_at = time.perf_counter()
        workload = Stage13Workload(WorkloadConfig(provider_namespace_id=namespace))
        generation_seconds = time.perf_counter() - generated_at
        gt = workload.export_hidden_gt(output / "normal-evaluator-only")
        manifest = workload.manifest
        actual_cases = Counter()
        identity_keys = set()
        model_started = time.perf_counter()
        for plan in workload.plans:
            identity_keys.add(
                workload.request(plan.index).remote_execution_business_key
            )
            for case_index in range(plan.case_count):
                actual_cases[workload.case_result(plan, case_index)["outcome"]] += 1
        assert len(identity_keys) == 9000
        assert actual_cases == {"PASS": 895500, "FAILED": 4500}
        model_check_seconds = time.perf_counter() - model_started
        storms = {}
        for profile in ("storm-10", "storm-30", "storm-50"):
            config = WorkloadConfig(
                provider_namespace_id=f"{namespace}-{profile}", profile_id=profile
            )
            storm = Stage13Workload(config)
            replay = Stage13Workload(config)
            assert storm.manifest == replay.manifest
            assert (
                storm.manifest["affected_environment_count"]
                == int(profile.split("-")[1]) * 30
            )
            assert storm.manifest["hidden_root_count"] == 3
            storm.export_hidden_gt(output / f"{profile}-evaluator-only")
            storms[profile] = {
                key: storm.manifest[key]
                for key in (
                    "affected_environment_count",
                    "failing_execution_count",
                    "failed_case_count",
                    "hidden_root_count",
                    "manifest_digest",
                )
            }
        provider = ControlledCIProvider(ProviderStore(database, workload))
        await provider.initialize()
        assert (await provider.store.counts())[
            "key_rows"
        ] == 0, "controlled run 要求新 namespace；replay 请复用原 key 的独立命令"
        receipts = {}
        submitted_at = time.perf_counter()
        with (output / "normal-evaluator-only" / "remote-submit-receipts.jsonl").open(
            "wb"
        ) as stream:
            for offset in range(0, len(workload.plans), 100):
                # 限定八个并发请求，不制造 9000 个常驻任务。
                for batch_start in range(
                    offset, min(offset + 100, len(workload.plans)), 8
                ):
                    indices = range(
                        batch_start,
                        min(batch_start + 8, offset + 100, len(workload.plans)),
                    )
                    values = await asyncio.gather(
                        *(
                            provider.submit_execution(workload.request(index))
                            for index in indices
                        )
                    )
                    for index, receipt in zip(indices, values, strict=True):
                        receipts[index] = receipt
                        stream.write(
                            canonical_bytes(receipt.model_dump(mode="json")) + b"\n"
                        )
                if offset % 1000 == 0:
                    print(
                        json.dumps(
                            {
                                "phase": "durable_submit",
                                "completed": len(receipts),
                                "expected": 9000,
                            }
                        ),
                        flush=True,
                    )
        submit_seconds = time.perf_counter() - submitted_at
        assert (
            len({receipt.remote_execution_id for receipt in receipts.values()}) == 9000
        )
        await ControlledProviderOperator(provider.store).advance_clock(20000)
        counts = Counter()
        terminal_count = 0
        max_summary_bytes = 0
        summary_started = time.perf_counter()
        with (output / "normal-evaluator-only" / "terminal-summary-digests.jsonl").open(
            "wb"
        ) as stream:
            for offset in range(0, len(workload.plans), 8):
                indices = range(offset, min(offset + 8, len(workload.plans)))
                requests = [
                    RemoteRequest(
                        remote_execution_id=receipts[index].remote_execution_id,
                        remote_execution_business_key=receipts[
                            index
                        ].remote_execution_business_key,
                        request_digest=receipts[index].request_digest,
                    )
                    for index in indices
                ]
                packets = await asyncio.gather(
                    *(provider.get_ci_summary(request) for request in requests)
                )
                for packet in packets:
                    body = json.loads(packet.content)
                    assert (
                        body["remote_state"] == "COMPLETED" and body["result_available"]
                    )
                    assert sha256(packet.content.encode("utf-8")) == packet.digest
                    terminal_count += 1
                    counts.update(body["case_counts"])
                    max_summary_bytes = max(
                        max_summary_bytes, len(packet.model_dump_json().encode("utf-8"))
                    )
                    stream.write(
                        canonical_bytes(
                            {
                                "remote_execution_id": str(packet.remote_execution_id),
                                "digest": packet.digest,
                                "evidence_id": packet.evidence_id,
                                "case_counts": body["case_counts"],
                            }
                        )
                        + b"\n"
                    )
                if offset % 1000 == 0:
                    print(
                        json.dumps(
                            {
                                "phase": "terminal_summary",
                                "completed": terminal_count,
                                "expected": 9000,
                            }
                        ),
                        flush=True,
                    )
        summary_seconds = time.perf_counter() - summary_started
        assert counts == {"PASS": 895500, "FAILED": 4500, "ERROR": 0, "SKIPPED": 0}
        async with database.session() as session:
            physical = (
                await session.execute(
                    text("""
                SELECT count(*), count(*) FILTER (WHERE jsonb_array_length(plan->'failed_cases') > 0),
                       sum((plan->>'case_count')::int), sum(jsonb_array_length(plan->'failed_cases')),
                       pg_total_relation_size('stage13_provider_keys')
                FROM stage13_provider_keys WHERE provider_namespace_id=:namespace
            """),
                    {"namespace": namespace},
                )
            ).one()
            roots = (
                await session.execute(
                    text("""
                SELECT (assignment.value->>1)::int, count(*) FROM stage13_provider_keys
                CROSS JOIN LATERAL jsonb_array_elements(plan->'failed_cases') assignment(value)
                WHERE provider_namespace_id=:namespace GROUP BY 1 ORDER BY 1
            """),
                    {"namespace": namespace},
                )
            ).all()
        assert tuple(physical[:4]) == (9000, 900, 900000, 4500)
        assert len(roots) == 24 and sum(count for _, count in roots) == 4500
        assert sum(count for root, count in roots if root < 3) == 2475
        assert sum(count for root, count in roots if 3 <= root < 10) == 1260
        assert sum(count for root, count in roots if root >= 10) == 765
        observation = {
            "classification": "CONTROLLED_EVIDENCE",
            "clock_mode": "LOGICAL_SIMULATION_TIME",
            "runtime_soak": "FULL_PROVIDER_RUNTIME_NOT_SOAKED",
            "provider_namespace_id": namespace,
            "normal_observed": {
                "environments": 3000,
                "unique_remote_executions": physical[0],
                "completed_remote_executions": terminal_count,
                "case_executions": physical[2],
                "failing_executions": physical[1],
                "failed_cases": physical[3],
                "hidden_root_causes": len(roots),
            },
            "case_model_outcomes": dict(actual_cases),
            "delivered_terminal_summary_counts": dict(counts),
            "root_distribution": {str(root): count for root, count in roots},
            "storms": storms,
            "workload_manifest_digest": manifest["manifest_digest"],
            "gt_manifest_digest": gt["gt_manifest_digest"],
            "performance": {
                "generation_wall_seconds_millis": round(generation_seconds * 1000),
                "full_case_model_check_millis": round(model_check_seconds * 1000),
                "durable_submit_millis": round(submit_seconds * 1000),
                "terminal_summary_millis": round(summary_seconds * 1000),
                "sampled_peak_rss_bytes": peak[0],
                "rss_sampling_interval_ms": 10,
                "db_rows_inserted": physical[0] + 1,
                "db_exceptional_case_entries": physical[3],
                "failure_table_rows": 0,
                "manifest_bytes": len(canonical_bytes(manifest)),
                "provider_key_relation_bytes": physical[4],
                "max_summary_envelope_bytes": max_summary_bytes,
            },
            "metrics": await provider.metrics(),
        }
        (output / "controlled-evidence.json").write_bytes(canonical_bytes(observation))
        print(
            json.dumps(
                {
                    "result": "WP01_CONTROLLED_EVIDENCE_PASS",
                    "observed": observation["normal_observed"],
                    "performance": observation["performance"],
                }
            ),
            flush=True,
        )
    finally:
        stop.set()
        sampler.join(timeout=1)
        await database.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--namespace", required=True)
    arguments = parser.parse_args()
    asyncio.run(collect(arguments.output, arguments.namespace))
