"""Worker process used by the Stage10 real-process crash E2E.

This module intentionally keeps all recovery facts in PostgreSQL.  The small
files written by the worker are only parent/child synchronization markers and
an invocation audit trail; neither worker reads the other's Python memory.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import sys
import time
import traceback

# Executing this file as a child process makes ``tests/`` the first import
# root.  Add the repository root explicitly so the worker exercises the same
# production modules as the parent pytest process.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from core.persistence.database import Database, DatabaseConfig
from core.persistence.repositories.execution import DurableExecutionRepository
from core.runtime.application_services import ApplicationRuntimeServices
from core.runtime.client_event_feed import PostgresClientEventFeed
from core.runtime.execution_aggregate import ExecutionRootInput
from core.runtime.event_journal_store import PostgresRunEventJournal
from core.runtime.planning import (
    ExecutionKind,
    OutputPolicy,
    Plan,
    PlanSource,
    PlanStep,
    TaskCapabilityRequirements,
)
from core.runtime.recovery_coordinator import RecoveryCoordinator, RecoveryCoordinatorConfig
from core.runtime.run_control import DurableRunControlService
from core.runtime.runtime_factory import CoordinatedRuntimeFactory

from tests._runtime_assembly_fixtures import make_services


def build_crash_plan() -> Plan:
    """Create a deterministic two-step plan with one internal checkpoint."""
    requirements = TaskCapabilityRequirements()
    plan = Plan(
        plan_id="stage10-real-crash-plan",
        version=1,
        task_summary="deterministic two-step crash recovery",
        steps=(
            PlanStep(
                step_id="checkpoint",
                # Keep title equal to step_id because the current rehydration
                # contract reconstructs the local StepState name from its ID.
                title="checkpoint",
                description="Produce the durable checkpoint.",
                depends_on=(),
                completion_criteria="checkpoint is complete",
                preferred_agent="data_analyst",
                capability_requirements=requirements,
                execution_kind=ExecutionKind.AGENT,
                output_policy=OutputPolicy.INTERNAL,
            ),
            PlanStep(
                step_id="finish",
                title="finish",
                description="Produce the final deterministic result.",
                depends_on=("checkpoint",),
                completion_criteria="final result is complete",
                capability_requirements=requirements,
                output_policy=OutputPolicy.FINAL_SYNTHESIS,
                execution_kind=ExecutionKind.SYNTHESIS,
                preferred_agent="synthesis_agent",
            ),
        ),
        created_at=datetime.now(UTC),
        source=PlanSource.DETERMINISTIC,
    )
    return plan


class DeterministicCrashRouter:
    """A process-local deterministic provider with no shared state dependency."""

    def __init__(self, role: str, run_id: str, marker_dir: Path) -> None:
        self.role = role
        self.run_id = run_id
        self.marker_dir = marker_dir
        self.invocation_log = marker_dir / "invocations.jsonl"

    def complete_single_agent(self, agent_id: str, query: str, **kwargs) -> str:
        run_context = kwargs.get("run_context")
        step_id = getattr(getattr(run_context, "active_step", None), "step_id", None)
        if step_id is None:
            # The adapter supplies the step only through the event/request path;
            # use the stable instruction marker as a fallback for old APIs.
            instruction = str(query)
            step_id = "finish" if "final deterministic" in instruction else "checkpoint"
        record = {
            "pid": os.getpid(),
            "role": self.role,
            "run_id": self.run_id,
            "step_id": step_id,
        }
        with self.invocation_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
        if self.role == "A" and step_id == "finish":
            (self.marker_dir / "finish.started").write_text(
                str(os.getpid()), encoding="utf-8"
            )
            # Parent terminates this process.  No in-memory state is needed by
            # Worker B; its provider follows the same deterministic contract.
            while True:
                time.sleep(60)
        return f"deterministic-{step_id}"

    def complete_context_items(self, agent_id: str, context_items, **kwargs) -> str:
        record = {
            "pid": os.getpid(),
            "role": self.role,
            "run_id": self.run_id,
            "step_id": "finish",
        }
        with self.invocation_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
        if self.role == "A":
            (self.marker_dir / "finish.started").write_text(
                str(os.getpid()), encoding="utf-8"
            )
            while True:
                time.sleep(60)
        return "deterministic-finish"


def _services(database: Database, control: DurableRunControlService, feed: PostgresClientEventFeed, journal: PostgresRunEventJournal, owner: str) -> ApplicationRuntimeServices:
    base = make_services(snapshot_enabled=False)
    return replace(
        base,
        event_journal=journal,
        durable_run_control=control,
        run_control_owner_id=owner,
        client_event_feed=feed,
    )


async def _run_worker_a(args: argparse.Namespace) -> None:
    marker_dir = Path(args.marker_dir)
    marker_dir.mkdir(parents=True, exist_ok=True)
    database = Database(DatabaseConfig(url=args.dsn, use_null_pool=True))
    control = DurableRunControlService(database, lease_seconds=args.lease_seconds)
    journal = PostgresRunEventJournal(database)
    feed = PostgresClientEventFeed(database)
    repository = DurableExecutionRepository(database, control)
    run_id = args.run_id
    owner = f"worker-a-{os.getpid()}"
    lease = await control.claim(run_id, owner)
    plan = build_crash_plan()
    root = ExecutionRootInput(
        run_id=run_id,
        resume_input={
            "entry_agent_id": "core_router",
            "session_id": "stage10-real-crash",
            "trace_id": run_id,
            "user_query": "stage10 real process crash",
        },
        plan=plan,
        absolute_deadline=datetime.now(UTC) + timedelta(seconds=120),
        budget_totals={"total_tokens": 1000},
        budget_reserved={"total_tokens": 0},
        budget_consumed={"total_tokens": 0},
    )
    await repository.initialize(root, lease=lease)
    (marker_dir / "lease-a.json").write_text(
        json.dumps({
            "run_id": lease.run_id,
            "owner_id": lease.owner_id,
            "lease_until": lease.lease_until.isoformat(),
            "fencing_token": lease.fencing_token,
            "version": lease.version,
        }),
        encoding="utf-8",
    )
    image = await repository.load(run_id)
    if image is None:
        raise RuntimeError("worker A could not load its initialized execution image")
    router = DeterministicCrashRouter("A", run_id, marker_dir)
    services = _services(database, control, feed, journal, owner)
    factory = CoordinatedRuntimeFactory(
        router,
        services,
        max_concurrency=1,
        execution_repository=repository,
    )
    scope = await factory.create_rehydrated_run_scope(image, lease=lease, run_id=run_id)
    try:
        await scope.execute()
    finally:
        await scope.close(abort=True)
        await database.dispose()


async def _run_worker_b(args: argparse.Namespace) -> None:
    marker_dir = Path(args.marker_dir)
    database = Database(DatabaseConfig(url=args.dsn, use_null_pool=True))
    control = DurableRunControlService(database, lease_seconds=args.lease_seconds)
    journal = PostgresRunEventJournal(database)
    feed = PostgresClientEventFeed(database)
    repository = DurableExecutionRepository(database, control)
    owner = f"worker-b-{os.getpid()}"
    router = DeterministicCrashRouter("B", args.run_id, marker_dir)
    services = _services(database, control, feed, journal, owner)
    factory = CoordinatedRuntimeFactory(
        router,
        services,
        max_concurrency=1,
        execution_repository=repository,
    )

    async def recover(run_id: str, lease, image) -> None:
        # RecoveryCoordinator already classified the image under the new
        # fence; the callback mirrors the production composition exactly.
        try:
            scope = await factory.create_rehydrated_run_scope(
                image, lease=lease, run_id=run_id
            )
            try:
                await scope.execute()
            finally:
                await scope.close(abort=True)
        except Exception:
            (marker_dir / "worker-b.error.txt").write_text(
                traceback.format_exc(), encoding="utf-8"
            )
            raise
        (marker_dir / "worker-b.done").write_text(str(os.getpid()), encoding="utf-8")

    coordinator = RecoveryCoordinator(
        repository,
        control,
        recover,
        instance_id=owner,
        config=RecoveryCoordinatorConfig(
            cadence_seconds=0.10,
            batch_size=4,
            max_attempts=30,
            initial_backoff_seconds=0.10,
            max_backoff_seconds=1,
            shutdown_grace_seconds=1,
        ),
    )
    await coordinator.start()
    try:
        while not (marker_dir / "worker-b.done").exists():
            await asyncio.sleep(0.05)
    finally:
        await coordinator.stop()
        await database.dispose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=("A", "B"), required=True)
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--marker-dir", required=True)
    parser.add_argument("--lease-seconds", type=int, default=1)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.role == "A":
        asyncio.run(_run_worker_a(args))
    else:
        asyncio.run(_run_worker_b(args))


if __name__ == "__main__":
    main()
