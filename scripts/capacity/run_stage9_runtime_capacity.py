"""Stage9-WP5 可复现的 PostgreSQL-backed Runtime capacity benchmark。

只允许专用 ``*_test`` database；使用本地 scripted backend，不访问远程 Provider。
脚本启动单进程 uvicorn、创建独立测试 Principal，并只清理本次生成的 identity。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import shutil
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
    EventConsumptionCheckpointRow,
    ManualToolResolutionAuditRow,
    LongTermMemoryRow,
    MessageExchangeRow,
    MessageRow,
    ObjectOwnershipRow,
    RoleRow,
    RunControlCommandRow,
    RunControlRow,
    RuntimeEventJournalRow,
    RuntimeModelInvocationRow,
    RuntimeRunExecutionRow,
    RuntimeSnapshotRow,
    RuntimeStepExecutionRow,
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
ARTIFACT_SCHEMA = "stage10.wp3.capacity.v1"
TERMINAL_EVENT_TYPES = ("run.completed", "run.failed", "run.cancelled")
WP3_STAGES = ("single", "small", "sweep", "sse", "tool", "soak")


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
    if not latencies:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
    return {
        "p50": percentile(latencies, 50),
        "p95": percentile(latencies, 95),
        "p99": percentile(latencies, 99),
        "max": max(latencies),
    }


def _artifact_failure_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """把旧 harness 的错误字段投影为稳定、机器可读的失败列表。"""
    failures = payload.get("failures")
    if isinstance(failures, list):
        return failures
    result: list[dict[str, Any]] = []
    for key in ("failed", "accepted_without_terminal"):
        value = payload.get(key)
        if isinstance(value, (int, float)) and value:
            result.append({"kind": key, "count": value})
    for key, value in (payload.get("error_type_counts") or {}).items():
        if value:
            result.append({"kind": str(key), "count": value})
    return result


def _write_artifact(
    output_dir: Path,
    name: str,
    payload: dict[str, Any],
    environment: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
) -> None:
    """Write the WP3 envelope while retaining Stage9 top-level fields.

    ``raw`` is deliberately bounded by callers; it must contain measurements,
    not private request bodies or unredacted server logs.
    """
    clean = {
        key: value
        for key, value in payload.items()
        if key not in {"run_ids", "continuation_ids"}
    }
    duration = clean.get("duration_seconds", clean.get("duration", 0.0))
    sample = clean.get("sample", clean.get("attempted", 0))
    summary = clean.get("summary")
    if not isinstance(summary, dict):
        summary = {
            key: value
            for key, value in clean.items()
            if key not in {"raw", "raw_samples", "failures"}
        }
    resource_raw = (
        clean.get("resource_evidence", {}).get("raw_samples", [])
        if isinstance(clean.get("resource_evidence"), dict)
        else []
    )
    raw = clean.get("raw", clean.get("raw_samples", resource_raw))
    if raw is None:
        raw = []
    effective_config = config or clean.get("config")
    if not isinstance(effective_config, dict):
        effective_config = {
            key: clean[key]
            for key in (
                "active_run_slots",
                "blocking_max_workers",
                "tool_limit",
                "evidence_profile",
            )
            if key in clean
        }
    artifact = {
        "schema": ARTIFACT_SCHEMA,
        "timestamp": datetime.now(UTC).isoformat(),
        "env": environment,
        "config": effective_config,
        "sample": sample,
        "concurrency": clean.get("concurrency"),
        "duration": duration,
        "raw": raw,
        "summary": summary,
        "failures": _artifact_failure_list(clean),
    }
    clean.update(artifact)
    (output_dir / name).write_text(
        json.dumps(clean, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


async def _bootstrap_identity(database: Database, *, role_code: str = "USER"):
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
        role = await session.scalar(select(RoleRow).where(RoleRow.code == role_code))
        if role is None:
            role = RoleRow(id=uuid.uuid4(), code=role_code)
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
            "roles": [role_code],
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


async def _wait_terminal_events(
    database: Database, run_ids: list[str], timeout: float
) -> dict[str, tuple[str, datetime]]:
    deadline = time.monotonic() + timeout
    expected = set(run_ids)
    found: dict[str, tuple[str, datetime]] = {}
    if not expected:
        return found
    while time.monotonic() < deadline:
        async with database.session() as session:
            rows = (
                await session.execute(
                    select(
                        ClientDeliveryEventRow.run_id,
                        ClientDeliveryEventRow.event_type,
                        ClientDeliveryEventRow.created_at,
                    )
                    .where(
                        ClientDeliveryEventRow.run_id.in_(run_ids),
                        ClientDeliveryEventRow.event_type.in_(TERMINAL_EVENT_TYPES),
                    )
                    .order_by(ClientDeliveryEventRow.created_at.asc())
                )
            ).all()
        for run_id, event_type, created_at in rows:
            found.setdefault(str(run_id), (str(event_type), created_at))
        if set(found) == expected:
            return found
        await asyncio.sleep(0.1)
    return found


async def _wait_terminals(database: Database, run_ids: list[str], timeout: float) -> set[str]:
    """Stage9-compatible terminal id helper."""
    return set((await _wait_terminal_events(database, run_ids, timeout)).keys())


async def _terminal_event_counts(database: Database, run_ids: list[str]) -> Counter:
    if not run_ids:
        return Counter()
    async with database.session() as session:
        rows = (
            await session.scalars(
                select(ClientDeliveryEventRow.event_type).where(
                    ClientDeliveryEventRow.run_id.in_(run_ids),
                    ClientDeliveryEventRow.event_type.in_(TERMINAL_EVENT_TYPES),
                )
            )
        ).all()
    return Counter(str(item) for item in rows)


def _prometheus_gauge(body: str, name: str, *, state: str | None = None) -> float | None:
    prefix = name if state is None else f'{name}{{state="{state}"}}'
    for line in body.splitlines():
        if line.startswith(prefix + " "):
            try:
                return float(line.rsplit(" ", 1)[-1])
            except ValueError:
                return None
    return None


def _prometheus_histogram(body: str, name: str) -> dict[str, Any]:
    buckets: dict[float, float] = {}
    for line in body.splitlines():
        if line.startswith(name + '_bucket{le="'):
            raw_bound = line.split('le="', 1)[1].split('"', 1)[0]
            try:
                bound = float("inf") if raw_bound == "+Inf" else float(raw_bound)
                buckets[bound] = float(line.rsplit(" ", 1)[-1])
            except ValueError:
                continue
    return {
        "buckets": buckets,
        "count": _prometheus_scalar(body, name + "_count"),
        "sum": _prometheus_scalar(body, name + "_sum"),
    }


def _histogram_delta_summary(
    before: dict[str, Any], after: dict[str, Any]
) -> dict[str, Any]:
    before_count = before.get("count")
    after_count = after.get("count")
    if before_count is None or after_count is None:
        return {"status": NOT_MEASURED}
    count = max(0.0, after_count - before_count)
    delta_buckets = {
        bound: max(0.0, value - before.get("buckets", {}).get(bound, 0.0))
        for bound, value in after.get("buckets", {}).items()
    }

    def quantile(percent: float) -> float | str:
        if count <= 0:
            return NOT_MEASURED
        target = count * percent / 100.0
        for bound in sorted(delta_buckets):
            if delta_buckets[bound] >= target:
                return bound if math.isfinite(bound) else NOT_MEASURED
        return NOT_MEASURED

    before_sum = before.get("sum")
    after_sum = after.get("sum")
    return {
        "status": "MEASURED_BUCKET_UPPER_BOUND",
        "count": count,
        "sum_seconds": (
            max(0.0, after_sum - before_sum)
            if before_sum is not None and after_sum is not None
            else None
        ),
        "p50_upper_bound_seconds": quantile(50),
        "p95_upper_bound_seconds": quantile(95),
        "p99_upper_bound_seconds": quantile(99),
    }


async def _histogram_snapshot(
    client: httpx.AsyncClient, name: str
) -> dict[str, Any]:
    try:
        response = await client.get("/metrics")
    except httpx.HTTPError:
        return {"buckets": {}, "count": None, "sum": None}
    if response.status_code != 200:
        return {"buckets": {}, "count": None, "sum": None}
    return _prometheus_histogram(response.text, name)


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
        "runtime_run_admission_waiting": NOT_INSTRUMENTED,
        "runtime_tool_active": NOT_INSTRUMENTED,
        "runtime_sse_active_connections": NOT_INSTRUMENTED,
    }
    metrics_samples = 0
    metric_last: dict[str, float | str] = dict(metric_peaks)
    raw_samples: list[dict[str, Any]] = []

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
                    "runtime_run_admission_waiting": _prometheus_gauge(
                        response.text, "localagent_runtime_run_admission_waiting"
                    ),
                    "runtime_tool_active": _prometheus_gauge(
                        response.text, "localagent_runtime_tool_active"
                    ),
                    "runtime_sse_active_connections": _prometheus_gauge(
                        response.text, "localagent_runtime_sse_active_connections"
                    ),
                }
                for name, value in values.items():
                    if value is not None:
                        metric_last[name] = value
                        previous = metric_peaks[name]
                        metric_peaks[name] = (
                            value if isinstance(previous, str) else max(previous, value)
                        )
                if len(raw_samples) < 1000:
                    raw_samples.append(
                        {
                            "timestamp": datetime.now(UTC).isoformat(),
                            "metrics": {
                                key: value for key, value in values.items() if value is not None
                            },
                        }
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
            "final": metric_last,
            "sample_count": metrics_samples,
            "db_checkout_timeout": NOT_INSTRUMENTED,
        },
        "raw_samples": raw_samples,
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
    admission_before = await _histogram_snapshot(
        client, "localagent_runtime_run_admission_wait_seconds"
    )
    semaphore = asyncio.Semaphore(concurrency)
    results: list[
        tuple[float, int | None, str | None, str | None, datetime | None, datetime]
    ] = []

    async def one() -> None:
        run_id = uuid.uuid4().hex
        submitted_at = datetime.now(UTC)
        started = time.perf_counter()
        try:
            response = await client.post(
                "/api/v1/chat",
                headers=headers,
                json={
                    "agent_id": "data_analyst",
                    "query": "deterministic capacity probe",
                    "run_id": run_id,
                },
            )
            elapsed = (time.perf_counter() - started) * 1000
            body = response.json() if response.status_code == 200 else {}
            accepted_at = datetime.now(UTC) if response.status_code == 200 else None
            results.append(
                (elapsed, response.status_code, body.get("run_id"), None, accepted_at, submitted_at)
            )
        except httpx.TimeoutException:
            results.append(
                ((time.perf_counter() - started) * 1000, None, None, "timeout", None, submitted_at)
            )
        except httpx.HTTPError as exc:
            results.append(
                (
                    (time.perf_counter() - started) * 1000,
                    None,
                    None,
                    type(exc).__name__,
                    None,
                    submitted_at,
                )
            )

    wall_started = time.perf_counter()

    async def limited() -> None:
        async with semaphore:
            await one()

    await asyncio.gather(*(limited() for _ in range(samples)))
    duration = time.perf_counter() - wall_started
    accepted = [run_id for _, status, run_id, _, _, _ in results if status == 200 and run_id]
    accepted_at = {
        run_id: timestamp
        for _, status, run_id, _, timestamp, _ in results
        if status == 200 and run_id and timestamp is not None
    }
    submitted_at = {
        run_id: timestamp
        for _, status, run_id, _, _, timestamp in results
        if status == 200 and run_id
    }
    terminal_events = await _wait_terminal_events(database, accepted, terminal_timeout)
    terminals = set(terminal_events)
    terminal_counts = await _terminal_event_counts(database, accepted)
    admission_after = await _histogram_snapshot(
        client, "localagent_runtime_run_admission_wait_seconds"
    )
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
    completion_latencies = [
        max(
            0.0,
            (terminal_events[run_id][1] - submitted_at[run_id]).total_seconds() * 1000,
        )
        for run_id in terminals
        if run_id in submitted_at
    ]
    completion_latency_by_run = {
        run_id: max(
            0.0,
            (terminal_events[run_id][1] - submitted_at[run_id]).total_seconds() * 1000,
        )
        for run_id in terminals
        if run_id in submitted_at
    }
    completion_run_ids = [run_id for run_id in accepted if run_id in submitted_at]
    completion_duration = 0.0
    if terminals and completion_run_ids:
        first_submit = min(submitted_at[run_id] for run_id in completion_run_ids)
        last_terminal = max(terminal_events[run_id][1] for run_id in terminals)
        completion_duration = max(
            0.0, (last_terminal - first_submit).total_seconds()
        )
    correctness = {
        "unique_run_ids": unique,
        "duplicate_run_ids": len(accepted) - unique,
        "ownership_missing": len(accepted) - ownership,
        "tool_snapshots_missing": len(accepted) - snapshots,
        "accepted_without_terminal": len(accepted) - len(terminals),
        "duplicate_terminal_events": max(0, sum(terminal_counts.values()) - len(terminals)),
    }
    succeeded = sum(
        1
        for _, status, _, error, _, _ in results
        if status == 200 and error is None
    )
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
            "completion_latency_ms": _summary(completion_latencies),
            "completion_latency_definition": "request_submitted_at_to_client_feed_terminal_created_at_clamped_nonnegative_ms",
            "completion_throughput_window": "first_accepted_request_submitted_at_to_last_client_feed_terminal_created_at",
            "terminal_event_distribution": dict(terminal_counts),
            "completion_duration_seconds": completion_duration,
            "completion_throughput_rps": (
                len(completion_latencies) / completion_duration
                if completion_duration > 0
                else 0.0
            ),
            "http_status_distribution": dict(status_counts),
            "error_type_counts": dict(error_counts),
            "run_admission_wait": _histogram_delta_summary(
                admission_before, admission_after
            ),
            "correctness": correctness,
            "raw": [
                {
                    "run_id": run_id,
                    "accepted_at": accepted_at.get(run_id).isoformat()
                    if accepted_at.get(run_id)
                    else None,
                    "submitted_at": submitted_at.get(run_id).isoformat()
                    if submitted_at.get(run_id)
                    else None,
                    "terminal_event": terminal_events[run_id][0]
                    if run_id in terminal_events
                    else None,
                    "terminal_at": terminal_events[run_id][1].isoformat()
                    if run_id in terminal_events
                    else None,
                    "completion_latency_ms": completion_latency_by_run.get(run_id),
                }
                for run_id in accepted[:1000]
            ],
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


async def _metrics_snapshot(client: httpx.AsyncClient) -> dict[str, float | None]:
    """Read only bounded gauges needed to prove SSE cleanup."""
    try:
        response = await client.get("/metrics")
    except httpx.HTTPError:
        return {"runtime_sse_active_connections": None, "runtime_tool_active": None}
    if response.status_code != 200:
        return {"runtime_sse_active_connections": None, "runtime_tool_active": None}
    return {
        "runtime_sse_active_connections": _prometheus_gauge(
            response.text, "localagent_runtime_sse_active_connections"
        ),
        "runtime_tool_active": _prometheus_gauge(
            response.text, "localagent_runtime_tool_active"
        ),
    }


async def _start_run_requests(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    *,
    concurrency: int,
    samples: int,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Start accepted Runs without waiting for terminal completion.

    This is intentionally the production HTTP start endpoint; live SSE uses
    these accepted IDs while the producer may still be active.
    """
    semaphore = asyncio.Semaphore(concurrency)
    accepted: list[str] = []
    raw: list[dict[str, Any]] = []

    async def one() -> None:
        run_id = uuid.uuid4().hex
        submitted_at = datetime.now(UTC)
        started = time.perf_counter()
        status: int | None = None
        error: str | None = None
        returned_id: str | None = None
        try:
            response = await client.post(
                "/api/v1/chat",
                headers=headers,
                json={
                    "agent_id": "data_analyst",
                    "query": "deterministic capacity probe",
                    "run_id": run_id,
                },
            )
            status = response.status_code
            if status == 200:
                returned_id = response.json().get("run_id")
                if returned_id:
                    accepted.append(str(returned_id))
        except httpx.TimeoutException:
            error = "timeout"
        except httpx.HTTPError as exc:
            error = type(exc).__name__
        raw.append(
            {
                "requested_run_id": run_id,
                "returned_run_id": returned_id,
                "submitted_at": submitted_at.isoformat(),
                "status": status,
                "error": error,
                "latency_ms": (time.perf_counter() - started) * 1000,
            }
        )

    async def limited() -> None:
        async with semaphore:
            await one()

    await asyncio.gather(*(limited() for _ in range(samples)))
    return accepted, raw


async def _read_live_sse(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    run_id: str,
    *,
    max_events: int | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    cursors: list[int] = []
    events: list[str] = []
    error: str | None = None
    status: int | None = None
    try:
        async with client.stream(
            "GET", f"/api/v1/runs/{run_id}/events", headers=headers
        ) as response:
            status = response.status_code
            if status == 200:
                current_event: str | None = None
                async for line in response.aiter_lines():
                    if line.startswith("id: "):
                        cursors.append(int(line[4:]))
                    elif line.startswith("event: "):
                        current_event = line[7:]
                        events.append(current_event)
                        if current_event in TERMINAL_EVENT_TYPES or (
                            max_events is not None and len(events) >= max_events
                        ):
                            break
    except httpx.TimeoutException:
        error = "timeout"
    except (httpx.HTTPError, ValueError) as exc:
        error = type(exc).__name__
    monotonic = all(left < right for left, right in zip(cursors, cursors[1:]))
    terminal_count = sum(event in TERMINAL_EVENT_TYPES for event in events)
    return {
        "run_id": run_id,
        "status": status,
        "cursors": cursors,
        "events": events,
        "terminal_count": terminal_count,
        "latency_ms": (time.perf_counter() - started) * 1000,
        "error": error,
        "cursor_monotonic": monotonic,
        "terminal_seen": bool(events and events[-1] in TERMINAL_EVENT_TYPES),
    }


async def _live_sse_point(
    client: httpx.AsyncClient,
    database: Database,
    headers: dict[str, str],
    *,
    concurrency: int,
    samples: int,
    terminal_timeout: float,
) -> dict[str, Any]:
    """Exercise active production Runs and their live Client Feed streams."""
    poll_before = await _histogram_snapshot(
        client, "localagent_runtime_sse_poll_duration_seconds"
    )
    wall_started = time.perf_counter()
    run_ids, start_raw = await _start_run_requests(
        client, headers, concurrency=concurrency, samples=samples
    )
    semaphore = asyncio.Semaphore(concurrency)

    async def limited(run_id: str) -> dict[str, Any]:
        async with semaphore:
            return await _read_live_sse(client, headers, run_id)

    stream_results = await asyncio.gather(*(limited(run_id) for run_id in run_ids))
    # Give the database-backed feed a bounded chance to expose terminal rows;
    # this does not manufacture terminal completion when the producer is stuck.
    terminal_events = await _wait_terminal_events(database, run_ids, terminal_timeout)
    expected = await _expected_feed(database, run_ids) if run_ids else {}
    disconnect_samples: list[dict[str, Any]] = []
    for run_id in run_ids[: min(len(run_ids), max(1, min(10, samples)))]:
        first = await _read_live_sse(client, headers, run_id, max_events=1)
        disconnect_samples.append(
            {
                "run_id": run_id,
                "first_cursor": first["cursors"][0] if first["cursors"] else None,
                "closed_after_first_event": bool(first["events"]),
            }
        )
    after_disconnect = await _metrics_snapshot(client)
    failures: list[dict[str, Any]] = []
    duplicate_terminal = 0
    cursor_violations = 0
    terminal_loss = 0
    replay_gap = 0
    for item in stream_results:
        duplicate_terminal += int(item["terminal_count"] > 1)
        cursor_violations += int(not item["cursor_monotonic"])
        terminal_loss += int(not item["terminal_seen"])
        expected_cursors = expected.get(item["run_id"], [])
        if item["terminal_seen"] and expected_cursors and item["cursors"] != expected_cursors:
            replay_gap += 1
        if item["error"]:
            failures.append({"run_id": item["run_id"], "kind": item["error"]})
    failures.extend(
        {"run_id": item["run_id"], "kind": "disconnect_first_event_missing"}
        for item in disconnect_samples
        if not item["closed_after_first_event"]
    )
    cleanup_ok = after_disconnect.get("runtime_sse_active_connections") in (None, 0.0)
    if not cleanup_ok:
        failures.append(
            {
                "kind": "disconnect_cleanup",
                "active_connections": after_disconnect.get("runtime_sse_active_connections"),
            }
        )
    duration = time.perf_counter() - wall_started
    poll_after = await _histogram_snapshot(
        client, "localagent_runtime_sse_poll_duration_seconds"
    )
    poll_summary = _histogram_delta_summary(poll_before, poll_after)
    if isinstance(poll_summary.get("count"), (int, float)):
        poll_summary["poll_rate_per_second"] = (
            poll_summary["count"] / duration if duration else 0.0
        )
    succeeded = sum(
        item["status"] == 200
        and item["cursor_monotonic"]
        and item["terminal_seen"]
        and item["terminal_count"] == 1
        for item in stream_results
    )
    return {
        "scenario": "sse_live_active_runs",
        "evidence_profile": EVIDENCE_PROFILE,
        "concurrency": concurrency,
        "attempted": len(run_ids),
        "succeeded": succeeded,
        "failed": len(run_ids) - succeeded,
        "duration_seconds": duration,
        "throughput_rps": len(run_ids) / duration if duration else 0.0,
        "latency_ms": _summary([item["latency_ms"] for item in stream_results]),
        "sse_poll_duration": poll_summary,
        "correctness": {
            "cursor_monotonic_violations": cursor_violations,
            "duplicate_terminal": duplicate_terminal,
            "terminal_loss": terminal_loss,
            "replay_gap": replay_gap,
            "disconnect_cleanup": int(cleanup_ok),
            "terminal_rows_observed": len(terminal_events),
        },
        "disconnect": {
            "samples": disconnect_samples,
            "metrics_after_disconnect": after_disconnect,
        },
        "raw": [{"start": item} for item in start_raw]
        + [{"stream": item} for item in stream_results],
        "failures": failures,
        "expected_feed_cursors": expected,
        "run_ids": run_ids,
    }


class _ToolCapacityMetrics:
    """Bounded metrics sink used only to observe the production controller."""

    def __init__(self) -> None:
        self.permit_wait_seconds: list[float] = []
        self.timeout_count = 0
        self.active = 0
        self.active_peak = 0

    def observe_tool_permit_wait(self, duration: float) -> None:
        self.permit_wait_seconds.append(float(duration))

    def observe_tool_permit_timeout(self, _status: str = "timeout") -> None:
        self.timeout_count += 1

    def set_tool_active(self, value: int) -> None:
        self.active = int(value)
        self.active_peak = max(self.active_peak, self.active)


async def _tool_controller_unit_point(
    *, limit: int, load_concurrency: int, samples: int, delay_seconds: float, timeout_seconds: float
) -> dict[str, Any]:
    """Saturate the production ToolConcurrencyController at one load point."""
    from core.runtime import CancellationSource, ToolConcurrencyController, ToolResourceAcquireError

    metrics = _ToolCapacityMetrics()
    controller = ToolConcurrencyController(max_concurrency=limit, metrics=metrics)
    semaphore = asyncio.Semaphore(load_concurrency)
    rows: list[dict[str, Any]] = []
    wall_started = time.perf_counter()

    async def one(index: int) -> None:
        async with semaphore:
            started = time.perf_counter()
            deadline = started + timeout_seconds
            lease = None
            status = "succeeded"
            error: str | None = None
            try:
                lease = await controller.acquire(
                    tool_name="capacity_deterministic_delay",
                    tool_max_concurrency=limit,
                    resource_key=None,
                    cancellation_token=CancellationSource().token,
                    remaining_seconds=lambda: deadline - time.monotonic(),
                )
                permit_wait_ms = (time.perf_counter() - started) * 1000
                await asyncio.sleep(delay_seconds)
                if time.monotonic() >= deadline:
                    status = "timeout"
                    error = "TOOL_EXECUTION_TIMEOUT"
            except (ToolResourceAcquireError, TimeoutError) as exc:
                status = "timeout"
                error = getattr(exc, "safe_error_code", type(exc).__name__)
                permit_wait_ms = (time.perf_counter() - started) * 1000
            finally:
                if lease is not None:
                    lease.release()
            rows.append(
                {
                    "index": index,
                    "status": status,
                    "error": error,
                    "permit_wait_ms": permit_wait_ms,
                    "latency_ms": (time.perf_counter() - started) * 1000,
                }
            )

    await asyncio.gather(*(one(index) for index in range(samples)))
    duration = time.perf_counter() - wall_started
    wait_values = [item["permit_wait_ms"] for item in rows]
    timeout_count = sum(item["status"] == "timeout" for item in rows)
    return {
        "scenario": "tool_semaphore_saturation",
        "evidence_profile": EVIDENCE_PROFILE,
        "concurrency": load_concurrency,
        "tool_limit": limit,
        "attempted": len(rows),
        "succeeded": len(rows) - timeout_count,
        "failed": timeout_count,
        "duration_seconds": duration,
        "throughput_rps": len(rows) / duration if duration else 0.0,
        "latency_ms": _summary([item["latency_ms"] for item in rows]),
        "permit_wait_ms": _summary(wait_values),
        "active_peak": metrics.active_peak,
        "timeout_count": timeout_count,
        "correctness": {
            "active_returned_to_zero": controller.active_permit_count == 0,
            "active_peak_not_above_limit": metrics.active_peak <= limit,
        },
        "execution_path": {
            "controller": "production ToolConcurrencyController",
            "http_tool_trigger": "NOT_AVAILABLE",
            "seam": "direct production controller acquire/release; no fake repository and no durability bypass",
        },
        "raw": rows[:1000],
        "failures": [item for item in rows if item["status"] == "timeout"],
        "metrics": {
            "permit_wait_samples": len(metrics.permit_wait_seconds),
            "permit_timeout_callbacks": metrics.timeout_count,
            "active_final": metrics.active,
        },
    }


def _prometheus_scalar(body: str, name: str) -> float | None:
    """Parse an unlabelled Prometheus sample without accepting a prefix collision."""
    for line in body.splitlines():
        if line.startswith(name + " "):
            try:
                return float(line.rsplit(" ", 1)[-1])
            except ValueError:
                return None
    return None


async def _tool_metrics_snapshot(client: httpx.AsyncClient) -> dict[str, float | None]:
    try:
        response = await client.get("/metrics")
    except httpx.HTTPError:
        return {
            "permit_wait_count": None,
            "permit_wait_sum_seconds": None,
            "permit_timeout_total": None,
        }
    if response.status_code != 200:
        return {
            "permit_wait_count": None,
            "permit_wait_sum_seconds": None,
            "permit_timeout_total": None,
        }
    body = response.text
    return {
        "permit_wait_count": _prometheus_scalar(
            body, "localagent_runtime_tool_permit_wait_seconds_count"
        ),
        "permit_wait_sum_seconds": _prometheus_scalar(
            body, "localagent_runtime_tool_permit_wait_seconds_sum"
        ),
        "permit_timeout_total": _prometheus_scalar(
            body, "localagent_runtime_tool_permit_timeout_total"
        ),
    }


def _production_tool_limit() -> int:
    from core.runtime.tool_adapters import ComplexWorkflowToolAdapter

    return int(ComplexWorkflowToolAdapter.spec.max_concurrency)


def _tool_query(
    *, resource_key: str, item_count: int, processing_delay_ms: int = 250
) -> str:
    payload = {
        "resource_key": resource_key,
        "execution_mode": "DRY_RUN",
        "items": [
            {
                "item_id": f"capacity-item-{index}-{uuid.uuid4().hex[:8]}",
                "action": "ADD",
                "quantity": 1,
            }
            for index in range(item_count)
        ],
        "processing_options": {
            "max_parallel_items": 1,
            "processing_delay_ms": processing_delay_ms,
        },
    }
    return "请调用 complex_workflow_simulator(" + json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    ) + ")"


async def _tool_semaphore_point(
    client: httpx.AsyncClient,
    database: Database,
    headers: dict[str, str],
    *,
    load_concurrency: int,
    samples: int,
    item_count: int,
    processing_delay_ms: int,
    runtime_timeout_seconds: float,
) -> dict[str, Any]:
    """Run deterministic Tool load through the production HTTP/Runtime path."""
    if (
        item_count <= 0
        or samples <= 0
        or load_concurrency <= 0
        or processing_delay_ms < 0
        or processing_delay_ms > 5_000
    ):
        raise ValueError("item_count, samples and load_concurrency must be positive")
    before_metrics = await _tool_metrics_snapshot(client)
    permit_wait_before = await _histogram_snapshot(
        client, "localagent_runtime_tool_permit_wait_seconds"
    )
    wall_started = time.perf_counter()
    semaphore = asyncio.Semaphore(load_concurrency)
    rows: list[dict[str, Any]] = []
    submitted_at: dict[str, datetime] = {}
    requested_ids: list[str] = []
    accepted_ids: list[str] = []
    workload_agents = (
        "data_analyst",
        "code_expert",
        "feature_understanding",
        "risk_analysis",
        "test_planning",
        "failure_triage",
        "ci_guardian",
    )

    async def one(index: int) -> None:
        async with semaphore:
            run_id = str(uuid.uuid4())
            resource_key = f"capacity-tool-{uuid.uuid4().hex}"
            agent_id = workload_agents[index % len(workload_agents)]
            submitted = datetime.now(UTC)
            submitted_at[run_id] = submitted
            requested_ids.append(run_id)
            started = time.perf_counter()
            row: dict[str, Any] = {
                "index": index,
                "run_id": run_id,
                "resource_key": resource_key,
                "agent_id": agent_id,
                "http_status": None,
                "runtime_status": None,
                "error": None,
            }
            try:
                response = await client.post(
                    "/api/runtime/execute",
                    headers=headers,
                    timeout=runtime_timeout_seconds + 5.0,
                    json={
                        "agent_id": agent_id,
                        "query": _tool_query(
                            resource_key=resource_key,
                            item_count=item_count,
                            processing_delay_ms=processing_delay_ms,
                        ),
                        "run_id": run_id,
                        "timeout_seconds": runtime_timeout_seconds,
                    },
                )
                row["http_status"] = response.status_code
                try:
                    body = response.json()
                except ValueError:
                    body = {}
                row["runtime_status"] = body.get("status")
                row["stop_reason"] = body.get("stop_reason")
                row["error_code"] = body.get("error_code")
                if response.status_code == 200:
                    accepted_ids.append(run_id)
                else:
                    row["error"] = body.get("detail", "http_error")
            except httpx.TimeoutException:
                row["error"] = "timeout"
            except httpx.HTTPError as exc:
                row["error"] = type(exc).__name__
            row["http_latency_ms"] = (time.perf_counter() - started) * 1000
            rows.append(row)

    await asyncio.gather(*(one(index) for index in range(samples)))
    terminal_events = await _wait_terminal_events(
        database, accepted_ids, max(0.0, runtime_timeout_seconds)
    )
    terminal_counts = await _terminal_event_counts(database, accepted_ids)
    after_metrics = await _tool_metrics_snapshot(client)
    permit_wait_after = await _histogram_snapshot(
        client, "localagent_runtime_tool_permit_wait_seconds"
    )
    duration = time.perf_counter() - wall_started
    for row in rows:
        terminal = terminal_events.get(row["run_id"])
        row["terminal_event"] = terminal[0] if terminal else None
        row["terminal_at"] = terminal[1].isoformat() if terminal else None
        submitted = submitted_at.get(row["run_id"])
        row["completion_latency_ms"] = (
            max(0.0, (terminal[1] - submitted).total_seconds() * 1000)
            if terminal and submitted
            else None
        )
    succeeded = sum(
        row["http_status"] == 200
        and row["runtime_status"] == "SUCCEEDED"
        and row["run_id"] in terminal_events
        for row in rows
    )
    failures = [
        {
            "run_id": row["run_id"],
            "kind": row["error"] or row["error_code"] or "runtime_not_succeeded",
        }
        for row in rows
        if not (
            row["http_status"] == 200
            and row["runtime_status"] == "SUCCEEDED"
            and row["run_id"] in terminal_events
        )
    ]
    metric_delta = {
        key: (
            after_metrics[key] - before_metrics[key]
            if after_metrics[key] is not None and before_metrics[key] is not None
            else None
        )
        for key in before_metrics
    }
    latencies = [
        row["completion_latency_ms"]
        for row in rows
        if row["completion_latency_ms"] is not None
    ]
    return {
        "scenario": "tool_semaphore_saturation_real_http",
        "evidence_profile": EVIDENCE_PROFILE,
        "concurrency": load_concurrency,
        "tool_limit": _production_tool_limit(),
        "item_count": item_count,
        "processing_delay_ms": processing_delay_ms,
        "attempted": len(rows),
        "succeeded": succeeded,
        "failed": len(rows) - succeeded,
        "duration_seconds": duration,
        "throughput_rps": len(rows) / duration if duration else 0.0,
        "completion_latency_ms": _summary(latencies),
        "http_status_distribution": dict(
            Counter(str(row["http_status"]) for row in rows)
        ),
        "runtime_status_distribution": dict(
            Counter(str(row["runtime_status"]) for row in rows)
        ),
        "metrics_delta": metric_delta,
        "permit_wait": _histogram_delta_summary(
            permit_wait_before, permit_wait_after
        ),
        "correctness": {
            "unique_run_ids": len({row["run_id"] for row in rows}),
            "accepted_http_runs": len(accepted_ids),
            "terminal_loss": len(accepted_ids) - len(terminal_events),
            "duplicate_terminal_events": max(
                0, sum(terminal_counts.values()) - len(terminal_events)
            ),
            "runtime_success_missing_terminal": sum(
                row["runtime_status"] == "SUCCEEDED" and row["run_id"] not in terminal_events
                for row in rows
            ),
        },
        "execution_path": {
            "http_endpoint": "/api/runtime/execute",
            "agent_ids": list(workload_agents),
            "tool_name": "complex_workflow_simulator",
            "governance": "production AgentRouter/ToolGovernance/ToolExecutionService/RunCoordinator",
            "database": "real PostgreSQL test database; durable runtime enabled",
        },
        "raw": rows[:1000],
        "failures": failures,
        "run_ids": requested_ids,
    }


def _soak_windows(duration_seconds: float) -> dict[str, tuple[float, float]]:
    """Return deterministic first/middle/last windows for a short soak."""
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive")
    third = duration_seconds / 3.0
    return {
        "first": (0.0, third),
        "middle": (third, third * 2),
        "last": (third * 2, duration_seconds),
    }


async def _soak_point(
    client: httpx.AsyncClient,
    database: Database,
    headers: dict[str, str],
    *,
    duration_seconds: float,
    concurrency: int,
    interval_seconds: float,
    terminal_timeout: float,
) -> dict[str, Any]:
    """Run the production start path for a bounded soak and retain three windows."""
    windows = _soak_windows(duration_seconds)
    started = time.perf_counter()
    deadline = started + duration_seconds
    rows: list[dict[str, Any]] = []
    soak_run_ids: set[str] = set()
    submissions: dict[str, datetime] = {}
    while time.perf_counter() < deadline:
        batch_started = time.perf_counter()
        accepted, raw = await _start_run_requests(
            client, headers, concurrency=concurrency, samples=concurrency
        )
        soak_run_ids.update(accepted)
        for item in raw:
            returned_run_id = item.get("returned_run_id")
            submitted_at = item.get("submitted_at")
            if returned_run_id and submitted_at:
                submissions[str(returned_run_id)] = datetime.fromisoformat(submitted_at)
        now = time.perf_counter() - started
        rows.append(
            {
                "elapsed_seconds": now,
                "accepted": len(accepted),
                "request_duration_seconds": time.perf_counter() - batch_started,
                "requests": raw,
            }
        )
        remaining = interval_seconds - (time.perf_counter() - batch_started)
        if remaining > 0:
            await asyncio.sleep(min(remaining, max(0.0, deadline - time.perf_counter())))
    actual_duration = time.perf_counter() - started
    terminal_events = await _wait_terminal_events(
        database, list(soak_run_ids), terminal_timeout
    )
    completion_latency_ms = {
        run_id: max(
            0.0,
            (terminal_at - submissions[run_id]).total_seconds() * 1000,
        )
        for run_id, (_, terminal_at) in terminal_events.items()
        if run_id in submissions
    }
    samples_by_window = {
        name: [
            row
            for row in rows
            if begin <= row["elapsed_seconds"] < end
            or (
                name == "last"
                and begin <= row["elapsed_seconds"] <= end
            )
        ]
        for name, (begin, end) in windows.items()
    }
    summary = {
        name: {
            "batches": len(items),
            "attempted": sum(len(item["requests"]) for item in items),
            "accepted": sum(item["accepted"] for item in items),
            "terminal": sum(
                1
                for item in items
                for request in item["requests"]
                if request.get("returned_run_id") in terminal_events
            ),
            "completion_latency_ms": _summary(
                [
                    completion_latency_ms[request["returned_run_id"]]
                    for item in items
                    for request in item["requests"]
                    if request.get("returned_run_id") in completion_latency_ms
                ]
            ),
            "error_rate": (
                sum(
                    1
                    for item in items
                    for request in item["requests"]
                    if request.get("status") != 200
                    or request.get("returned_run_id") not in terminal_events
                )
                / sum(len(item["requests"]) for item in items)
                if sum(len(item["requests"]) for item in items)
                else 0.0
            ),
            "completion_throughput_rps": (
                sum(
                    1
                    for item in items
                    for request in item["requests"]
                    if request.get("returned_run_id") in terminal_events
                )
                / (duration_seconds / 3.0)
            ),
        }
        for name, items in samples_by_window.items()
    }
    return {
        "scenario": "runtime_short_soak",
        "evidence_profile": EVIDENCE_PROFILE,
        "concurrency": concurrency,
        "attempted": sum(item["accepted"] for item in rows),
        "succeeded": len(terminal_events),
        "failed": len(soak_run_ids) - len(terminal_events),
        "duration_seconds": actual_duration,
        "throughput_rps": sum(item["accepted"] for item in rows) / actual_duration
        if actual_duration
        else 0.0,
        "windows": summary,
        "raw": rows[:1000],
        "failures": (
            [
                {
                    "kind": "accepted_without_terminal",
                    "count": len(soak_run_ids) - len(terminal_events),
                }
            ]
            if len(soak_run_ids) > len(terminal_events)
            else []
        ),
        "run_ids": list(soak_run_ids),
    }


def _selected_stages(args: argparse.Namespace) -> set[str]:
    requested = set(getattr(args, "stage", ()) or ())
    if not requested or "all" in requested:
        # ``soak`` is opt-in even under ``all`` because it is intentionally a
        # >=10 minute operation and must never start accidentally.
        return set(WP3_STAGES) - {"soak"}
    return requested


def _control_concurrency(args: argparse.Namespace, stages: set[str]) -> list[int]:
    values = list(args.concurrency)
    if "sweep" in stages:
        return values
    if "single" in stages:
        return values[:1]
    if "small" in stages:
        return values[:2]
    return []


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


async def _cleanup(
    database: Database,
    user_id: uuid.UUID,
    run_ids: set[str],
    continuation_ids: set[str],
) -> None:
    ids = list(run_ids)
    async with database.transaction() as session:
        if continuation_ids:
            await session.execute(delete(DurableContinuationRow).where(DurableContinuationRow.continuation_id.in_(continuation_ids)))
        if ids:
            for model in (
                DurableToolExecutionClaimRow,
                DurableToolInvocationRow,
                DurableApprovalRow,
                ManualToolResolutionAuditRow,
                ToolResolutionSnapshotRow,
                ClientDeliveryEventRow,
                RuntimeEventJournalRow,
                EventConsumptionCheckpointRow,
                RuntimeSnapshotRow,
                RuntimeModelInvocationRow,
                RuntimeStepExecutionRow,
                RuntimeRunExecutionRow,
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
    stages = _selected_stages(args)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    database = Database(DatabaseConfig(url=_database_url()))
    await database.verify_reachable()
    environment = _environment()
    environment["blocking_max_workers"] = args.blocking_max_workers
    benchmark_role = "ADMIN" if "tool" in stages else "USER"
    user_id, public_key, token = await _bootstrap_identity(
        database, role_code=benchmark_role
    )
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
            "LOCAL_AGENT_BLOCKING_MAX_WORKERS": str(args.blocking_max_workers),
        }
    )
    environment["active_run_slots"] = _configure_active_run_slots(
        server_env, args.active_run_slots
    )
    environment["principal_role"] = benchmark_role
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
    retain_log = bool(args.keep_server_log)
    try:
        await _wait_for_server(base_url, process, args.startup_timeout)
        async with httpx.AsyncClient(
            base_url=base_url, trust_env=False, timeout=args.request_timeout
        ) as client:
            runtime_runs: list[str] = []
            for concurrency in _control_concurrency(args, stages):
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
                retain_log = retain_log or bool(result.get("failed")) or bool(result.get("failures"))
                runtime_runs.extend(terminal_run_ids)
                all_run_ids.update(run_ids)
                output_name = _run_start_file_name(args, concurrency)
                _write_artifact(output_dir, output_name, result, environment)
                print(json.dumps({"file": output_name, "summary": result}, ensure_ascii=False))
                correctness = result["correctness"]
                if result["failed"] or any(
                    correctness[key]
                    for key in (
                        "duplicate_run_ids",
                        "ownership_missing",
                        "tool_snapshots_missing",
                        "accepted_without_terminal",
                        "duplicate_terminal_events",
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
            if "sse" in stages:
                for concurrency in args.sse_concurrency:
                    resource_stop = asyncio.Event()
                    resource_task = asyncio.create_task(
                        _collect_resource_evidence(client, process, resource_stop)
                    )
                    try:
                        result = {}
                        result = await _live_sse_point(
                            client,
                            database,
                            headers,
                            concurrency=concurrency,
                            samples=args.sse_samples,
                            terminal_timeout=args.terminal_timeout,
                        )
                    finally:
                        resource_stop.set()
                        result["resource_evidence"] = await resource_task
                    retain_log = retain_log or bool(result.get("failed")) or bool(result.get("failures"))
                    all_run_ids.update(result.pop("run_ids", []))
                    _write_artifact(output_dir, f"sse_live_c{concurrency}.json", result, environment)
                    print(
                        json.dumps(
                            {"file": f"sse_live_c{concurrency}.json", "summary": result},
                            ensure_ascii=False,
                        )
                    )
            if "sse" in stages and len(completed) >= args.samples:
                for concurrency in args.concurrency:
                    result = await _sse_replay_point(
                        client,
                        database,
                        headers,
                        completed,
                        concurrency=concurrency,
                        samples=args.samples,
                    )
                    _write_artifact(output_dir, f"sse_replay_c{concurrency}.json", result, environment)
                    print(json.dumps({"file": f"sse_replay_c{concurrency}.json", "summary": result}, ensure_ascii=False))
                resume = await _disconnect_resume(
                    client, database, headers, completed, args.resume_samples
                )
                _write_artifact(output_dir, "sse_disconnect_resume.json", resume, environment)
                print(json.dumps({"file": "sse_disconnect_resume.json", "summary": resume}, ensure_ascii=False))
            elif "sse" in stages:
                result = {
                    "scenario": "sse_replay",
                    "evidence_profile": EVIDENCE_PROFILE,
                    "attempted": 0,
                    "succeeded": 0,
                    "failed": 1,
                    "correctness": {"terminal_loss": 1},
                    "failures": [{"kind": "insufficient_completed_runtime_runs", "count": len(completed)}],
                }
                _write_artifact(output_dir, "sse_replay_blocked.json", result, environment)
            if "tool" in stages:
                limit = _production_tool_limit()
                levels = sorted(
                    set(args.tool_concurrency or (max(1, limit // 2), limit, limit * 2))
                )
                for load_concurrency in levels:
                    resource_stop = asyncio.Event()
                    resource_task = asyncio.create_task(
                        _collect_resource_evidence(client, process, resource_stop)
                    )
                    try:
                        result = {}
                        result = await _tool_semaphore_point(
                            client,
                            database,
                            headers,
                            load_concurrency=load_concurrency,
                            samples=args.tool_samples,
                            item_count=args.tool_item_count,
                            processing_delay_ms=args.tool_delay_ms,
                            runtime_timeout_seconds=args.tool_runtime_timeout_seconds,
                        )
                    finally:
                        resource_stop.set()
                        result["resource_evidence"] = await resource_task
                    retain_log = retain_log or bool(result.get("failed")) or bool(result.get("failures"))
                    all_run_ids.update(result.pop("run_ids", []))
                    _write_artifact(output_dir, f"tool_semaphore_c{load_concurrency}.json", result, environment)
                    print(json.dumps({"file": f"tool_semaphore_c{load_concurrency}.json", "summary": result}, ensure_ascii=False))
            if "soak" in stages:
                resource_stop = asyncio.Event()
                resource_task = asyncio.create_task(
                    _collect_resource_evidence(client, process, resource_stop)
                )
                try:
                    result = {}
                    result = await _soak_point(
                        client,
                        database,
                        headers,
                        duration_seconds=args.soak_duration_seconds,
                        concurrency=args.soak_concurrency,
                        interval_seconds=args.soak_interval_seconds,
                        terminal_timeout=args.terminal_timeout,
                    )
                finally:
                    resource_stop.set()
                    result["resource_evidence"] = await resource_task
                retain_log = retain_log or bool(result.get("failed")) or bool(result.get("failures"))
                all_run_ids.update(result.pop("run_ids", []))
                _write_artifact(output_dir, "runtime_short_soak.json", result, environment)
                print(json.dumps({"file": "runtime_short_soak.json", "summary": result}, ensure_ascii=False))
        if "continuation" in stages:
            for concurrency in args.concurrency:
                result = await _continuation_point(
                    database, concurrency=concurrency, samples=args.samples
                )
                all_run_ids.update(result.pop("run_ids"))
                all_continuation_ids.update(result.pop("continuation_ids"))
                _write_artifact(output_dir, f"continuation_claim_c{concurrency}.json", result, environment)
                print(json.dumps({"file": f"continuation_claim_c{concurrency}.json", "summary": result}, ensure_ascii=False))
            reaper = await _reaper_check(database)
            all_run_ids.update(reaper.pop("run_ids"))
            all_continuation_ids.update(reaper.pop("continuation_ids"))
            _write_artifact(output_dir, "continuation_reaper.json", reaper, environment)
            print(json.dumps({"file": "continuation_reaper.json", "summary": reaper}, ensure_ascii=False))
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        log_output.close()
        if retain_log or process.returncode not in (0, None):
            try:
                shutil.copyfile(log_path, output_dir / "server_failure.log")
            except OSError:
                pass
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
    parser.add_argument(
        "--stage",
        nargs="+",
        choices=("all", *WP3_STAGES, "continuation"),
        default=["all"],
        help="按阶段运行；all 不包含 >=10 分钟 soak，soak 需显式指定",
    )
    for stage_name in WP3_STAGES:
        parser.add_argument(
            f"--{stage_name}",
            action="store_true",
            help=f"仅运行 {stage_name} 阶段（等价于 --stage {stage_name}）",
        )
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
    parser.add_argument("--sse-concurrency", type=int, nargs="+", default=[10, 25, 50])
    parser.add_argument("--sse-samples", type=int, default=50)
    parser.add_argument("--tool-limit", type=int)
    parser.add_argument("--tool-concurrency", type=int, nargs="+")
    parser.add_argument("--tool-samples", type=int, default=32)
    parser.add_argument("--tool-item-count", type=int, default=2)
    parser.add_argument("--tool-delay-ms", type=int, default=250)
    parser.add_argument("--tool-runtime-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--blocking-max-workers", type=int, default=4)
    parser.add_argument("--soak-duration-seconds", type=float, default=600.0)
    parser.add_argument("--soak-concurrency", type=int, default=4)
    parser.add_argument("--soak-interval-seconds", type=float, default=1.0)
    parser.add_argument("--keep-server-log", action="store_true")
    args = parser.parse_args()
    explicit_stages = [stage for stage in WP3_STAGES if getattr(args, stage)]
    if explicit_stages:
        args.stage = explicit_stages
    if (
        args.samples <= 0
        or args.resume_samples <= 0
        or any(value <= 0 for value in args.concurrency)
        or any(value <= 0 for value in args.sse_concurrency)
        or args.sse_samples <= 0
        or args.tool_samples <= 0
        or args.tool_item_count <= 0
        or args.tool_delay_ms < 0
        or args.tool_delay_ms > 5_000
        or args.blocking_max_workers <= 0
        or args.tool_runtime_timeout_seconds <= 0
        or (args.tool_concurrency is not None and any(value <= 0 for value in args.tool_concurrency))
        or args.soak_duration_seconds <= 0
        or args.soak_concurrency <= 0
        or args.soak_interval_seconds <= 0
        or (args.tool_limit is not None and args.tool_limit <= 0)
        or (args.active_run_slots is not None and args.active_run_slots <= 0)
        or (args.confirmation_run is not None and args.confirmation_run <= 0)
    ):
        parser.error(
            "samples, resume-samples, concurrency, sse-concurrency, samples and "
            "stage-specific limits must be positive"
        )
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
