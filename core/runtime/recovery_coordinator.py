"""Application-scoped stale Run recovery orchestration.

The coordinator only scans, claims, loads and supervises.  Execution remains
owned by the existing Runtime Factory/RunCoordinator.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import uuid4

from core.persistence.repositories.execution import DurableExecutionRepository
from core.runtime.run_control import DurableRunControlService, RunControlConflict, RunLease


@dataclass(frozen=True, slots=True)
class RecoveryCoordinatorConfig:
    cadence_seconds: float = 5.0
    batch_size: int = 20
    max_attempts: int = 5
    initial_backoff_seconds: int = 5
    max_backoff_seconds: int = 60
    shutdown_grace_seconds: float = 5.0

    def __post_init__(self) -> None:
        if self.cadence_seconds <= 0 or self.batch_size <= 0 or self.max_attempts <= 0:
            raise ValueError("recovery cadence, batch size and max attempts must be positive")
        if self.initial_backoff_seconds <= 0 or self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError("invalid recovery backoff bounds")
        if self.shutdown_grace_seconds < 0:
            raise ValueError("shutdown grace must be non-negative")


class RecoveryCoordinator:
    """Narrow application worker; no Scheduler, Tool state machine or terminal owner."""

    def __init__(self, repository: DurableExecutionRepository,
                 run_control: DurableRunControlService,
                 rehydrate_and_execute: Callable[[str, RunLease, object], Awaitable[None]],
                 *, instance_id: str | None = None,
                 config: RecoveryCoordinatorConfig | None = None) -> None:
        if not isinstance(repository, DurableExecutionRepository):
            raise TypeError("repository must be DurableExecutionRepository")
        if not isinstance(run_control, DurableRunControlService):
            raise TypeError("run_control must be DurableRunControlService")
        if not callable(rehydrate_and_execute):
            raise TypeError("rehydrate_and_execute must be callable")
        self.repository = repository
        self.run_control = run_control
        self.rehydrate_and_execute = rehydrate_and_execute
        self.instance_id = instance_id or f"recovery-{uuid4().hex}"
        self.config = config or RecoveryCoordinatorConfig()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._local_tasks: set[asyncio.Task[None]] = set()

    async def scan_once(self) -> int:
        candidates = await self.repository.stale_run_ids(limit=self.config.batch_size)
        started = 0
        for run_id in candidates:
            try:
                lease = await self.run_control.claim(run_id, self.instance_id)
            except RunControlConflict:
                # Claim races are expected and are not recovery failures.
                continue
            except Exception as exc:
                await self.repository.record_recovery_failure(
                    run_id, type(exc).__name__.upper(),
                    max_attempts=self.config.max_attempts,
                    initial_seconds=self.config.initial_backoff_seconds,
                    max_seconds=self.config.max_backoff_seconds,
                )
                continue
            try:
                # Claim is the authority boundary.  Classify abandoned work
                # under the newly acquired fence before Factory hydration;
                # a pre-claim scan image is only a discovery hint.
                image = await self.repository.prepare_recovery(lease)
            except Exception as exc:
                await self.repository.record_recovery_failure(
                    run_id, type(exc).__name__.upper(),
                    max_attempts=self.config.max_attempts,
                    initial_seconds=self.config.initial_backoff_seconds,
                    max_seconds=self.config.max_backoff_seconds,
                )
                continue
            if image is None:
                continue
            task = asyncio.create_task(self._supervise(run_id, lease, image))
            self._local_tasks.add(task)
            task.add_done_callback(self._local_tasks.discard)
            started += 1
        return started

    async def _supervise(self, run_id: str, lease: RunLease, image: object) -> None:
        try:
            await self.rehydrate_and_execute(run_id, lease, image)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.repository.record_recovery_failure(
                run_id, type(exc).__name__.upper(),
                max_attempts=self.config.max_attempts,
                initial_seconds=self.config.initial_backoff_seconds,
                max_seconds=self.config.max_backoff_seconds,
            )

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="runtime-recovery-coordinator")

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A transient database/scan failure must not kill the
                # application-scoped worker.  Candidate-specific failures
                # are recorded by ``scan_once``; there is no run id to which
                # a global scan failure can safely be attributed.
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.config.cadence_seconds)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()
        scan_task = self._task
        self._task = None
        if scan_task is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(scan_task),
                    timeout=self.config.shutdown_grace_seconds,
                )
            except asyncio.TimeoutError:
                scan_task.cancel()
                await asyncio.gather(scan_task, return_exceptions=True)
            except asyncio.CancelledError:
                await asyncio.gather(scan_task, return_exceptions=True)
            except Exception:
                # The scan loop handles candidate failures itself.  A final
                # scan exception must not prevent local recovery tasks from
                # reaching the bounded shutdown path.
                pass
        if self._local_tasks:
            done, pending = await asyncio.wait(self._local_tasks, timeout=self.config.shutdown_grace_seconds)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            self._local_tasks.clear()


__all__ = ["RecoveryCoordinator", "RecoveryCoordinatorConfig"]
