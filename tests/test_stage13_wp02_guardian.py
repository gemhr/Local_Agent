"""WP02：真实 PG + WP01 Provider + governed tools 的业务状态与恢复验证。"""

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select, text, update

from core.persistence.errors import PersistenceError
from core.stage13.contracts import LookupRequest, RemoteRequest, WorkloadConfig
from core.stage13.guardian import (
    GuardianScheduleService,
    StaleClaim,
    eligibility,
    poll_jitter,
)
from core.stage13.guardian_models import (
    CycleRow,
    DueRow,
    GuardianRow,
    ObservationRow,
    OccupancyRow,
    SchedulerRow,
    VersionRow,
)
from core.stage13.guardian_worker import GuardianWorker, build_guardian_invoker
from core.stage13.http import create_provider_app
from core.stage13.provider import (
    ControlledCIProvider,
    ControlledFaults,
    ControlledProviderOperator,
)
from core.stage13.store import ProviderStore
from core.stage13.workload import Stage13Workload

BASE = datetime(2026, 10, 5, 16, tzinfo=UTC)
EVIDENCE = Path(__file__).resolve().parents[1] / ".ai/handoff/stage13_wp02/evidence"


def evidence(name, value):
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


class Cohort:
    def __init__(self, database, *, faults=None, count=100):
        self.database = database
        self.config = WorkloadConfig(
            provider_namespace_id=f"wp02-{uuid4().hex}", environment_count=count
        )
        self.workload = Stage13Workload(self.config)
        self.provider = ControlledCIProvider(
            ProviderStore(database, self.workload), faults=faults
        )
        self.operator = ControlledProviderOperator(self.provider.store)
        self.service = GuardianScheduleService(database, self.config.owner_scope_id)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=create_provider_app(self.provider, agent_token="wp02-test-only")
            ),
            base_url="http://controlled-ci",
            headers={"Authorization": "Bearer wp02-test-only"},
        )
        self.worker = GuardianWorker(
            self.service,
            build_guardian_invoker(
                database, self.client, self.config, owner_id=f"wp02-{uuid4()}"
            ),
            enabled=True,
        )

    async def initialize(self):
        await self.provider.initialize()
        await self.service.initialize(logical_now=BASE)
        return self

    async def clock(self, seconds):
        await self.service.advance_clock(BASE + timedelta(seconds=seconds))
        await self.operator.advance_clock(seconds)

    async def discover(self, index=0, *, suite=None):
        env = self.workload.environment(index)
        guardian_id = await self.service.register_guardian(
            self.config.automation_project_id,
            suite or self.config.suite_id,
            env["environment_id"],
            env["channel_group"],
        )
        counts = tuple(
            p.case_count for p in self.workload.plans[index * 3 : index * 3 + 3]
        )
        cycle_id = await self.service.discover(
            guardian_id,
            provider_namespace=self.config.provider_namespace_id,
            versions=self.config.product_versions,
            expected_cases=counts,
            plan_revision=self.config.generator_version,
        )
        return guardian_id, cycle_id

    async def versions(self, cycle):
        async with self.database.session() as session:
            return (
                (
                    await session.execute(
                        select(VersionRow)
                        .where(VersionRow.cycle_id == cycle)
                        .order_by(VersionRow.ordinal)
                    )
                )
                .scalars()
                .all()
            )

    async def row(self, model, key):
        async with self.database.session() as session:
            return await session.get(model, key)

    async def remote_terminal(self, version, state="COMPLETED"):
        if state == "COMPLETED":
            await self.operator.advance_execution(version.business_key, "RUNNING")
        await self.operator.advance_execution(version.business_key, state)

    async def drain(self, *, maximum=200):
        for _ in range(maximum):
            if not await self.worker.tick():
                return
        raise AssertionError("due batch 未在预算内 drain")

    async def close(self):
        await self.worker.close()
        await self.client.aclose()


def test_timezone_and_deterministic_jitter():
    assert eligibility(BASE)[0] == "2026-10-06"
    assert eligibility(BASE.astimezone())[1] == BASE
    values = [poll_jitter(f"version-{i}", 1) for i in range(3000)]
    assert min(values) == -30 and max(values) == 30
    assert len(set(values)) == 61
    assert values == [poll_jitter(f"version-{i}", 1) for i in range(3000)]
    evidence(
        "poll-jitter.json",
        {
            "count": len(values),
            "min": min(values),
            "max": max(values),
            "buckets": dict(sorted(Counter(values).items())),
            "deterministic": True,
        },
    )


@pytest.mark.asyncio
async def test_identity_plan_freeze_overlap_and_disabled(clean_database):
    c = await Cohort(clean_database).initialize()
    try:
        guardian, cycle = await c.discover()
        assert (await c.discover()) == (guardian, cycle)
        versions = await c.versions(cycle)
        assert len(versions) == 3
        with pytest.raises(ValueError, match="PLAN_CONFLICT"):
            await c.service.discover(
                guardian,
                provider_namespace=c.config.provider_namespace_id,
                versions=("V1", "V2", "new-V3"),
                expected_cases=(100, 100, 100),
                plan_revision="new",
            )
        with pytest.raises(PersistenceError):
            async with clean_database.transaction() as session:
                await session.execute(
                    update(CycleRow)
                    .where(CycleRow.cycle_id == cycle)
                    .values(channel="changed")
                )
        _, other_suite = await c.discover(suite="another-suite")
        assert (await c.row(CycleRow, other_suite)).status == "SKIPPED_OVERLAP"
        assert all(
            v.status == "SKIPPED" and v.intent_at is None
            for v in await c.versions(other_suite)
        )
        await c.clock(86400)
        _, next_day = await c.discover()
        assert (await c.row(CycleRow, next_day)).status == "SKIPPED_OVERLAP"
        assert len(await c.versions(next_day)) == 3
        await c.service.set_enabled(guardian, False)
        await c.clock(172800)
        assert (await c.discover())[1] is None
        assert (await c.provider.store.counts())["unique_executions"] == 0
        evidence(
            "overlap.json",
            {
                "cross_suite": "SKIPPED_OVERLAP",
                "day2": "SKIPPED_OVERLAP",
                "day2_versions": 3,
                "remote_submits": 0,
                "disabled_prevents_new_cycle": True,
            },
        )
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_ten_environment_serial_cycle_with_failed_cases(clean_database):
    c = await Cohort(clean_database).initialize()
    try:
        failed_env = next(
            p.environment_index for p in c.workload.plans if p.failed_cases
        )
        indices = list(dict.fromkeys([failed_env, *range(10)]))[:10]
        cycles = [(await c.discover(i))[1] for i in indices]
        seconds = 480
        await c.clock(seconds)
        for ordinal in range(1, 4):
            for _ in range(2):
                await c.drain()
                seconds += 1
                await c.clock(seconds)
            for cycle in cycles:
                versions = await c.versions(cycle)
                assert versions[ordinal - 1].status == "ACTIVE"
                assert all(v.status == "PLANNED" for v in versions[ordinal:])
                await c.remote_terminal(versions[ordinal - 1])
            seconds += 331
            await c.clock(seconds)
            await c.drain()
            for cycle in cycles:
                versions = await c.versions(cycle)
                assert versions[ordinal - 1].status == "COMPLETED"
                if ordinal < 3:
                    async with clean_database.session() as session:
                        due = (
                            await session.execute(
                                select(DueRow).where(
                                    DueRow.version_execution_id
                                    == versions[ordinal].version_execution_id,
                                    DueRow.operation == "DISPATCH_VERSION",
                                )
                            )
                        ).scalar_one()
                    assert due.original_due_at >= versions[ordinal - 1].completed_at
        states = Counter([(await c.row(CycleRow, cycle)).status for cycle in cycles])
        assert sum(states.values()) == 10 and states["COMPLETED_WITH_FAILURES"] > 0
        assert (await c.provider.store.counts())["unique_executions"] == 30
        evidence(
            "controlled-cycle-summary.json",
            {
                "guardians": 10,
                "cycles": 10,
                "versions": 30,
                "remote_bindings": 30,
                "terminal_counts": states,
                "serial_progression": "PASS",
                "model_calls": 0,
            },
        )
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_response_loss_unknown_reconciliation_and_poll_diff(clean_database):
    c = await Cohort(clean_database).initialize()
    try:
        _, cycle = await c.discover()
        v1 = (await c.versions(cycle))[0]
        c.provider.faults.response_loss_keys = frozenset({v1.business_key})
        c.provider.faults.lookup_failures[v1.business_key] = 1
        await c.clock(480)
        await c.drain()
        v1 = (await c.versions(cycle))[0]
        assert v1.knowledge_state == "UNKNOWN" and v1.remote_id is None
        original = (
            await c.provider.lookup_by_business_key(
                LookupRequest(
                    remote_execution_business_key=v1.business_key,
                    request_digest=v1.request_digest,
                )
            )
            if not c.provider.faults.lookup_failures[v1.business_key]
            else None
        )
        # 重建 application owner，原 PostgreSQL 身份/UNKNOWN 窗口保留。
        c.service = GuardianScheduleService(clean_database, c.config.owner_scope_id)
        c.worker.service = c.service
        await c.service.initialize(logical_now=BASE)
        await c.clock(520)
        await c.drain()
        assert (await c.versions(cycle))[0].knowledge_state == "UNKNOWN"
        await c.clock(560)
        await c.drain()
        v1 = (await c.versions(cycle))[0]
        assert (
            v1.remote_id is not None
            and v1.knowledge_state == "KNOWN"
            and v1.submit_attempts == 1
        )
        original = await c.provider.lookup_by_business_key(
            LookupRequest(
                remote_execution_business_key=v1.business_key,
                request_digest=v1.request_digest,
            )
        )
        assert v1.remote_id == str(original.receipt.remote_execution_id)
        for seconds in (891, 1222):
            await c.clock(seconds)
            await c.drain()
        await c.remote_terminal(v1)
        await c.clock(1553)
        await c.drain()
        v1, v2, _ = await c.versions(cycle)
        assert v1.status == "COMPLETED" and v1.poll_requests == 3
        async with clean_database.session() as session:
            observations = (
                (
                    await session.execute(
                        select(ObservationRow).where(
                            ObservationRow.version_execution_id
                            == v1.version_execution_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(observations) == 3 and sum(o.changed for o in observations) == 2
        assert sum(o.evidence is not None for o in observations) == 2
        assert all(o.read_at != o.source_observed_at for o in observations)
        assert v2.status in {"DISPATCHING", "ACTIVE"}
        evidence(
            "unknown-recovery.json",
            {
                "response_lost_after_commit": True,
                "lookup_temporary_failure": True,
                "same_remote_id": v1.remote_id,
                "submit_attempts": v1.submit_attempts,
                "unknown_recoveries": v1.unknown_recoveries,
                "poll_requests": 3,
                "changed_observations": 2,
                "v2_status": v2.status,
                "model_calls": 0,
            },
        )
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_unresolved_hold_seal_and_terminal_resolution(clean_database):
    c = await Cohort(clean_database).initialize()
    try:
        _, cycle = await c.discover()
        v1 = (await c.versions(cycle))[0]
        # intent 先 durable；模拟在 HTTP 前 crash，未知是否发送。
        await c.clock(480)
        claim = (await c.service.claim_due(1))[0]
        prepared = await c.service.prepare(claim)
        await c.service.finish(prepared, error="SUBMIT_UNCERTAIN", uncertain=True)
        c.provider.faults.lookup_failures[v1.business_key] = 100
        await c.clock(2280)
        await c.drain()
        assert (await c.versions(cycle))[0].status == "UNRESOLVED"
        hold = await c.row(OccupancyRow, (c.config.owner_scope_id, "env-0000"))
        assert hold.safety_hold
        await c.clock(86400)
        _, skipped = await c.discover()
        assert (await c.row(CycleRow, skipped)).status == "SKIPPED_OVERLAP"
        with pytest.raises(ValueError, match="FINAL_PROOF"):
            from core.stage13.contracts import LookupResult

            await c.service.resolve_hold(
                v1.version_execution_id,
                lookup=LookupResult(result="NOT_CREATED"),
                source="test-operator",
            )
        sealed = await c.operator.seal_absent_key(
            LookupRequest(
                remote_execution_business_key=v1.business_key,
                request_digest=v1.request_digest,
            )
        )
        assert await c.service.resolve_hold(
            v1.version_execution_id, lookup=sealed, source="controlled-operator"
        )
        assert (await c.row(CycleRow, cycle)).status == "UNRESOLVED"
        assert (await c.row(CycleRow, skipped)).status == "SKIPPED_OVERLAP"
        hold = await c.row(OccupancyRow, (c.config.owner_scope_id, "env-0000"))
        assert not hold.safety_hold and len(hold.resolutions) == 1
        _, resolved_day_suite = await c.discover(suite="enabled-after-resolution")
        assert (await c.row(CycleRow, resolved_day_suite)).status == "SKIPPED_OVERLAP"
        # 另一路：remote 已创建，timeout 后获得 terminal receipt 解除 hold。
        _, other = await c.discover(1)
        await c.clock(86400 + 480)
        c.provider.faults.response_loss_keys = frozenset(
            {(await c.versions(other))[0].business_key}
        )
        await c.drain()
        ov = (await c.versions(other))[0]
        c.provider.faults.lookup_failures[ov.business_key] = 100
        await c.clock(86400 + 2280)
        await c.drain()
        assert (await c.row(CycleRow, other)).status == "UNRESOLVED"
        await c.remote_terminal(ov)
        c.provider.faults.lookup_failures[ov.business_key] = 0
        found = await c.provider.lookup_by_business_key(
            LookupRequest(
                remote_execution_business_key=ov.business_key,
                request_digest=ov.request_digest,
            )
        )
        packet = await c.provider.get_ci_summary(
            RemoteRequest(
                remote_execution_business_key=ov.business_key,
                request_digest=ov.request_digest,
                remote_execution_id=found.receipt.remote_execution_id,
            )
        )
        assert await c.service.resolve_hold(
            ov.version_execution_id,
            lookup=found,
            terminal_packet=packet,
            source="controlled-terminal-proof",
        )
        assert (await c.row(CycleRow, other)).status == "UNRESOLVED"
        evidence(
            "unresolved-hold.json",
            {
                "deadline_seconds": 1800,
                "day2": "SKIPPED_OVERLAP",
                "ordinary_not_created_rejected": True,
                "seal_resolution": "PASS",
                "terminal_resolution": "PASS",
                "old_cycle_frozen": True,
            },
        )
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_poll_error_and_result_delay_infra_stop(clean_database):
    c = await Cohort(
        clean_database, faults=ControlledFaults(result_delay_seconds=400)
    ).initialize()
    try:
        _, cycle = await c.discover()
        await c.clock(480)
        await c.drain()
        v1 = (await c.versions(cycle))[0]
        c.provider.faults.status_failures[v1.business_key] = 1
        await c.clock(811)
        await c.drain()
        assert (await c.versions(cycle))[0].status == "ACTIVE"
        await c.remote_terminal(v1)
        await c.clock(842)
        await c.drain()
        assert (await c.versions(cycle))[0].last_observed_state == "COMPLETED"
        for seconds in (872, 992, 1292):
            await c.clock(seconds)
            await c.drain()
        assert (await c.versions(cycle))[0].status == "COMPLETED"
        v2 = (await c.versions(cycle))[1]
        assert v2.remote_id
        await c.remote_terminal(v2, "INFRA_FAILED")
        await c.clock(1623)
        await c.drain()
        versions = await c.versions(cycle)
        assert versions[1].status == "INFRA_FAILED" and versions[2].status == "SKIPPED"
        assert (await c.row(CycleRow, cycle)).status == "FAILED"
        evidence(
            "result-delay-infra.json",
            {
                "temporary_poll_did_not_fail_cycle": True,
                "result_retries": versions[0].result_retries,
                "remote_completed_preserved": True,
                "infra_stops_remaining": True,
            },
        )
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_result_collection_exhausted_does_not_rewrite_remote(clean_database):
    c = await Cohort(
        clean_database, faults=ControlledFaults(result_delay_seconds=10000)
    ).initialize()
    try:
        _, cycle = await c.discover()
        await c.clock(480)
        await c.drain()
        v1 = (await c.versions(cycle))[0]
        await c.remote_terminal(v1)
        for seconds in (811, 841, 961, 1261):
            await c.clock(seconds)
            await c.drain()
        v1 = (await c.versions(cycle))[0]
        assert (
            v1.status == "INFRA_FAILED"
            and v1.reason == "RESULT_COLLECTION_FAILED"
            and v1.result_retries == 3
        )
        remote = RemoteRequest(
            remote_execution_business_key=v1.business_key,
            request_digest=v1.request_digest,
            remote_execution_id=v1.remote_id,
        )
        assert (
            json.loads((await c.provider.get_ci_summary(remote)).content)[
                "remote_state"
            ]
            == "COMPLETED"
        )
        assert (await c.row(CycleRow, cycle)).status == "FAILED"
        _, late_cycle = await c.discover(1)
        await c.drain()
        late = (await c.versions(late_cycle))[0]
        await c.remote_terminal(late)
        await c.clock(1600)
        await c.drain()
        reads = c.provider.counters["summary_reads"]
        await c.clock(36001)
        await c.drain()
        late = (await c.versions(late_cycle))[0]
        assert (
            late.status == "INFRA_FAILED" and late.reason == "RESULT_COLLECTION_FAILED"
        )
        assert late.last_observed_state == "COMPLETED"
        assert c.provider.counters["summary_reads"] == reads
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_multiworker_claim_and_real_lease_takeover(clean_database):
    c = await Cohort(clean_database).initialize()
    try:
        _, cycle = await c.discover()
        await c.clock(480)
        claims = await asyncio.gather(*(c.service.claim_due(1) for _ in range(4)))
        assert sum(len(x) for x in claims) == 1
        old = next(x[0] for x in claims if x)
        # lease 真实 DB 时钟；不靠业务 logical clock 过期。
        async with clean_database.transaction() as session:
            await session.execute(
                update(DueRow)
                .where(DueRow.work_key == old.work_key)
                .values(
                    lease_until=func.clock_timestamp() - text("interval '1 second'")
                )
            )
        takeover = (await c.service.claim_due(1))[0]
        assert takeover.epoch == old.epoch + 1 and takeover.token != old.token
        with pytest.raises(StaleClaim):
            await c.service.prepare(old)
        await c.worker.execute_claim(takeover)
        assert (await c.versions(cycle))[0].status == "ACTIVE"
        state = await c.row(SchedulerRow, c.config.owner_scope_id)
        assert state.stale_writes_rejected == 1
        evidence(
            "lease-takeover.json",
            {
                "workers": 4,
                "claim_winners": 1,
                "old_epoch": old.epoch,
                "new_epoch": takeover.epoch,
                "stale_rejected": state.stale_writes_rejected,
                "takeover_completed": True,
                "clock": "REAL_DB_TIME",
                "lease_expiry": "TEST_SCOPE_EXPLICIT_DB_EXPIRATION",
            },
        )
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_progression_transaction_rollback_replay_and_recovery_repair(
    clean_database,
):
    c = await Cohort(clean_database).initialize()
    try:
        _, cycle = await c.discover()
        await c.clock(480)
        await c.drain()
        v1 = (await c.versions(cycle))[0]
        await c.remote_terminal(v1)
        await c.clock(811)
        claim = (await c.service.claim_due(1))[0]
        prepared = await c.service.prepare(claim)
        result = await c.worker.invoker(
            "stage13_ci_summary",
            prepared.payload,
            principal_agent_id="core_router",
            operation_identity=prepared.operation_identity,
        )

        def crash(point):
            if point == "before_progression_commit":
                raise RuntimeError("TEST_CRASH_BEFORE_COMMIT")

        with pytest.raises(RuntimeError, match="BEFORE_COMMIT"):
            await c.service.finish(prepared, result, fault=crash)
        assert (await c.versions(cycle))[0].status == "ACTIVE"
        await c.service.finish(prepared, result)
        v1, v2, _ = await c.versions(cycle)
        async with clean_database.session() as session:
            due = (
                (
                    await session.execute(
                        select(DueRow).where(
                            DueRow.version_execution_id == v2.version_execution_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert v1.status == "COMPLETED" and len(due) == 1
        with pytest.raises(StaleClaim):
            await c.service.finish(prepared, result)
        # 明确测试 seam 制造 missing successor；recovery 通过业务 Owner 幂等补齐。
        async with clean_database.transaction() as session:
            await session.execute(
                text("DELETE FROM stage13_due_work WHERE work_key=:key"),
                {"key": due[0].work_key},
            )
        await c.service.recover("recovery-test")
        await c.service.recover("recovery-test")
        async with clean_database.session() as session:
            repaired = (
                await session.execute(
                    select(func.count())
                    .select_from(DueRow)
                    .where(DueRow.version_execution_id == v2.version_execution_id)
                )
            ).scalar_one()
        assert repaired == 1
        evidence(
            "progression-recovery.json",
            {
                "before_commit_rollback": True,
                "after_commit_v1_completed": True,
                "successor_due": repaired,
                "replay_fenced": True,
                "missing_successor_repaired_once": True,
            },
        )
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_fleet_bounded_admission_and_shutdown(clean_database):
    c = await Cohort(clean_database).initialize()
    try:
        for i in range(15):
            await c.discover(i)
        await c.clock(480)
        workers = [
            GuardianWorker(c.service, c.worker.invoker, enabled=True, concurrency=10)
            for _ in range(2)
        ]
        await asyncio.gather(*(w.tick() for w in workers))
        assert c.provider.counters["submit_attempts"] == 10
        await c.drain()
        assert c.provider.counters["submit_attempts"] == 10
        await c.clock(481)
        await c.drain()
        assert c.provider.counters["submit_attempts"] == 15
        await c.discover(15)
        await c.clock(482)
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked_invoker(*args, **kwargs):
            entered.set()
            await release.wait()
            return await c.worker.invoker(*args, **kwargs)

        draining = GuardianWorker(c.service, blocked_invoker, enabled=True)
        tick = asyncio.create_task(draining.tick())
        await asyncio.wait_for(entered.wait(), timeout=10)
        closing = asyncio.create_task(draining.close())
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        await tick
        await closing
        assert await draining.tick() == 0
        await c.worker.close()
        assert await c.worker.tick() == 0
        assert await GuardianWorker(c.service, c.worker.invoker).tick() == 0
    finally:
        await c.close()


@pytest.mark.REAL_PROCESS_CRASH_E2E
def test_real_process_unknown_receipt_loss_and_progression_crashes(
    clean_database, pg_schema, tmp_path
):
    config = WorkloadConfig(
        provider_namespace_id=f"wp02-process-{uuid4().hex}", environment_count=100
    )
    config_path = tmp_path / "config.json"
    config_path.write_text(config.model_dump_json(), encoding="utf-8")
    workload = Stage13Workload(config)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = {
        **os.environ,
        "LOCAL_AGENT_TEST_DATABASE_URL": pg_schema,
        "STAGE13_PROVIDER_DATABASE_URL": pg_schema,
        "STAGE13_PROVIDER_CONFIG_PATH": str(config_path),
        "STAGE13_PROVIDER_AGENT_TOKEN": "wp02-process-test-only",
        "STAGE13_PROVIDER_RESPONSE_LOSS_KEYS": json.dumps(
            [workload.request(0).remote_execution_business_key]
        ),
        "WP02_PROVIDER_URL": f"http://127.0.0.1:{port}",
        "PYTHONIOENCODING": "utf-8",
    }
    provider_process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "core.stage13.http:provider_app_from_environment",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
            "--no-access-log",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    async def owner(action, seconds=None, cycle=None):
        # 每次真正创建/关闭 engine；不跨 event loop 复用 fixture pool。
        from core.persistence.database import Database, DatabaseConfig

        database = Database(DatabaseConfig(url=pg_schema))
        service = GuardianScheduleService(database, config.owner_scope_id)
        provider = ControlledCIProvider(ProviderStore(database, workload))
        try:
            await provider.initialize()
            await service.initialize(logical_now=BASE)
            if seconds is not None:
                await service.advance_clock(BASE + timedelta(seconds=seconds))
                await ControlledProviderOperator(provider.store).advance_clock(seconds)
            if action == "discover":
                gid = await service.register_guardian(
                    config.automation_project_id,
                    config.suite_id,
                    "env-0000",
                    "channel-00",
                )
                return await service.discover(
                    gid,
                    provider_namespace=config.provider_namespace_id,
                    versions=config.product_versions,
                    expected_cases=tuple(p.case_count for p in workload.plans[:3]),
                    plan_revision=config.generator_version,
                )
            async with database.session() as session:
                versions = (
                    (
                        await session.execute(
                            select(VersionRow)
                            .where(VersionRow.cycle_id == cycle)
                            .order_by(VersionRow.ordinal)
                        )
                    )
                    .scalars()
                    .all()
                    if cycle
                    else []
                )
            if action == "terminal":
                operator = ControlledProviderOperator(provider.store)
                await operator.advance_execution(versions[0].business_key, "RUNNING")
                await operator.advance_execution(versions[0].business_key, "COMPLETED")
            if action == "snapshot":
                async with database.session() as session:
                    due_count = (
                        await session.execute(
                            select(func.count())
                            .select_from(DueRow)
                            .where(
                                DueRow.version_execution_id
                                == versions[1].version_execution_id,
                                DueRow.operation == "DISPATCH_VERSION",
                            )
                        )
                    ).scalar_one()
                return {
                    "v1": versions[0].status,
                    "knowledge": versions[0].knowledge_state,
                    "remote": versions[0].remote_id,
                    "v2": versions[1].status,
                    "v2_due": due_count,
                }
        finally:
            await database.dispose()

    def child(mode="normal"):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "tests._stage13_wp02_process",
                mode,
                str(config_path),
            ],
            env=env,
            capture_output=True,
            encoding="utf-8",
            timeout=40,
        )
        assert result.returncode == (
            42 if mode == "after_claim" else 41 if mode != "normal" else 0
        ), result.stderr
        return result.returncode

    try:
        with httpx.Client(
            base_url=env["WP02_PROVIDER_URL"],
            headers={"Authorization": "Bearer wp02-process-test-only"},
        ) as client:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                assert provider_process.poll() is None
                try:
                    if (
                        client.post(
                            "/v1/lookup",
                            json={
                                "remote_execution_business_key": workload.request(
                                    0
                                ).remote_execution_business_key,
                                "request_digest": workload.request(0).request_digest,
                            },
                        ).status_code
                        == 200
                    ):
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.05)
            else:
                pytest.fail("Provider readiness timeout")
        cycle = asyncio.run(owner("discover", 480))
        child("after_claim")
        time.sleep(31)
        child()
        assert asyncio.run(owner("snapshot", cycle=cycle))["knowledge"] == "UNKNOWN"
        asyncio.run(owner("clock", 520))
        child()
        bound = asyncio.run(owner("snapshot", cycle=cycle))
        assert bound["remote"] and bound["knowledge"] == "KNOWN"
        asyncio.run(owner("terminal", cycle=cycle))
        asyncio.run(owner("clock", 851))
        child("before_progression_commit")
        assert asyncio.run(owner("snapshot", cycle=cycle))["v1"] == "ACTIVE"
        # 真正等待 DB lease 过期；logical clock 不动。
        time.sleep(31)
        child("after_progression_commit")
        committed = asyncio.run(owner("snapshot", cycle=cycle))
        assert committed["v1"] == "COMPLETED" and committed["v2_due"] == 1
        child("after_provider_receipt")
        assert asyncio.run(owner("snapshot", cycle=cycle))["v2"] == "DISPATCHING"
        time.sleep(31)
        child()
        recovered = asyncio.run(owner("snapshot", cycle=cycle))
        assert recovered["v2"] == "ACTIVE" and recovered["v2_due"] == 1
        evidence(
            "real-process-recovery.json",
            {
                "provider_process": "uvicorn_HTTP",
                "local_owner_processes": 7,
                "claim_crash_takeover": "PASS",
                "response_loss_unknown_restart": "PASS",
                "remote_id": bound["remote"],
                "before_commit_rollback": "PASS",
                "after_commit_successor_once": "PASS",
                "receipt_before_binding_crash": "PASS",
                "real_lease_wait_seconds": 93,
                "recovered": recovered,
                "exit_codes": [42, 0, 0, 41, 41, 41, 0],
            },
        )
    finally:
        if provider_process.poll() is None:
            provider_process.terminate()
        provider_process.wait(timeout=10)


@pytest.mark.asyncio
async def test_realtime_http_admission_persists_across_workers(clean_database):
    service = GuardianScheduleService(clean_database, "wp02-real-admission")
    await service.initialize()
    accepted = await asyncio.gather(
        *(service.admit_http(submit=True) for _ in range(11))
    )
    assert sum(delay == 0 for delay in accepted) == 10
    restarted = GuardianScheduleService(clean_database, "wp02-real-admission")
    assert await restarted.admit_http(submit=True) > 0
    reads = await asyncio.gather(*(service.admit_http(submit=False) for _ in range(21)))
    assert sum(delay == 0 for delay in reads) == 20
    evidence(
        "real-admission.json",
        {
            "clock": "REAL_DB_TIME",
            "concurrent_submit_admission": 10,
            "concurrent_read_admission": 20,
            "restart_does_not_reset_budget": True,
        },
    )
