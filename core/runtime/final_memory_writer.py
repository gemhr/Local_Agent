#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Run-level delivered-only final Memory owner (WP4 minimum + WP5 atomicity).

The final consensus requires Memory to receive only the original user message
and the unique, confirmed-DELIVERED final assistant message. INTERNAL,
FAILED and OUTCOME_UNKNOWN finals never reach Memory; specialist/Synthesis
raw results are never written. The completion pipeline calls this writer only
after OutputGate reports DELIVERED.

WP5 hardening:
- ``write_delivered`` commits the user + assistant rows inside one SQLite
  transaction (``append_exchange_atomic``), so both succeed or neither does;
- the writer is write-once per Run: a failed commit is never silently retried
  and a second call in the same Run is rejected (no duplicated user text);
- the commit is observable through the ``runtime.final_memory_commit`` span
  and the ``runtime_final_memory_commit_*`` metrics when injectors are wired.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from uuid import uuid4

from core.memory_manager import (
    MemoryExchangeError,
    MemoryExchangeErrorCode,
)
from core.runtime.step_result_store import StepResultStore
from core.runtime.memory_authorization import (
    MemoryAccessAuthorizer,
    MemoryAccessPrincipal,
    MemoryAuthorizationError,
)
from core.runtime.trace_contract import (
    RUNTIME_FINAL_MEMORY_COMMIT_SPAN,
    set_span_attributes,
)
from core.runtime.tracing import current_trace_context, start_span_safely


class FinalMemoryCommitStatus:
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    NOT_ATTEMPTED = "NOT_ATTEMPTED"


@dataclass(frozen=True, slots=True)
class CommittedExchangeReceipt:
    """成功提交的 canonical conversation exchange 的最小 committed identity。

    只携带必要 committed identity（run / exchange / entry agent / scope），
    不携带 user query、final answer、tool/RAG 输出或任意 RunContext；它不是
    generic context bag。post-delivery Semantic Formation 以它作为唯一
    provenance/eligibility 输入。
    """

    run_id: str | None
    exchange_id: str
    entry_agent_id: str
    memory_scope: str

    def __post_init__(self) -> None:
        for name in ("exchange_id", "entry_agent_id", "memory_scope"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} 必须是非空字符串")
        if self.run_id is not None and (
            not isinstance(self.run_id, str) or not self.run_id.strip()
        ):
            raise ValueError("run_id 必须是非空字符串或 None")


class RunFinalMemoryWriter:
    """One write per Run; write-once guarded; uses the existing direct scope.

    Scope contract (source-derived, not invented):
    - the entry agent's existing ``direct`` conversation scope receives the
      original user message and the delivered final assistant message, which
      preserves the single-Agent direct history persistence behavior;
    - the writer never stores specialist/Synthesis raw results.
    """

    def __init__(
        self,
        router,
        *,
        entry_agent_id: str,
        requester: MemoryAccessPrincipal | None = None,
        user_request: str,
        persist: bool,
        run_id: str | None = None,
        span_recorder=None,
        metrics_recorder=None,
    ) -> None:
        if not isinstance(entry_agent_id, str) or not entry_agent_id.strip():
            raise ValueError("entry_agent_id 不能为空")
        if requester is not None and not isinstance(requester, MemoryAccessPrincipal):
            raise TypeError("requester 必须是 MemoryAccessPrincipal 或 None")
        if not isinstance(user_request, str) or not user_request.strip():
            raise ValueError("user_request 不能为空")
        if type(persist) is not bool:
            raise TypeError("persist 必须是 bool")
        if run_id is not None and (
            not isinstance(run_id, str) or not run_id.strip()
        ):
            raise ValueError("run_id 必须是非空字符串")
        self._router = router
        self._entry_agent_id = entry_agent_id.strip()
        self._requester = requester or MemoryAccessPrincipal(self._entry_agent_id)
        self._authorizer = MemoryAccessAuthorizer()
        self._user_request = user_request
        self._persist = persist
        self._run_id = run_id
        self._span_recorder = span_recorder
        self._metrics_recorder = metrics_recorder
        self._write_lock = threading.Lock()
        self._written = False

    @property
    def written(self) -> bool:
        with self._write_lock:
            return self._written

    def write_delivered(
        self,
        *,
        final_step_id: str,
        store: StepResultStore,
    ) -> CommittedExchangeReceipt | None:
        """Write the original user message and the delivered final once.

        Write-once per Run: after the first call (success or failure) the
        writer refuses further calls, so a failed Memory commit is never
        automatically retried inside the same Run and user text is never
        duplicated.

        WP2: a successful commit returns the immutable committed exchange
        receipt (minimal committed identity only); ``persist=False`` returns
        ``None`` and never produces a receipt. The writer still owns only
        Conversation persistence — it never extracts candidates, calls a
        model, or writes Long-term Memory.
        """
        if not self._persist:
            return None
        if not isinstance(store, StepResultStore):
            raise TypeError("write_delivered 需要 StepResultStore")
        with self._write_lock:
            if self._written:
                raise RuntimeError(
                    "Run 级 final Memory 只能写入一次，拒绝重复写入"
                )
            self._written = True
        content = store.read_final_content(final_step_id)
        memory = getattr(self._router, "memory_manager", None)
        if memory is None:
            self._record_memory_outcome(
                status=FinalMemoryCommitStatus.FAILED,
                error_code="MEMORY_MANAGER_UNAVAILABLE",
                duration_ms=0,
                span=None,
                user_write_status="FAILED",
                assistant_write_status="NOT_ATTEMPTED",
            )
            raise RuntimeError("router 没有可用的 memory_manager")
        scope = getattr(self._router, "DIRECT_MEMORY_SCOPE", "direct")
        authorization = self._authorizer.authorize_private_create(
            self._requester,
            self._entry_agent_id,
            scope,
            requested_memory_scope=scope,
        )
        if not authorization.allowed:
            self._record_memory_outcome(
                status=FinalMemoryCommitStatus.FAILED,
                error_code=authorization.reason,
                duration_ms=0,
                span=None,
                user_write_status="NOT_ATTEMPTED",
                assistant_write_status="NOT_ATTEMPTED",
            )
            raise MemoryAuthorizationError(authorization)
        started = time.monotonic()
        span = None
        if self._span_recorder is not None:
            parent = current_trace_context()
            span = start_span_safely(
                self._span_recorder,
                trace_id=(
                    parent.trace_id
                    if parent is not None
                    else (self._run_id or "unknown")
                ),
                run_id=(
                    parent.run_id
                    if parent is not None
                    else (self._run_id or "unknown")
                ),
                component="final_memory",
                operation=RUNTIME_FINAL_MEMORY_COMMIT_SPAN,
                step_id=final_step_id,
                parent_context=parent,
            )
        user_write_status = "NOT_ATTEMPTED"
        assistant_write_status = "NOT_ATTEMPTED"
        transaction_used = True
        exchange_id = (
            self._run_id
            if self._run_id is not None
            else "exchange-" + str(uuid4().hex)
        )
        try:
            memory.append_exchange_atomic(
                self._entry_agent_id,
                scope,
                self._user_request,
                content,
                run_id=self._run_id,
                exchange_id=exchange_id,
            )
            user_write_status = "WRITTEN"
            assistant_write_status = "WRITTEN"
        except MemoryExchangeError as exc:
            duration_ms = max(
                0, int((time.monotonic() - started) * 1000)
            )
            self._record_memory_outcome(
                status=FinalMemoryCommitStatus.FAILED,
                error_code=exc.error_code,
                duration_ms=duration_ms,
                span=span,
                user_write_status=user_write_status,
                assistant_write_status=assistant_write_status,
                transaction_used=transaction_used,
            )
            raise
        except BaseException:
            duration_ms = max(
                0, int((time.monotonic() - started) * 1000)
            )
            self._record_memory_outcome(
                status=FinalMemoryCommitStatus.FAILED,
                error_code="FINAL_OUTPUT_MEMORY_COMMIT_FAILED",
                duration_ms=duration_ms,
                span=span,
                user_write_status=user_write_status,
                assistant_write_status=assistant_write_status,
                transaction_used=transaction_used,
            )
            raise
        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        if span is not None:
            set_span_attributes(
                span,
                persist_enabled=self._persist,
                entry_agent_id=self._entry_agent_id,
                memory_scope=scope,
                delivery_status="DELIVERED",
                user_write_status=user_write_status,
                assistant_write_status=assistant_write_status,
                transaction_used=transaction_used,
            )
            span.end_ok()
        self._record_memory_outcome(
            status=FinalMemoryCommitStatus.SUCCEEDED,
            error_code="OK",
            duration_ms=duration_ms,
            span=None,
            user_write_status=user_write_status,
            assistant_write_status=assistant_write_status,
            transaction_used=transaction_used,
        )
        return CommittedExchangeReceipt(
            run_id=self._run_id,
            exchange_id=exchange_id,
            entry_agent_id=self._entry_agent_id,
            memory_scope=scope,
        )

    def _record_memory_outcome(
        self,
        *,
        status: str,
        error_code: str,
        duration_ms: int,
        span,
        user_write_status: str,
        assistant_write_status: str,
        transaction_used: bool = True,
    ) -> None:
        if span is not None:
            set_span_attributes(
                span,
                persist_enabled=self._persist,
                entry_agent_id=self._entry_agent_id,
                memory_scope=getattr(
                    self._router, "DIRECT_MEMORY_SCOPE", "direct"
                ),
                delivery_status="DELIVERED",
                user_write_status=user_write_status,
                assistant_write_status=assistant_write_status,
                transaction_used=transaction_used,
            )
            span.end_error(error_code)
        recorder = self._metrics_recorder
        if recorder is None:
            return
        try:
            recorder.increment_counter(
                "runtime_final_memory_commit_total",
                labels={"status": status, "error_code": error_code},
            )
            recorder.observe_histogram(
                "runtime_final_memory_commit_duration_seconds",
                max(0.0, duration_ms / 1000.0),
                labels={"status": status},
            )
        except Exception:
            return

    def __repr__(self) -> str:
        return (
            "RunFinalMemoryWriter("
            f"entry_agent_id={self._entry_agent_id!r}, "
            f"persist={self._persist!r}, written={self.written!r})"
        )


__all__ = [
    "CommittedExchangeReceipt",
    "FinalMemoryCommitStatus",
    "MemoryExchangeErrorCode",
    "RunFinalMemoryWriter",
]
