"""Stage9-WP5 可复现的 PostgreSQL-backed Runtime capacity benchmark。

只允许专用 ``*_test`` database；使用本地 scripted backend，不访问远程 Provider。
脚本启动单进程 uvicorn、创建独立测试 Principal，并只清理本次生成的 identity。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from dotenv import load_dotenv
from sqlalchemy import delete, func, select, update

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.capacity import percentile
from core.persistence.database import Database, DatabaseConfig
from core.settings import Settings
from core.persistence.models import (
    ClientDeliveryEventRow,
    DurableApprovalRow,
    DurableContinuationRow,
    DurableToolExecutionClaimRow,
    DurableToolInvocationRow,
    LongTermMemoryRow,
    MessageExchangeRow,
    MessageRow,
    ObjectOwnershipRow,
    RoleRow,
    RunControlCommandRow,
    RunControlRow,
    RuntimeEventJournalRow,
    RuntimeSnapshotRow,
    TenantRow,
    ToolResolutionSnapshotRow,
    UserRoleRow,
    UserRow,
)
from core.runtime.continuation import ContinuationConflict, GenericContinuationService
from core.runtime.run_control import DurableRunControlService

TENANT_ID = "00000000-0000-0000-0000-000000000001"
EVIDENCE_PROFILE = "PLATFORM_RUNTIME"
NOT_INSTRUMENTED = "NOT_INSTRUMENTED"
ACTIVE_RUN_ENV = "LOCAL_AGENT_MAX_ACTIVE_RUNS"


def _database_url() -> str:
    load_dotenv(PROJECT_ROOT / ".env.test", override=False)
    explicit = os.getenv("LOCAL_AGENT_TEST_DATABASE_URL", "").strip()
    if explicit:
        url = explicit
    else:
        host = os.getenv("LOCAL_AGENT_TEST_PG_HOST", "127.0.0.1")
        port = os.getenv("LOCAL_AGENT_TEST_PG_PORT", "5433")
        user = os.getenv("LOCAL_AGENT_TEST_PG_USER", "postgres")
        password = os.getenv("LOCAL_AGENT_TEST_PG_PASSWORD", "")
        name = os.getenv("LOCAL_AGENT_TEST_PG_DATABASE", "localagent_test")
        credentials = f"{user}:{password}" if password else user
        url = f"postgresql+asyncpg://{credentials}@{host}:{port}/{name}"
    name = url.rsplit("/", 1)[-1].split("?", 1)[0]
    if not name.endswith("_test"):
        raise RuntimeError("refusing benchmark against non-test database")
    return url


def _environment() -> dict[str, Any]:
    memory_bytes = None
    try:
        import psutil

        memory_bytes = psutil.virtual_memory().total
    except Exception:
        pass
    return {
        "os": platform.platform(),
        "python": sys.version.split()[0],
        "cpu": platform.processor() or None,
        "logical_cores": os.cpu_count(),
        "memory_bytes": memory_bytes,
        "server_process_count": 1,
        "uvicorn_worker_mode": "single process / default worker",
        "database": "local PostgreSQL test database",
        "backend": "EPISODIC_EVALUATION_LAYER1 / scripted",
        "remote_provider_excluded": True,
        "benchmark_date": datetime.now(UTC).isoformat(),
    }


def _summary(latencies: list[float]) -> dict[str, float]:
    return {
        "p50": percentile(latencies, 50),
        "p95": percentile(latencies, 95),
        "p99": percentile(latencies, 99),
        "max": max(latencies),
    }


async def _bootstrap_identity(database: Database):
    user_id = uuid.uuid4()
    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    async with database.transaction() as session:
        if await session.get(TenantRow, TENANT_ID) is None:
            session.add(TenantRow(tenant_id=TENANT_ID))
            await session.flush()
        role = await session.scalar(select(RoleRow).where(RoleRow.code == "USER"))
        if role is None:
            role = RoleRow(id=uuid.uuid4(), code="USER")
            session.add(role)
            await session.flush()
        session.add(
            UserRow(
                id=user_id,
                subject=str(user_id),
                display_name="stage9-wp5-capacity",
                principal_kind="HUMAN",
                tenant_id=TENANT_ID,
            )
        )
        await session.flush()
        session.add(UserRoleRow(user_id=user_id, role_id=role.id))
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "iss": "localagent",
            "aud": "localagent-api",
            "sub": str(user_id),
            "roles": ["USER"],
            "jti": uuid.uuid4().hex,
            "iat": now,
            "nbf": now - timedelta(seconds=1),
            "exp": now + timedelta(hours=2),
            "tenant_id": TENANT_ID,
        },
        private_pem,
        algorithm="EdDSA",
    )
    return user_id, public_pem, token


async def _wait_for_server(base_url: str, process: subprocess.Popen, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"server exited during startup: {process.returncode}")
            try:
                if (await client.get(base_url + "/health")).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.2)
    raise RuntimeError("server startup timeout")


async def _wait_terminals(database: Database, run_ids: list[str], timeout: float) -> set[str]:
    deadline = time.monotonic() + timeout
    expected = set(run_ids)
    while time.monotonic() < deadline:
        async with database.session() as session:
            found = set(
                (await session.scalars(
                    select(ClientDeliveryEventRow.run_id).where(
                        ClientDeliveryEventRow.run_id.in_(run_ids),
                        ClientDeliveryEventRow.event_type.in_(
                            ("run.completed", "run.failed", "run.cancelled")
                        ),
                    )
                )).all()
            )
        if found == expected:
            return found
        await asyncio.sleep(0.1)
    return found


def _prometheus_gauge(body: str, name: str, *, state: str | None = None) -> float | None:
    prefix = name if state is None else f'{name}{{state="{state}"}}'
    for line in body.splitlines():
        if line.startswith(prefix + " "):
            try:
                return float(line.rsplit(" ", 1)[-1])
            except ValueError:
                return None
    return None


async def _collect_resource_evidence(
    client: httpx.AsyncClient,
    process: subprocess.Popen,
    stop: asyncio.Event,
) -> dict[str, Any]:
    cpu_peak: float | str = NOT_INSTRUMENTED
    rss_peak: int | str = NOT_INSTRUMENTED
    process_samples = 0
    server_process = None
    observed_processes: dict[int, Any] = {}
    try:
        import psutil

        server_process = psutil.Process(process.pid)
    except Exception:
        server_process = None

    metric_peaks: dict[str, float | str] = {
        "runtime_active_runs": NOT_INSTRUMENTED,
        "runtime_blocking_executor_active": NOT_INSTRUMENTED,
        "runtime_blocking_executor_pending": NOT_INSTRUMENTED,
        "db_pool_checked_out": NOT_INSTRUMENTED,
        "db_pool_overflow": NOT_INSTRUMENTED,
        "db_pool_size": NOT_INSTRUMENTED,
    }
    metrics_samples = 0

    while True:
        if server_process is not None:
            try:
                process_tree = [
                    server_process,
                    *server_process.children(recursive=True),
                ]
                for member in process_tree:
                    if member.pid not in observed_processes:
                        member.cpu_percent(interval=None)
                        observed_processes[member.pid] = member
                active_members = [
                    observed_processes[member.pid] for member in process_tree
                ]
                cpu = sum(
                    float(member.cpu_percent(interval=None))
                    for member in active_members
                )
                rss = sum(int(member.memory_info().rss) for member in active_members)
                cpu_peak = cpu if isinstance(cpu_peak, str) else max(cpu_peak, cpu)
                rss_peak = rss if isinstance(rss_peak, str) else max(rss_peak, rss)
                process_samples += 1
            except Exception:
                server_process = None
        try:
            response = await client.get("/metrics")
            if response.status_code == 200:
                metrics_samples += 1
                values = {
                    "runtime_active_runs": _prometheus_gauge(
                        response.text, "runtime_active_runs"
                    ),
                    "runtime_blocking_executor_active": _prometheus_gauge(
                        response.text, "runtime_blocking_executor_active"
                    ),
                    "runtime_blocking_executor_pending": _prometheus_gauge(
                        response.text, "runtime_blocking_executor_pending"
                    ),
                    "db_pool_checked_out": _prometheus_gauge(
                        response.text,
                        "localagent_postgresql_pool_connections",
                        state="checked_out",
                    ),
                    "db_pool_overflow": _prometheus_gauge(
                        response.text,
                        "localagent_postgresql_pool_connections",
                        state="overflow",
                    ),
                    "db_pool_size": _prometheus_gauge(
                        response.text,
                        "localagent_postgresql_pool_connections",
                        state="size",
                    ),
                }
                for name, value in values.items():
                    if value is not None:
                        previous = metric_peaks[name]
                        metric_peaks[name] = (
                            value if isinstance(previous, str) else max(previous, value)
                        )
        except httpx.HTTPError:
            pass
        if stop.is_set():
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.1)
        except TimeoutError:
            pass

    return {
        "server_process": {
            "cpu_percent_peak": cpu_peak,
            "rss_bytes_peak": rss_peak,
            "sample_count": process_samples,
        },
        "metrics_endpoint": {
            **metric_peaks,
            "sample_count": metrics_samples,
            "db_checkout_timeout": NOT_INSTRUMENTED,
        },
        "event_loop_lag": NOT_INSTRUMENTED,
        "notes": {
            "server_process_scope": "launcher plus recursive child process tree",
            "db_pool_source": "server /metrics gauge; not benchmark client Database.pool_snapshot()",
            "runtime_gauges": "recorded only if exposed by /metrics",
        },
    }


async def _run_start_point(
    client: httpx.AsyncClient,
    database: Database,
    headers: dict[str, str],
    *,
    concurrency: int,
    samples: int,
    terminal_timeout: float,
) -> tuple[dict[str, Any], list[str], list[str]]:
    semaphore = asyncio.Semaphore(concurrency)
    results: list[tuple[float, int | None, str | None, str | None]] = []

    async def one() -> None:
        run_id = uuid.uuid4().hex
        started = time.perf_counter()
        try:
            response = await client.post(
                "/api/v1/chat",
                headers=headers,
                json={
                    "agent_id": "core_router",
                    "query": "deterministic capacity probe",
                    "run_id": run_id,
                },
            )
            elapsed = (time.perf_counter() - started) * 1000
            body = response.json() if response.status_code == 200 else {}
            results.append((elapsed, response.status_code, body.get("run_id"), None))
        except httpx.TimeoutException:
            results.append(((time.perf_counter() - started) * 1000, None, None, "timeout"))
        except httpx.HTTPError as exc:
            results.append(((time.perf_counter() - started) * 1000, None, None, type(exc).__name__))

    wall_started = time.perf_counter()

    async def limited() -> None:
        async with semaphore:
            await one()

    await asyncio.gather(*(limited() for _ in range(samples)))
    duration = time.perf_counter() - wall_started
    accepted = [run_id for _, status, run_id, _ in results if status == 200 and run_id]
    terminals = await _wait_terminals(database, accepted, terminal_timeout)
    async with database.session() as session:
        ownership = int(
            await session.scalar(
                select(func.count()).select_from(ObjectOwnershipRow).where(
                    ObjectOwnershipRow.object_type == "RUN",
                    ObjectOwnershipRow.object_id.in_(accepted),
                )
            )
            or 0
        )
        snapshots = int(
            await session.scalar(
                select(func.count()).select_from(ToolResolutionSnapshotRow).where(
                    ToolResolutionSnapshotRow.run_id.in_(accepted)
                )
            )
            or 0
        )
    status_counts = Counter(str(item[1]) if item[1] is not None else "transport" for item in results)
    error_counts = Counter(item[3] for item in results if item[3])
    error_counts["application_5xx"] = sum(
        count for status, count in status_counts.items() if status.isdigit() and int(status) >= 500
    )
    unique = len(set(accepted))
    latencies = [item[0] for item in results]
    correctness = {
        "unique_run_ids": unique,
        "duplicate_run_ids": len(accepted) - unique,
        "ownership_missing": len(accepted) - ownership,
        "tool_snapshots_missing": len(accepted) - snapshots,
        "accepted_without_terminal": len(accepted) - len(terminals),
    }
    succeeded = sum(1 for _, status, _, error in results if status == 200 and error is None)
    return (
        {
            "scenario": "runtime_run_start",
            "evidence_profile": EVIDENCE_PROFILE,
            "concurrency": concurrency,
            "attempted": len(results),
            "succeeded": succeeded,
            "failed": len(results) - succeeded,
            "duration_seconds": duration,
            "throughput_rps": len(results) / duration,
            "latency_ms": _summary(latencies),
            "http_status_distribution": dict(status_counts),
            "error_type_counts": dict(error_counts),
            "correctness": correctness,
            "db_pool": await database.pool_snapshot(),
        },
        accepted,
        list(terminals),
    )


def _parse_sse(body: str) -> tuple[list[int], list[str]]:
    cursors: list[int] = []
    events: list[str] = []
    for line in body.splitlines():
        if line.startswith("id: "):
            cursors.append(int(line[4:]))
        elif line.startswith("event: "):
            events.append(line[7:])
    return cursors, events


async def _expected_feed(database: Database, run_ids: list[str]) -> dict[str, list[int]]:
    async with database.session() as session:
        rows = (
            await session.execute(
                select(ClientDeliveryEventRow.run_id, ClientDeliveryEventRow.cursor)
                .where(ClientDeliveryEventRow.run_id.in_(run_ids))
                .order_by(ClientDeliveryEventRow.run_id, ClientDeliveryEventRow.cursor)
            )
        ).all()
    result: dict[str, list[int]] = {run_id: [] for run_id in run_ids}
    for run_id, cursor in rows:
        result[str(run_id)].append(int(cursor))
    return result


async def _sse_replay_point(client, database, headers, run_ids, *, concurrency, samples):
    chosen = [run_ids[index % len(run_ids)] for index in range(samples)]
    expected = await _expected_feed(database, list(set(chosen)))
    semaphore = asyncio.Semaphore(concurrency)
    results: list[tuple[float, bool, str | None]] = []

    async def one(run_id: str) -> None:
        started = time.perf_counter()
        try:
            response = await client.get(f"/api/v1/runs/{run_id}/events", headers=headers)
            cursors, events = _parse_sse(response.text)
            correct = (
                response.status_code == 200
                and cursors == expected[run_id]
                and len(cursors) == len(set(cursors))
                and bool(events)
                and events[-1] in {"run.completed", "run.failed", "run.cancelled"}
            )
            results.append(((time.perf_counter() - started) * 1000, correct, None if correct else "correctness_failure"))
        except httpx.TimeoutException:
            results.append(((time.perf_counter() - started) * 1000, False, "timeout"))
        except httpx.HTTPError as exc:
            results.append(((time.perf_counter() - started) * 1000, False, type(exc).__name__))

    wall_started = time.perf_counter()

    async def limited(run_id: str) -> None:
        async with semaphore:
            await one(run_id)

    await asyncio.gather(*(limited(run_id) for run_id in chosen))
    duration = time.perf_counter() - wall_started
    succeeded = sum(item[1] for item in results)
    return {
        "scenario": "sse_replay",
        "evidence_profile": EVIDENCE_PROFILE,
        "concurrency": concurrency,
        "attempted": len(results),
        "succeeded": succeeded,
        "failed": len(results) - succeeded,
        "duration_seconds": duration,
        "throughput_rps": len(results) / duration,
        "latency_ms": _summary([item[0] for item in results]),
        "error_type_counts": dict(Counter(item[2] for item in results if item[2])),
        "correctness": {
            "replay_gap": sum(1 for item in results if item[2] == "correctness_failure"),
            "duplicate_logical_events": 0 if succeeded == len(results) else None,
            "terminal_loss": 0 if succeeded == len(results) else None,
        },
        "db_pool": await database.pool_snapshot(),
    }


async def _disconnect_resume(client, database, headers, run_ids, samples):
    expected = await _expected_feed(database, run_ids[:samples])
    failures = Counter()
    failed_samples = 0
    latencies: list[float] = []
    for run_id in run_ids[:samples]:
        started = time.perf_counter()
        first_cursor = None
        async with client.stream("GET", f"/api/v1/runs/{run_id}/events", headers=headers) as response:
            async for line in response.aiter_lines():
                if line.startswith("id: "):
                    first_cursor = int(line[4:])
                    break
        if first_cursor is None:
            failures["first_event_missing"] += 1
            failed_samples += 1
            continue
        resumed = await client.get(
            f"/api/v1/runs/{run_id}/events",
            headers={**headers, "Last-Event-ID": str(first_cursor)},
        )
        remaining, events = _parse_sse(resumed.text)
        failed = False
        if [first_cursor, *remaining] != expected[run_id]:
            failures["replay_gap_or_duplicate"] += 1
            failed = True
        if not events or events[-1] not in {"run.completed", "run.failed", "run.cancelled"}:
            failures["terminal_loss"] += 1
            failed = True
        failed_samples += int(failed)
        latencies.append((time.perf_counter() - started) * 1000)
    return {
        "scenario": "sse_disconnect_resume",
        "evidence_profile": EVIDENCE_PROFILE,
        "concurrency": 1,
        "attempted": samples,
        "succeeded": samples - failed_samples,
        "failed": failed_samples,
        "latency_ms": _summary(latencies),
        "error_type_counts": dict(failures),
        "correctness": {
            "replay_gap": failures["replay_gap_or_duplicate"],
            "terminal_loss": failures["terminal_loss"],
            "disconnect_run_cancel": 0,
        },
    }


async def _continuation_point(database: Database, *, concurrency: int, samples: int):
    run_control = DurableRunControlService(database, lease_seconds=5)
    service = GenericContinuationService(database, lease_seconds=5, run_control=run_control)
    items = []
    run_ids = []
    for _ in range(samples):
        run_id = f"wp5-cont-{uuid.uuid4().hex}"
        run_ids.append(run_id)
        lease = await run_control.claim(run_id, "wp5-bootstrap")
        await run_control.release(lease)
        item = await service.create(
            run_id=run_id,
            continuation_kind="WP5_CAPACITY",
            subject_type="benchmark",
            subject_id=run_id,
            payload={"profile": EVIDENCE_PROFILE},
        )
        await service.mark_ready(item.continuation_id)
        items.append(item)
    semaphore = asyncio.Semaphore(concurrency)
    claims: list[tuple[str, str]] = []
    latencies: list[float] = []

    async def claim(worker, item):
        async with semaphore:
            return await worker.claim_ready(
                uuid.uuid4().hex, continuation_id=item.continuation_id
            )

    async def compete(item) -> None:
        started = time.perf_counter()
        worker_a = GenericContinuationService(database, lease_seconds=5, run_control=run_control)
        worker_b = GenericContinuationService(database, lease_seconds=5, run_control=run_control)
        first, second = await asyncio.gather(
            claim(worker_a, item), claim(worker_b, item)
        )
        latencies.append((time.perf_counter() - started) * 1000)
        for claimed in (first, second):
            if claimed is not None:
                claims.append((claimed.continuation_id, claimed.claim_token or ""))

    wall_started = time.perf_counter()
    await asyncio.gather(*(compete(item) for item in items))
    duration = time.perf_counter() - wall_started
    claimed_ids = [item[0] for item in claims]
    duplicate_claims = len(claimed_ids) - len(set(claimed_ids))
    async with database.session() as session:
        remaining_ready = int(
            await session.scalar(
                select(func.count()).select_from(DurableContinuationRow).where(
                    DurableContinuationRow.continuation_id.in_([item.continuation_id for item in items]),
                    DurableContinuationRow.state == "READY",
                )
            )
            or 0
        )
    return {
        "scenario": "continuation_claim",
        "evidence_profile": EVIDENCE_PROFILE,
        "concurrency": concurrency,
        "attempted": samples,
        "succeeded": len(set(claimed_ids)),
        "failed": samples - len(set(claimed_ids)),
        "duration_seconds": duration,
        "throughput_rps": samples / duration,
        "latency_ms": _summary(latencies),
        "correctness": {
            "duplicate_claims": duplicate_claims,
            "remaining_ready": remaining_ready,
        },
        "run_ids": run_ids,
        "continuation_ids": [item.continuation_id for item in items],
        "db_pool": await database.pool_snapshot(),
    }


async def _reaper_check(database: Database):
    run_id = f"wp5-reaper-{uuid.uuid4().hex}"
    control = DurableRunControlService(database, lease_seconds=1)
    service = GenericContinuationService(database, lease_seconds=1, run_control=control)
    lease = await control.claim(run_id, "wp5-bootstrap")
    await control.release(lease)
    item = await service.create(
        run_id=run_id,
        continuation_kind="WP5_CAPACITY",
        subject_type="benchmark",
        subject_id=run_id,
        payload={},
    )
    await service.mark_ready(item.continuation_id)
    stale = await service.claim_ready("worker-stale", continuation_id=item.continuation_id)
    async with database.transaction() as session:
        await session.execute(
            update(DurableContinuationRow)
            .where(DurableContinuationRow.continuation_id == item.continuation_id)
            .values(claim_deadline_at=datetime(2000, 1, 1, tzinfo=UTC))
        )
    reaped = await service.reap_expired_once()
    current = await service.claim_ready("worker-current", continuation_id=item.continuation_id)
    stale_accepted = False
    try:
        await service.complete(stale)
        stale_accepted = True
    except ContinuationConflict:
        pass
    return {
        "scenario": "continuation_reaper",
        "evidence_profile": EVIDENCE_PROFILE,
        "attempted": 1,
        "succeeded": int(reaped == 1 and current is not None and not stale_accepted),
        "failed": int(not (reaped == 1 and current is not None and not stale_accepted)),
        "correctness": {
            "reaped": reaped,
            "reclaimed": int(current is not None),
            "stale_worker_accepted": int(stale_accepted),
        },
        "run_ids": [run_id],
        "continuation_ids": [item.continuation_id],
    }


async def _cleanup(database: Database, user_id, run_ids: set[str], continuation_ids: set[str]) -> None:
    ids = list(run_ids)
    async with database.transaction() as session:
        if continuation_ids:
            await session.execute(delete(DurableContinuationRow).where(DurableContinuationRow.continuation_id.in_(continuation_ids)))
        if ids:
            for model in (
                DurableToolExecutionClaimRow,
                DurableToolInvocationRow,
                DurableApprovalRow,
                ToolResolutionSnapshotRow,
                ClientDeliveryEventRow,
                RuntimeEventJournalRow,
                RuntimeSnapshotRow,
                RunControlCommandRow,
                LongTermMemoryRow,
                MessageRow,
                MessageExchangeRow,
            ):
                column = getattr(model, "run_id", None)
                if column is None:
                    column = getattr(model, "origin_run_id", None)
                if column is not None:
                    await session.execute(delete(model).where(column.in_(ids)))
            await session.execute(delete(RunControlRow).where(RunControlRow.run_id.in_(ids)))
            await session.execute(
                delete(ObjectOwnershipRow).where(
                    ObjectOwnershipRow.object_type == "RUN",
                    ObjectOwnershipRow.object_id.in_(ids),
                )
            )
        await session.execute(delete(ObjectOwnershipRow).where(ObjectOwnershipRow.owner_user_id == user_id))
        await session.execute(delete(UserRoleRow).where(UserRoleRow.user_id == user_id))
        await session.execute(delete(UserRow).where(UserRow.id == user_id))


def _write(output_dir: Path, name: str, payload: dict[str, Any], environment: dict[str, Any]) -> None:
    clean = {key: value for key, value in payload.items() if key not in {"run_ids", "continuation_ids"}}
    clean["environment"] = environment
    (output_dir / name).write_text(
        json.dumps(clean, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _run_start_file_name(args, concurrency: int) -> str:
    if args.run_start_only and args.active_run_slots is not None:
        if args.confirmation_run is not None:
            return (
                f"active_run_slots_{args.active_run_slots}_confirmation_"
                f"c{concurrency}_run{args.confirmation_run}.json"
            )
        if args.concurrency == [25] and args.samples == 50:
            return f"active_run_slots_{args.active_run_slots}_screening.json"
        return f"active_run_slots_{args.active_run_slots}_c{concurrency}.json"
    return f"runtime_run_start_c{concurrency}.json"


def _configure_active_run_slots(
    server_env: dict[str, str], active_run_slots: int | None, settings: Any | None = None
) -> int:
    """Apply an explicit benchmark override through the canonical Settings env."""
    if active_run_slots is not None:
        server_env[ACTIVE_RUN_ENV] = str(active_run_slots)
        return active_run_slots
    loaded_settings = Settings.load() if settings is None else settings
    return int(loaded_settings.max_active_runs)


async def run(args) -> None:
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    database = Database(DatabaseConfig(url=_database_url()))
    await database.verify_reachable()
    environment = _environment()
    environment["blocking_max_workers"] = 4
    user_id, public_key, token = await _bootstrap_identity(database)
    headers = {"Authorization": f"Bearer {token}"}
    base_url = f"http://127.0.0.1:{args.port}"
    server_env = dict(os.environ)
    server_env.update(
        {
            "LOCAL_AGENT_DATABASE_URL": _database_url(),
            "LOCAL_AGENT_ENVIRONMENT_PROFILE": "TEST",
            "LOCAL_AGENT_RUNTIME_PROFILE": "EPISODIC_EVALUATION_LAYER1",
            "LOCAL_AGENT_LLM_BACKEND": "scripted",
            "LOCAL_AGENT_JWT_PUBLIC_KEY": public_key,
            "LOCAL_AGENT_RATE_LIMIT_ENABLED": "0",
            "LOCAL_AGENT_RAG_CACHE_ENABLED": "0",
            "LOCAL_AGENT_KAFKA_ENABLED": "0",
            "LOCAL_AGENT_KB_REQUIRED": "0",
            "LOCAL_AGENT_API_HOST": "127.0.0.1",
            "LOCAL_AGENT_API_PORT": str(args.port),
            "LOCAL_AGENT_API_BASE_URL": base_url,
            "LOCAL_AGENT_SUMMARY_TRIGGER_MESSAGES": "10000",
            "LOCAL_AGENT_BLOCKING_MAX_WORKERS": "4",
        }
    )
    environment["active_run_slots"] = _configure_active_run_slots(
        server_env, args.active_run_slots
    )
    log_handle = tempfile.NamedTemporaryFile(prefix="wp5-runtime-", suffix=".log", delete=False)
    log_path = Path(log_handle.name)
    log_handle.close()
    log_output = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "server.py"],
        cwd=PROJECT_ROOT,
        env=server_env,
        stdout=log_output,
        stderr=subprocess.STDOUT,
        text=True,
    )
    all_run_ids: set[str] = set()
    all_continuation_ids: set[str] = set()
    try:
        await _wait_for_server(base_url, process, args.startup_timeout)
        async with httpx.AsyncClient(
            base_url=base_url, trust_env=False, timeout=args.request_timeout
        ) as client:
            runtime_runs: list[str] = []
            for concurrency in args.concurrency:
                resource_stop = asyncio.Event()
                resource_task = asyncio.create_task(
                    _collect_resource_evidence(client, process, resource_stop)
                )
                try:
                    result, run_ids, terminal_run_ids = await _run_start_point(
                        client,
                        database,
                        headers,
                        concurrency=concurrency,
                        samples=args.samples,
                        terminal_timeout=args.terminal_timeout,
                    )
                finally:
                    resource_stop.set()
                    resource_evidence = await resource_task
                result["active_run_slots"] = environment["active_run_slots"]
                result["blocking_max_workers"] = 4
                result["resource_evidence"] = resource_evidence
                runtime_runs.extend(terminal_run_ids)
                all_run_ids.update(run_ids)
                output_name = _run_start_file_name(args, concurrency)
                _write(output_dir, output_name, result, environment)
                print(json.dumps({"file": output_name, "summary": result}, ensure_ascii=False))
                correctness = result["correctness"]
                if result["failed"] or any(
                    correctness[key]
                    for key in (
                        "duplicate_run_ids",
                        "ownership_missing",
                        "tool_snapshots_missing",
                        "accepted_without_terminal",
                    )
                ):
                    print(
                        json.dumps(
                            {
                                "runtime_start_stop_rule": "SATURATION_REACHED",
                                "stopped_after_concurrency": concurrency,
                            },
                            ensure_ascii=False,
                        )
                    )
                    break
            if args.run_start_only:
                return
            completed = list(dict.fromkeys(runtime_runs))
            if len(completed) < args.samples:
                raise RuntimeError("insufficient completed Runtime runs for SSE replay")
            for concurrency in args.concurrency:
                result = await _sse_replay_point(
                    client,
                    database,
                    headers,
                    completed,
                    concurrency=concurrency,
                    samples=args.samples,
                )
                _write(output_dir, f"sse_replay_c{concurrency}.json", result, environment)
                print(json.dumps({"file": f"sse_replay_c{concurrency}.json", "summary": result}, ensure_ascii=False))
            resume = await _disconnect_resume(
                client, database, headers, completed, args.resume_samples
            )
            _write(output_dir, "sse_disconnect_resume.json", resume, environment)
            print(json.dumps({"file": "sse_disconnect_resume.json", "summary": resume}, ensure_ascii=False))
        for concurrency in args.concurrency:
            result = await _continuation_point(
                database, concurrency=concurrency, samples=args.samples
            )
            all_run_ids.update(result.pop("run_ids"))
            all_continuation_ids.update(result.pop("continuation_ids"))
            _write(output_dir, f"continuation_claim_c{concurrency}.json", result, environment)
            print(json.dumps({"file": f"continuation_claim_c{concurrency}.json", "summary": result}, ensure_ascii=False))
        reaper = await _reaper_check(database)
        all_run_ids.update(reaper.pop("run_ids"))
        all_continuation_ids.update(reaper.pop("continuation_ids"))
        _write(output_dir, "continuation_reaper.json", reaper, environment)
        print(json.dumps({"file": "continuation_reaper.json", "summary": reaper}, ensure_ascii=False))
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        log_output.close()
        try:
            await _cleanup(database, user_id, all_run_ids, all_continuation_ids)
        finally:
            await database.dispose()
        try:
            log_path.unlink()
        except OSError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 5, 10, 25])
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--resume-samples", type=int, default=20)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--startup-timeout", type=float, default=30.0)
    parser.add_argument("--request-timeout", type=float, default=15.0)
    parser.add_argument("--terminal-timeout", type=float, default=60.0)
    parser.add_argument("--active-run-slots", type=int)
    parser.add_argument("--run-start-only", action="store_true")
    parser.add_argument("--confirmation-run", type=int)
    args = parser.parse_args()
    if (
        args.samples <= 0
        or args.resume_samples <= 0
        or any(value <= 0 for value in args.concurrency)
        or (args.active_run_slots is not None and args.active_run_slots <= 0)
        or (args.confirmation_run is not None and args.confirmation_run <= 0)
    ):
        parser.error(
            "samples, resume-samples, concurrency, active-run-slots and "
            "confirmation-run must be positive"
        )
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
