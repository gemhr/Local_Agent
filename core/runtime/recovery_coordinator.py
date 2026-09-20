"""Application-scoped stale Run recovery orchestration.

The coordinator only scans, claims, loads and supervises.  Execution remains
owned by the existing Runtime Factory/RunCoordinator.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from time import monotonic
from dataclasses import dataclass
from uuid import uuid4

from core.persistence.repositories.execution import DurableExecutionRepository
from core.runtime.run_control import (
    DurableRunControlService,
    OwnershipLost,
    RunControlConflict,
    RunLease,
)
from core.runtime.structured_logging import emit_reconciliation_log


@dataclass(frozen=True, slots=True)
class RecoveryCoordinatorConfig:
    cadence_seconds: float = 5.0
    batch_size: int = 20
    max_attempts: int = 5
    initial_backoff_seconds: int = 5
    max_backoff_seconds: int = 60
    shutdown_grace_seconds: float = 5.0
    reconciliation_max_concurrency: int = 4

    def __post_init__(self) -> None:
        if self.cadence_seconds <= 0 or self.batch_size <= 0 or self.max_attempts <= 0 or self.reconciliation_max_concurrency <= 0:
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
                 config: RecoveryCoordinatorConfig | None = None,
                 metrics_recorder: object | None = None,
                 structured_logger: object | None = None) -> None:
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
        self.metrics_recorder = metrics_recorder
        self.structured_logger = structured_logger
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._local_tasks: set[asyncio.Task[None]] = set()
        self._reconciliation_handler: Callable[[object], Awaitable[None]] | None = None
        self._reconciliation_tasks: set[asyncio.Task[None]] = set()

    def configure_reconciliation(self, handler: Callable[[object], Awaitable[None]]) -> None:
        """接入 WP2 对账 lane；handler 必须负责 provider lookup 和 typed convergence。"""
        if not callable(handler):
            raise TypeError("reconciliation handler must be callable")
        self._reconciliation_handler = handler

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

    async def reconciliation_scan_once(self) -> int:
        """有界发现与调度；不会在 coordinator 内直接修改 Tool 状态。"""
        handler = self._reconciliation_handler
        if handler is None:
            return 0
        candidates = await self.repository.reconciliation_candidates(limit=self.config.batch_size)
        self._metric(
            "runtime_reconciliation_candidates_discovered_total", len(candidates)
        )
        slots = max(0, self.config.reconciliation_max_concurrency - len(self._reconciliation_tasks))
        scheduled = 0
        for candidate in candidates[:slots]:
            task = asyncio.create_task(self._supervise_reconciliation(candidate), name="runtime-tool-reconciliation")
            self._reconciliation_tasks.add(task)
            task.add_done_callback(self._reconciliation_tasks.discard)
            scheduled += 1
        return scheduled

    async def _supervise_reconciliation(self, candidate: object) -> None:
        handler = self._reconciliation_handler
        if handler is None:
            return
        started = monotonic()
        try:
            result = await handler(candidate)
            # The production handler claims the Run before returning.  Count
            # this as claimed only after that authoritative boundary succeeds;
            # scheduling a task is not a claim.
            self._metric("runtime_reconciliation_claimed_total")
            self._record_result_metrics(result)
            self._emit_reconciliation_log(candidate, result=result, started=started)
        except asyncio.CancelledError:
            raise
        except (RunControlConflict, OwnershipLost) as exc:
            self._metric("runtime_reconciliation_claim_lost_total")
            self._emit_reconciliation_log(
                candidate, outcome="CLAIM_LOST", error_code=type(exc).__name__.upper(), started=started
            )
        except Exception as exc:
            # Candidate-specific retry/backoff is owned by the Tool service;
            # the lane remains alive and does not fabricate an outcome.
            self._metric("runtime_reconciliation_failed_total")
            self._emit_reconciliation_log(
                candidate, outcome="FAILED", error_code=type(exc).__name__.upper(), started=started
            )
        finally:
            self._metric(
                "runtime_reconciliation_latency_seconds",
                monotonic() - started,
                histogram=True,
            )

    def _record_result_metrics(self, result: object) -> None:
        state = getattr(result, "state", None)
        state_value = getattr(state, "value", state)
        if state_value == "COMMITTED":
            self._metric("runtime_reconciliation_converged_committed_total")
        elif state_value == "NOT_COMMITTED":
            self._metric("runtime_reconciliation_converged_not_committed_total")
        elif state_value == "UNKNOWN":
            self._metric("runtime_reconciliation_still_unknown_total")
        else:
            self._metric("runtime_reconciliation_failed_total")
        safe_error = getattr(result, "last_safe_error_code", None)
        if safe_error == "PROVIDER_RECONCILIATION_UNSUPPORTED":
            self._metric("runtime_reconciliation_provider_unsupported_total")
        if bool(getattr(result, "manual_required", False)):
            self._metric("runtime_reconciliation_manual_required_total")

    def _emit_reconciliation_log(
        self,
        candidate: object,
        *,
        result: object | None = None,
        outcome: str | None = None,
        error_code: str | None = None,
        started: float,
    ) -> None:
        state = getattr(result, "state", None)
        state_value = getattr(state, "value", state)
        resolved_outcome = outcome or (str(state_value) if state_value is not None else "FAILED")
        result_error = getattr(result, "last_safe_error_code", None)
        emit_reconciliation_log(
            self.structured_logger,
            run_id=getattr(candidate, "run_id", ""),
            step_id=getattr(candidate, "step_id", None),
            invocation_id=getattr(candidate, "invocation_id", None),
            tool_name=getattr(candidate, "tool_name", None),
            provider_identity=None,
            attempt=getattr(result, "reconcile_attempt_count", None),
            outcome=resolved_outcome,
            safe_error_code=error_code or result_error,
            duration_ms=max(0, int((monotonic() - started) * 1000)),
        )

    def _metric(self, name: str, value: float = 1.0, *, histogram: bool = False) -> None:
        recorder = self.metrics_recorder
        if recorder is None:
            return
        method = getattr(recorder, "observe_histogram" if histogram else "increment_counter", None)
        if callable(method):
            try:
                method(name, value)
            except Exception:
                pass

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
                await self.reconciliation_scan_once()
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
        if self._reconciliation_tasks:
            done, pending = await asyncio.wait(self._reconciliation_tasks, timeout=self.config.shutdown_grace_seconds)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            self._reconciliation_tasks.clear()


__all__ = ["RecoveryCoordinator", "RecoveryCoordinatorConfig"]
