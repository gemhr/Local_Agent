"""WP03 受控采证；normal 消费 WP02 副本，storm 复用同一 Provider/Guardian 路径。"""

import argparse
import asyncio
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import sys
import time

import httpx
import psutil
from sqlalchemy import func, select, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.persistence.database import Database, DatabaseConfig
from core.stage13.contracts import WorkloadConfig, canonical_bytes, sha256
from core.stage13.guardian import GuardianScheduleService
from core.stage13.guardian_worker import GuardianWorker, build_guardian_invoker
from core.stage13.guardian_models import CycleRow, GuardianRow, VersionRow
from core.stage13.http import create_provider_app
from core.stage13.incident import IncidentAggregationService
from core.stage13.incident_models import (
    AnalysisJobRow,
    ClusterRow,
    CollectionRow,
    IncidentRow,
    MembershipRow,
    RevisionRow,
    INCIDENT_TABLES,
)
from core.stage13.incident_worker import IncidentWorker
from core.stage13.provider import ControlledCIProvider, ControlledProviderOperator
from core.stage13.store import NamespaceRow, ProviderStore, ProviderKeyRow
from core.stage13.workload import Stage13Workload


def save(name, value):
    folder = ROOT / ".ai/handoff/stage13_wp03/evidence"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


async def collect(profile):
    url = os.environ["LOCAL_AGENT_TEST_DATABASE_URL"]
    if not url.rsplit("/", 1)[-1].endswith("_test"):
        raise ValueError("只允许隔离 _test DB")
    db = Database(DatabaseConfig(url=url, pool_size=24, max_overflow=0))
    base = datetime(2026, 10, 5, 16, tzinfo=UTC)
    if profile == "normal":
        async with db.session() as s:
            namespace = (await s.execute(select(NamespaceRow))).scalar_one()
            config = WorkloadConfig.model_validate(namespace.config)
            assert (
                await s.execute(select(func.count()).select_from(VersionRow))
            ).scalar_one() == 9000
            seconds = namespace.logical_time
    else:
        config = WorkloadConfig(
            provider_namespace_id=f"wp03-{profile}-20261006", profile_id=profile
        )
        seconds = 0
    workload = Stage13Workload(config)
    provider = ControlledCIProvider(ProviderStore(db, workload))
    scheduler = GuardianScheduleService(db, config.owner_scope_id)
    operator = ControlledProviderOperator(provider.store)
    service = IncidentAggregationService(db, config.owner_scope_id)
    peak = 0

    async def clock(value):
        await scheduler.advance_clock(base + timedelta(seconds=value))
        await operator.advance_clock(value)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=create_provider_app(provider, agent_token="wp03-controlled-only")
        ),
        base_url="http://controlled-ci",
        headers={"Authorization": "Bearer wp03-controlled-only"},
    ) as client:
        invoker = build_guardian_invoker(
            db, client, config, owner_id="wp03-evidence", failure_details=True
        )
        guardian = GuardianWorker(scheduler, invoker, enabled=True)
        worker = IncidentWorker(service, invoker, enabled=True)
        try:
            await provider.initialize()
            if profile != "normal":
                await scheduler.initialize(logical_now=base)
                # 全部 3000 环境都经过真实 WP02 discovery/dispatch/binding/poll。
                for index in range(config.environment_count):
                    env = workload.environment(index)
                    gid = await scheduler.register_guardian(
                        config.automation_project_id,
                        config.suite_id,
                        env["environment_id"],
                        env["channel_group"],
                    )
                    await scheduler.discover(
                        gid,
                        provider_namespace=config.provider_namespace_id,
                        versions=config.product_versions,
                        expected_cases=tuple(
                            p.case_count
                            for p in workload.plans[index * 3 : index * 3 + 3]
                        ),
                        plan_revision=config.generator_version,
                    )
                print(profile, "3000 real Guardians/Cycles discovered", flush=True)
                seconds = 480
                await clock(seconds)
                while True:
                    await guardian.tick()
                    seconds += 1
                    await clock(seconds)
                    async with db.session() as s:
                        active = (
                            await s.execute(
                                select(func.count())
                                .select_from(VersionRow)
                                .where(VersionRow.status == "ACTIVE")
                            )
                        ).scalar_one()
                    if active == 3000:
                        break
                for ordinal in range(1, 4):
                    seconds += 8500
                    for step in range(1500):
                        await clock(seconds)
                        await guardian.tick()
                        seconds += 1
                        if step % 50 == 49:
                            async with db.session() as s:
                                states = dict(
                                    (
                                        await s.execute(
                                            select(
                                                VersionRow.status, func.count()
                                            ).group_by(VersionRow.status)
                                        )
                                    ).all()
                                )
                            print(profile, ordinal, states, flush=True)
                            if states.get("COMPLETED", 0) == 3000 * ordinal and (
                                ordinal == 3 or states.get("ACTIVE", 0) == 3000
                            ):
                                break
                    else:
                        raise AssertionError("storm WP02 progression 未闭合")
                await clock(seconds)
            aggregation_started = time.monotonic()
            ticks = 0
            while await worker.tick():
                ticks += 1
                seconds += 1
                await clock(seconds)
                peak = max(peak, psutil.Process().memory_info().rss)
                if ticks % 100 == 0:
                    print(profile, "aggregation batches", ticks, flush=True)
            await service.seal(
                config.automation_project_id, config.suite_id, config.business_date
            )
            seconds += 600
            await clock(seconds)
            admission_started = time.monotonic()
            admission = await service.admit(
                config.automation_project_id, config.suite_id, config.business_date
            )
            admission_wall = time.monotonic() - admission_started
            async with db.session() as s:
                versions = (await s.execute(select(VersionRow))).scalars().all()
                clusters = (await s.execute(select(ClusterRow))).scalars().all()
                incidents = (await s.execute(select(IncidentRow))).scalars().all()
                jobs = (await s.execute(select(AnalysisJobRow))).scalars().all()
                collections = (await s.execute(select(CollectionRow))).scalars().all()
                member_count = (
                    await s.execute(select(func.count()).select_from(MembershipRow))
                ).scalar_one()
                row_counts = {
                    name: (
                        await s.execute(text(f"SELECT count(*) FROM {name}"))
                    ).scalar_one()
                    for name in INCIDENT_TABLES
                }
            failed = sum(v.counts["FAILED"] for v in versions)
            errors = sum(v.counts["ERROR"] for v in versions)
            failing = sum(v.counts["FAILED"] + v.counts["ERROR"] > 0 for v in versions)
            expected = {
                "normal": 4500,
                "storm-10": 2700,
                "storm-30": 8100,
                "storm-50": 13500,
            }[profile]
            assert (
                failed == expected
                and errors == 0
                and member_count == failed
                and len(versions) == 9000
            )
            assert len(collections) == failing and all(
                c.state == "COMPLETE" for c in collections
            )
            assert admission["admitted_count"] <= 60
            assert all(i.state == "SEALED" for i in incidents)
            assert provider.counters["artifact_reads"] == 0
            assert all(
                j.status == "READY" or j.status == "DEFERRED_BUDGET" for j in jobs
            )
            detail_bytes = sum(
                len(p["content"].encode()) for c in collections for p in c.pages
            )
            representatives = sum(
                i.draft["failure_summary"]["represented_members"] for i in incidents
            )
            cluster_histogram = Counter()
            for cluster in clusters:
                cluster_histogram[cluster.version_id] += 1
            result = {
                "evidence_class": "CONTROLLED_EVIDENCE",
                "time_mode": "LOGICAL_SIMULATION_TIME",
                "profile": profile,
                "manifest_digest": workload.manifest["manifest_digest"],
                "failed_cases": failed,
                "error_cases": errors,
                "failing_executions": failing,
                "local_clusters": len(clusters),
                "global_incidents": len(incidents),
                "eligible_jobs": admission["eligible_count"],
                "admitted_jobs": admission["admitted_count"],
                "deferred_jobs": admission["deferred_count"],
                "admission": admission,
                "representative_evidence_count": representatives,
                "detail_bytes": detail_bytes,
                "summary_reads_added_by_wp03": (
                    provider.counters["summary_reads"] if profile == "normal" else None
                ),
                "detail_reads": provider.counters["detail_reads"],
                "artifact_reads": provider.counters["artifact_reads"],
                "provider_counters": dict(provider.counters),
                "aggregation_wall_seconds": time.monotonic() - aggregation_started,
                "admission_transaction_wall_seconds": admission_wall,
                "sampled_peak_rss_bytes": peak,
                "db_rows": row_counts,
                "clusters_per_execution_histogram": dict(
                    Counter(cluster_histogram.values())
                ),
                "ratios": {
                    "failed_case_to_cluster_ratio": failed / len(clusters),
                    "cluster_to_incident_ratio": len(clusters) / len(incidents),
                    "failed_case_to_incident_ratio": failed / len(incidents),
                    "incident_to_analysis_job_ratio": len(incidents)
                    / admission["admitted_count"],
                    "failed_case_to_analysis_job_ratio": failed
                    / admission["admitted_count"],
                },
                "model_calls": 0,
                "runtime_runs": 0,
                "repair_runs": 0,
                "hidden_gt_runtime_reads": 0,
                "capacity_claim": False,
                "production_validation": False,
            }
            save(
                "normal-compression.json" if profile == "normal" else profile + ".json",
                result,
            )
            if profile == "normal":
                save(
                    "local-clusters.json",
                    [
                        {
                            "cluster_id": c.cluster_id,
                            "cluster_key": c.cluster_key,
                            "version_id": c.version_id,
                            "incident_id": c.incident_id,
                            "signature": c.signature,
                            "components": c.components,
                        }
                        for c in clusters
                    ],
                )
                save(
                    "global-incidents.json",
                    [
                        {
                            "incident_id": i.incident_id,
                            "incident_key": i.incident_key,
                            "state": i.state,
                            "signature": i.signature,
                            "components": i.components,
                            "first_seen_at": i.first_seen_at,
                            "last_seen_at": i.last_seen_at,
                            "summary": i.draft["failure_summary"],
                        }
                        for i in incidents
                    ],
                )
                save(
                    "representative-evidence.json",
                    [
                        {
                            "incident_id": i.incident_id,
                            "input": j.input,
                            "input_digest": j.input_digest,
                            "bytes": len(canonical_bytes(j.input)),
                        }
                        for i in incidents
                        for j in jobs
                        if i.incident_id == j.incident_id
                    ],
                )
                save("analysis-admission.json", admission)
                # GT 关系只在采证 evaluator-side 计算，不进入任何产品服务。
                relations = defaultdict(set)
                async with db.session() as s:
                    membership = (
                        (await s.execute(select(MembershipRow))).scalars().all()
                    )
                    provider_rows = {
                        r.remote_execution_id: r
                        for r in (await s.execute(select(ProviderKeyRow)))
                        .scalars()
                        .all()
                    }
                by_version = {v.version_execution_id: v for v in versions}
                for m in membership:
                    source = provider_rows[by_version[m.version_id].remote_id]
                    private = dict(source.plan["failed_cases"])
                    index = int(m.case_id.split("-")[-1])
                    relations[m.incident_id].add(private[index])
                save(
                    "evaluator-only-incident-root-relationship.json",
                    {
                        "boundary": "EVALUATOR_ONLY_POST_HOC_NO_RUNTIME_OR_ADMISSION_USE",
                        "incident_roots": {k: sorted(v) for k, v in relations.items()},
                        "hidden_roots_observed": len(set().union(*relations.values())),
                    },
                )
            print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
        finally:
            await worker.close()
            await guardian.close()
            await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--profile",
        choices=["normal", "storm-10", "storm-30", "storm-50"],
        default="normal",
    )
    asyncio.run(collect(parser.parse_args().profile))
