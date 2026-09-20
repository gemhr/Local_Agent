"""Stage10-WP1 mandatory real process crash recovery evidence."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from core.persistence.repositories.execution import DurableExecutionRepository, RecoveryImage
from core.runtime.client_event_feed import PostgresClientEventFeed
from core.runtime.execution_aggregate import ExecutionRootInput
from core.runtime.event_journal_store import PostgresRunEventJournal
from core.runtime.planning import TaskCapabilityRequirements, create_single_step_plan
from core.runtime.run_control import DurableRunControlService, RunLease, OwnershipLost
from core.runtime.runtime_factory import CoordinatedRuntimeFactory
from tests._runtime_assembly_fixtures import make_services


pytestmark = pytest.mark.asyncio


async def _wait_for_path(path: Path, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path.name}")


def _worker_command(role: str, dsn: str, run_id: str, marker_dir: Path) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).with_name("_stage10_crash_worker.py")),
        "--role",
        role,
        "--dsn",
        dsn,
        "--run-id",
        run_id,
        "--marker-dir",
        str(marker_dir),
        "--lease-seconds",
        "1",
    ]


@pytest.mark.REAL_PROCESS_CRASH_E2E
async def test_REAL_PROCESS_CRASH_E2E(clean_database, pg_url: str, tmp_path: Path) -> None:
    """A real A/B process takeover proves durable recovery, dedup and fence."""
    print("REAL_PROCESS_CRASH_E2E")
    run_id = f"stage10-real-crash-{time.time_ns()}"
    marker_dir = tmp_path / "stage10-crash"
    marker_dir.mkdir()
    env = dict(__import__("os").environ)
    env["LOCAL_AGENT_DATABASE_URL"] = pg_url
    env["LOCAL_AGENT_TEST_DATABASE_URL"] = pg_url
    env["PYTHONUNBUFFERED"] = "1"

    worker_a = subprocess.Popen(
        _worker_command("A", pg_url, run_id, marker_dir),
        cwd=str(Path(__file__).parents[1]),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    worker_b: subprocess.Popen[str] | None = None
    try:
        await _wait_for_path(marker_dir / "finish.started", timeout=20)
        await _wait_for_path(marker_dir / "lease-a.json", timeout=5)
        # This is an OS-level process termination, not task cancellation or a
        # fake same-process worker.
        worker_a.kill()
        await asyncio.to_thread(worker_a.wait, 10)
        assert worker_a.returncode != 0

        worker_b = subprocess.Popen(
            _worker_command("B", pg_url, run_id, marker_dir),
            cwd=str(Path(__file__).parents[1]),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            await _wait_for_path(marker_dir / "worker-b.done", timeout=30)
        except AssertionError as exc:
            if worker_b.poll() is None:
                worker_b.kill()
                await asyncio.to_thread(worker_b.wait, 10)
            stdout = worker_b.stdout.read() if worker_b.stdout is not None else ""
            stderr = worker_b.stderr.read() if worker_b.stderr is not None else ""
            recovery_error_path = marker_dir / "worker-b.error.txt"
            recovery_error = (
                recovery_error_path.read_text(encoding="utf-8")
                if recovery_error_path.exists()
                else ""
            )
            raise AssertionError(
                f"{exc}; worker B returncode={worker_b.returncode}; "
                f"stdout={stdout!r}; stderr={stderr!r}; "
                f"recovery_error={recovery_error!r}"
            ) from exc
        await asyncio.to_thread(worker_b.wait, 10)
        assert worker_b.returncode == 0, (
            worker_b.stderr.read() if worker_b.stderr is not None else ""
        )

        control = DurableRunControlService(clean_database, lease_seconds=1)
        repository = DurableExecutionRepository(clean_database, control)
        image: RecoveryImage | None = await repository.load(run_id)
        assert image is not None
        assert {row.step_id: row.status for row in image.steps} == {
            "checkpoint": "SUCCEEDED",
            "finish": "SUCCEEDED",
        }

        invocations = [
            json.loads(line)
            for line in (marker_dir / "invocations.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert sum(item["step_id"] == "checkpoint" for item in invocations) == 1
        assert sum(item["step_id"] == "finish" for item in invocations) == 2

        feed = PostgresClientEventFeed(clean_database)
        delivered = await feed.read_after(run_id, 0, 100)
        terminal = [event for event in delivered if event.event_type in {
            "run.completed", "run.failed", "run.cancelled"
        }]
        assert len(terminal) == 1

        lease_data = json.loads((marker_dir / "lease-a.json").read_text(encoding="utf-8"))
        old_lease = RunLease(
            run_id=lease_data["run_id"],
            owner_id=lease_data["owner_id"],
            lease_until=datetime.fromisoformat(lease_data["lease_until"]),
            fencing_token=int(lease_data["fencing_token"]),
            version=int(lease_data["version"]),
        )
        with pytest.raises(OwnershipLost):
            await repository.start_step(
                old_lease,
                step_id="finish",
                plan_version=1,
            )
    finally:
        if worker_a.poll() is None:
            worker_a.kill()
            await asyncio.to_thread(worker_a.wait, 10)
        if worker_b is not None and worker_b.poll() is None:
            worker_b.kill()
            await asyncio.to_thread(worker_b.wait, 10)


@pytest.mark.parametrize(
    ("terminal_cause", "expected_status"),
    [("cancel", "CANCELLED"), ("deadline", "FAILED")],
)
async def test_recovery_cancel_and_deadline_precede_new_provider_work(
    clean_database, terminal_cause: str, expected_status: str
) -> None:
    run_id = f"stage10-{terminal_cause}-{time.time_ns()}"
    control = DurableRunControlService(clean_database, lease_seconds=30)
    repository = DurableExecutionRepository(clean_database, control)
    journal = PostgresRunEventJournal(clean_database)
    feed = PostgresClientEventFeed(clean_database)
    first = await control.claim(run_id, "worker-a")
    deadline = (
        datetime.now(UTC) - timedelta(seconds=1)
        if terminal_cause == "deadline"
        else datetime.now(UTC) + timedelta(minutes=5)
    )
    await repository.initialize(
        ExecutionRootInput(
            run_id=run_id,
            resume_input={
                "entry_agent_id": "core_router",
                "session_id": "stage10-precedence",
                "trace_id": run_id,
                "user_query": "must not invoke provider",
            },
            plan=create_single_step_plan(
                "core_router", TaskCapabilityRequirements()
            ),
            absolute_deadline=deadline,
            budget_totals={},
            budget_reserved={},
            budget_consumed={},
        ),
        lease=first,
    )
    if terminal_cause == "cancel":
        await control.request_cancel(run_id, "REQUEST_CANCELLED")
    await control.release(first)
    second = await control.claim(run_id, "worker-b")
    image = await repository.prepare_recovery(second)

    class NoCallRouter:
        def __init__(self) -> None:
            self.calls = 0

        def complete_single_agent(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("recovery precedence invoked provider")

    router = NoCallRouter()
    services = replace(
        make_services(snapshot_enabled=False),
        event_journal=journal,
        durable_run_control=control,
        run_control_owner_id="worker-b",
        client_event_feed=feed,
    )
    scope = await CoordinatedRuntimeFactory(
        router, services, execution_repository=repository
    ).create_rehydrated_run_scope(image, lease=second, run_id=run_id)
    try:
        result = await scope.execute()
    finally:
        await scope.close(abort=True)

    assert result.status.value == expected_status, result
    assert router.calls == 0
    persisted = await repository.load(run_id)
    assert persisted is not None
    assert persisted.root.status == expected_status
