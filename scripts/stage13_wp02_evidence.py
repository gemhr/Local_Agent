"""3000 Cycle × 三版本：真实 dispatch/binding/poll/progression，逻辑时间采证。"""

import argparse
import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import sys
import time

import httpx
import psutil
from sqlalchemy import func, select, text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.persistence.database import Database, DatabaseConfig
from core.stage13.contracts import WorkloadConfig
from core.stage13.guardian import GuardianScheduleService, hash_offset, poll_jitter
from core.stage13.guardian_models import (
    CycleRow,
    DueRow,
    GuardianRow,
    ObservationRow,
    OccupancyRow,
    VersionRow,
)
from core.stage13.guardian_worker import GuardianWorker, build_guardian_invoker
from core.stage13.http import create_provider_app
from core.stage13.provider import ControlledCIProvider, ControlledProviderOperator
from core.stage13.store import ProviderStore
from core.stage13.workload import Stage13Workload


async def collect(args):
    url = os.environ["LOCAL_AGENT_TEST_DATABASE_URL"]
    if not url.rsplit("/", 1)[-1].endswith("_test"):
        raise ValueError("只允许隔离 _test DB")
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)

    def save(name, value):
        (root / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    config = WorkloadConfig(
        provider_namespace_id=args.namespace, environment_count=args.environments
    )
    workload = Stage13Workload(config)
    base = datetime(2026, 10, 5, 16, tzinfo=UTC)
    database = Database(DatabaseConfig(url=url, pool_size=24, max_overflow=0))
    provider = ControlledCIProvider(ProviderStore(database, workload))
    service = GuardianScheduleService(database, config.owner_scope_id)
    operator = ControlledProviderOperator(provider.store)
    started = time.monotonic()
    peak = 0
    max_batch = claims = scans = 0

    async def clock(seconds):
        await service.advance_clock(base + timedelta(seconds=seconds))
        await operator.advance_clock(seconds)

    async def counts():
        async with database.session() as session:
            return dict(
                (
                    await session.execute(
                        select(VersionRow.status, func.count()).group_by(
                            VersionRow.status
                        )
                    )
                ).all()
            )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=create_provider_app(provider, agent_token="wp02-full-controlled-only")
        ),
        base_url="http://controlled-ci",
        headers={"Authorization": "Bearer wp02-full-controlled-only"},
    ) as client:
        worker = GuardianWorker(
            service,
            build_guardian_invoker(
                database, client, config, owner_id="wp02-full-worker"
            ),
            enabled=True,
        )

        async def tick():
            nonlocal peak, max_batch, claims, scans
            n = await worker.tick()
            max_batch = max(max_batch, n)
            claims += n
            scans += 1
            peak = max(peak, psutil.Process().memory_info().rss)

        try:
            await provider.initialize()
            await service.initialize(logical_now=base)
            async with database.session() as session:
                existing = (
                    await session.execute(select(func.count()).select_from(CycleRow))
                ).scalar_one()
            if existing:
                raise ValueError("full evidence 需要新隔离 DB，禁止复用旧 cohort")
            for i in range(config.environment_count):
                env = workload.environment(i)
                gid = await service.register_guardian(
                    config.automation_project_id,
                    config.suite_id,
                    env["environment_id"],
                    env["channel_group"],
                )
                await service.discover(
                    gid,
                    provider_namespace=config.provider_namespace_id,
                    versions=config.product_versions,
                    expected_cases=tuple(
                        p.case_count for p in workload.plans[i * 3 : i * 3 + 3]
                    ),
                    plan_revision=config.generator_version,
                )
                if (i + 1) % 500 == 0:
                    print(f"discovery: {i + 1} guardians / cycles", flush=True)
            async with database.session() as session:
                versions = (
                    (
                        await session.execute(
                            select(VersionRow).order_by(
                                VersionRow.cycle_id, VersionRow.ordinal
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                due = (await session.execute(select(DueRow))).scalars().all()
            offset_buckets = Counter(
                int((d.original_due_at - base).total_seconds()) for d in due
            )
            assert (
                len(versions) == config.environment_count * 3
                and len(due) == config.environment_count
            )
            assert min(offset_buckets) >= 0 and max(offset_buckets) < 480
            jitter = Counter(
                poll_jitter(v.version_execution_key, 1)
                for v in versions
                if v.ordinal == 1
            )
            save(
                "poll-jitter-full.json",
                {
                    "count": config.environment_count,
                    "buckets": dict(sorted(jitter.items())),
                    "range": [min(jitter), max(jitter)],
                    "deterministic": True,
                },
            )
            save(
                "identity-initial.json",
                {
                    "guardians": config.environment_count,
                    "cycles": config.environment_count,
                    "versions": len(versions),
                    "v1_due": len(due),
                    "offset_buckets": dict(sorted(offset_buckets.items())),
                },
            )
            print("V1: bounded nightly dispatch", flush=True)
            for seconds in range(600):
                await clock(seconds)
                await tick()
                if seconds % 120 == 119:
                    print(f"logical {seconds}s: {await counts()}", flush=True)
            initial = await counts()
            assert initial.get("ACTIVE") == config.environment_count, initial
            # 3000 overdue poll 的恢复 generation 与分布，在真实 active bindings 上验证。
            seconds = 1000
            await clock(seconds)
            while await service.recover("full-scheduler-restart-1"):
                pass
            async with database.session() as session:
                recovered_due = (
                    (
                        await session.execute(
                            select(DueRow).where(
                                DueRow.operation == "POLL_REMOTE",
                                DueRow.completed_at.is_(None),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            spread = Counter(
                int(
                    (
                        d.next_available_at - (base + timedelta(seconds=seconds))
                    ).total_seconds()
                )
                for d in recovered_due
            )
            before = {d.work_key: d.next_available_at for d in recovered_due}
            assert (
                len(before) == config.environment_count
                and min(spread) >= 0
                and max(spread) <= 300
                and len(spread) > 1
            )
            assert await service.recover("full-scheduler-restart-1") == 0
            async with database.session() as session:
                unchanged = (
                    (
                        await session.execute(
                            select(DueRow).where(DueRow.work_key.in_(before))
                        )
                    )
                    .scalars()
                    .all()
                )
            assert all(d.next_available_at == before[d.work_key] for d in unchanged)
            save(
                "restart-spread.json",
                {
                    "count": len(before),
                    "generation": 1,
                    "min": min(spread),
                    "max": max(spread),
                    "buckets": dict(sorted(spread.items())),
                    "repeat_scan_moves": 0,
                },
            )
            # 重建 worker/application owner；business identity 来自同一数据库。
            await worker.close()
            service = GuardianScheduleService(database, config.owner_scope_id)
            worker = GuardianWorker(
                service,
                build_guardian_invoker(
                    database, client, config, owner_id="wp02-full-restarted-worker"
                ),
                enabled=True,
            )
            for seconds in range(1000, 1361):
                await clock(seconds)
                await tick()
            async with database.session() as session:
                fresh = (
                    await session.execute(
                        select(func.count())
                        .select_from(VersionRow)
                        .where(
                            VersionRow.ordinal == 1,
                            VersionRow.last_successful_poll_at
                            >= base + timedelta(seconds=1000),
                        )
                    )
                ).scalar_one()
            assert fresh == config.environment_count
            print(f"restart: {fresh} fresh observations (logical time)", flush=True)
            for ordinal in range(1, 4):
                seconds += 8500
                print(
                    f"V{ordinal}: logical terminal collection / successor dispatch",
                    flush=True,
                )
                for step in range(1000):
                    await clock(seconds)
                    await tick()
                    seconds += 1
                    if step % 50 == 49:
                        state_counts = await counts()
                        print(f"logical {seconds}s: {state_counts}", flush=True)
                        completed = state_counts.get("COMPLETED", 0)
                        if completed == config.environment_count * ordinal and (
                            ordinal == 3
                            or state_counts.get("ACTIVE", 0) == config.environment_count
                        ):
                            break
                else:
                    raise AssertionError(
                        f"V{ordinal} progression 未收敛: {await counts()}"
                    )
            async with database.session() as session:
                versions = (
                    (
                        await session.execute(
                            select(VersionRow).order_by(
                                VersionRow.cycle_id, VersionRow.ordinal
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                cycles = (await session.execute(select(CycleRow))).scalars().all()
                due = (await session.execute(select(DueRow))).scalars().all()
                terminal_counts = Counter(c.status for c in cycles)
                changed = (
                    await session.execute(
                        select(func.count())
                        .select_from(ObservationRow)
                        .where(ObservationRow.changed)
                    )
                ).scalar_one()
                successful_reads = (
                    await session.execute(
                        select(func.count()).select_from(ObservationRow)
                    )
                ).scalar_one()
                occupied = (
                    await session.execute(
                        select(func.count())
                        .select_from(OccupancyRow)
                        .where(OccupancyRow.cycle_id.is_not(None))
                    )
                ).scalar_one()
                row_counts = {}
                for model in (
                    GuardianRow,
                    CycleRow,
                    VersionRow,
                    DueRow,
                    ObservationRow,
                    OccupancyRow,
                ):
                    row_counts[model.__tablename__] = (
                        await session.execute(select(func.count()).select_from(model))
                    ).scalar_one()
                # EXPLAIN 保留实际 planner 选择；小表 planner 可选择 seq scan，不强迫索引。
                explain = (
                    await session.execute(
                        text(
                            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT work_key FROM stage13_due_work WHERE scope=:scope AND completed_at IS NULL AND next_available_at <= :now AND (state='READY' OR (state='CLAIMED' AND lease_until <= clock_timestamp())) ORDER BY next_available_at,work_key LIMIT 100"
                        ),
                        {
                            "scope": config.owner_scope_id,
                            "now": base + timedelta(seconds=seconds),
                        },
                    )
                ).scalar_one()
                index = (
                    await session.execute(
                        text(
                            "SELECT indexdef FROM pg_indexes WHERE indexname='ix_s13_due_candidate'"
                        )
                    )
                ).scalar_one()
            remote_ids = {v.remote_id for v in versions}
            assert None not in remote_ids and len(remote_ids) == len(versions)
            serial_violations = 0
            for i in range(0, len(versions), 3):
                triplet = versions[i : i + 3]
                assert [v.ordinal for v in triplet] == [1, 2, 3]
                serial_violations += sum(
                    triplet[j + 1].intent_at < triplet[j].completed_at for j in (0, 1)
                )
            assert serial_violations == 0 and occupied == 0
            expected_terminal = Counter(
                (
                    "COMPLETED_WITH_FAILURES"
                    if any(
                        p.failed_cases or p.error_cases
                        for p in workload.plans[i * 3 : i * 3 + 3]
                    )
                    else "SUCCEEDED"
                )
                for i in range(config.environment_count)
            )
            assert terminal_counts == expected_terminal
            total_counts = {
                k: sum(v.counts[k] for v in versions)
                for k in ("PASS", "FAILED", "ERROR", "SKIPPED")
            }
            assert sum(total_counts.values()) == config.environment_count * 300
            assert total_counts["FAILED"] == workload.manifest["failed_case_count"]
            submit_buckets = Counter(
                int((d.business_started_at - base).total_seconds())
                for d in due
                if d.operation == "DISPATCH_VERSION" and d.started_at
            )
            read_buckets = Counter(
                int((d.business_started_at - base).total_seconds())
                for d in due
                if d.operation != "DISPATCH_VERSION" and d.started_at
            )
            assert (
                max(submit_buckets.values()) <= 10 and max(read_buckets.values()) <= 20
            )
            profile = {
                "verdict": "WP02_CONTROLLED_LOGICAL_PROFILE_PASS",
                "evidence_class": "CONTROLLED_EVIDENCE",
                "time_mode": "LOGICAL_SIMULATION_TIME",
                "guardians": config.environment_count,
                "daily_cycles": len(cycles),
                "versions": len(versions),
                "unique_remote_bindings": len(remote_ids),
                "provider_counts": await provider.store.counts(),
                "cycle_terminal_counts": terminal_counts,
                "expected_terminal_counts": expected_terminal,
                "case_counts": total_counts,
                "serial_violations": serial_violations,
                "active_occupancy": occupied,
                "rows": row_counts,
                "due_backlog": sum(d.completed_at is None for d in due),
                "max_batch": max_batch,
                "claim_count": claims,
                "due_scans": scans,
                "lease_takeovers": sum(d.takeovers for d in due),
                "poll_requests": sum(v.poll_requests for v in versions),
                "successful_polls": sum(v.successful_polls for v in versions),
                "successful_reads": successful_reads,
                "changed_observations": changed,
                "reconciliation_requests": sum(
                    v.reconciliation_requests for v in versions
                ),
                "dispatch_attempts": sum(v.submit_attempts for v in versions),
                "submit_amplification": provider.counters["submit_attempts"]
                / len(remote_ids),
                "poll_amplification": provider.counters["summary_reads"]
                / len(remote_ids),
                "provider_counters": dict(provider.counters),
                "max_submit_starts_per_logical_second": max(submit_buckets.values()),
                "max_read_starts_per_logical_second": max(read_buckets.values()),
                "logical_elapsed_seconds": seconds,
                "wall_seconds": round(time.monotonic() - started, 3),
                "peak_rss_bytes": peak,
                "model_calls": 0,
                "seed": config.seed,
                "manifest_digest": workload.manifest["manifest_digest"],
                "network_transport": "ASGITransport_real_WP01_application",
                "restart_fresh_observation_count": fresh,
                "measured_controlled_capacity": False,
            }
            save("logical-full-profile.json", profile)
            save(
                "identity-binding-check.json",
                {
                    "cycles": len(cycles),
                    "each_cycle_versions": 3,
                    "versions": len(versions),
                    "unique_bindings": len(remote_ids),
                    "serial_violations": serial_violations,
                    "binding_source": "real_governed_dispatch_receipt_poll_terminal_progression",
                    "bulk_binding_insert": False,
                },
            )
            save(
                "due-query-plan.json",
                {"candidate_index": index, "explain_analyze_buffers": explain},
            )
            print(json.dumps(profile, ensure_ascii=False), flush=True)
        finally:
            await worker.close()
            await database.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=".ai/handoff/stage13_wp02/evidence/full")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--environments", type=int, default=3000)
    asyncio.run(collect(parser.parse_args()))
