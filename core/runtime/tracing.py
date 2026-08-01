"""Failure-isolated, in-process tracing primitives for the coordinated runtime."""
from __future__ import annotations

import math
import re
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from types import MappingProxyType
from typing import Iterator, Mapping, Protocol
from uuid import uuid4

from core.runtime.cancellation import CancellationToken, RunCancelledError
from core.runtime.fault_injection import FaultInjectionController
from core.runtime.fault_injection_contract import (
    FaultAction,
    FaultMatchContext,
    FaultPoint,
    InjectedFaultError,
)

_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
SAFE_SPAN_ATTRIBUTES = frozenset({
    "component", "operation", "status", "error_code", "retry_index",
    "candidate_index", "model_profile", "retrieval_stage", "budget_dimension",
    "timeout_reason", "cancellation_reason", "side_effect_kind",
    "side_effect_state", "retry_disposition", "provider_started",
    "execution_detached", "worker_terminated", "resource_release_pending",
    "degraded", "input_count", "output_count", "citation_count", "tool_name",
})
DENIED_SPAN_ATTRIBUTES = frozenset({
    "prompt", "messages", "user_input", "model_output", "tool_arguments",
    "tool_output", "query", "rewritten_query", "embedding", "rag_chunk",
    "memory", "secret", "api_key", "provider_url", "canonical_path",
    "exception_message", "safe_message", "traceback", "idempotency_key",
    "resource_key",
})

def _identifier(value: str | None, name: str, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{name} must be 1-128 safe identifier characters")

@dataclass(frozen=True, slots=True)
class TraceContext:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    run_id: str
    step_id: str | None = None
    def __post_init__(self) -> None:
        for value, name, optional in ((self.trace_id,"trace_id",False),(self.span_id,"span_id",False),(self.parent_span_id,"parent_span_id",True),(self.run_id,"run_id",False),(self.step_id,"step_id",True)):
            _identifier(value, name, optional=optional)
        if self.parent_span_id == self.span_id:
            raise ValueError("span cannot parent itself")

class SpanStatus(str, Enum):
    UNSET="UNSET"; OK="OK"; ERROR="ERROR"; CANCELLED="CANCELLED"; TIMED_OUT="TIMED_OUT"

@dataclass(frozen=True, slots=True)
class SpanRecord:
    trace_id: str; span_id: str; parent_span_id: str | None; run_id: str
    step_id: str | None; component: str; operation: str; started_at: datetime
    completed_at: datetime | None = None; duration_ms: float | None = None
    status: SpanStatus = SpanStatus.UNSET; error_code: str | None = None
    attributes: Mapping[str, object] = field(default_factory=dict, repr=False)
    def __post_init__(self) -> None:
        TraceContext(self.trace_id,self.span_id,self.parent_span_id,self.run_id,self.step_id)
        _identifier(self.component,"component"); _identifier(self.operation,"operation")
        for value in (self.started_at, self.completed_at):
            if value is not None and (value.tzinfo is None or value.utcoffset().total_seconds()!=0):
                raise ValueError("span times must be UTC")
        if self.duration_ms is not None and (not math.isfinite(self.duration_ms) or self.duration_ms < 0):
            raise ValueError("duration_ms must be finite and non-negative")
        object.__setattr__(self,"attributes",MappingProxyType(dict(self.attributes)))

CURRENT_TRACE_CONTEXT: ContextVar[TraceContext | None] = ContextVar("runtime_trace_context", default=None)
def current_trace_context() -> TraceContext | None: return CURRENT_TRACE_CONTEXT.get()
def install_trace_context(context: TraceContext | None) -> Token | None:
    # A failed/no-op span start must not erase the nearest real parent.
    return None if context is None else CURRENT_TRACE_CONTEXT.set(context)
def reset_trace_context(token: Token | None) -> None:
    if token is not None: CURRENT_TRACE_CONTEXT.reset(token)

CURRENT_SPAN_RECORDER: ContextVar["SpanRecorder | None"] = ContextVar(
    "runtime_span_recorder", default=None
)
def current_span_recorder() -> "SpanRecorder | None": return CURRENT_SPAN_RECORDER.get()
def install_span_recorder(recorder: "SpanRecorder | None") -> Token:
    return CURRENT_SPAN_RECORDER.set(recorder)
def reset_span_recorder(token: Token) -> None: CURRENT_SPAN_RECORDER.reset(token)

class SpanSink(Protocol):
    def record(self, record: SpanRecord) -> None: ...


class TraceOperationError(RuntimeError):
    """Fixed-code trace failure which never carries span content."""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)

    def __repr__(self) -> str:
        return f"TraceOperationError(error_code={self.error_code!r})"

class SpanHandle:
    def __init__(self, context: TraceContext | None, record: SpanRecord | None, sink: SpanSink | None, started: float) -> None:
        self.context=context; self._record=record; self._sink=sink; self._started=started
        self._lock=threading.Lock(); self._ended=False; self._attributes: dict[str,object]={}
    @property
    def ended(self) -> bool: return self._ended
    def set_safe_attribute(self, key: str, value: object) -> None:
        if key not in SAFE_SPAN_ATTRIBUTES:
            raise ValueError(f"unsafe span attribute: {key}")
        if not isinstance(value,(str,bool,int,float)) or isinstance(value,float) and not math.isfinite(value):
            raise ValueError("span attribute must be a finite scalar")
        with self._lock:
            if self._ended: raise RuntimeError("span already ended")
            self._attributes[key]=value
    def _end(self,status:SpanStatus,error_code:str|None=None) -> SpanRecord | None:
        with self._lock:
            if self._ended: return self._record
            if error_code is not None: _identifier(error_code,"error_code")
            if self._record is None:
                self._ended=True
                return None
            completed=datetime.now(UTC); duration=max(0.0,(time.monotonic()-self._started)*1000)
            self._record=replace(self._record,completed_at=completed,duration_ms=duration,status=status,error_code=error_code,attributes=self._attributes)
            self._ended=True
        try:
            if self._sink is not None: self._sink.record(self._record)
        except Exception:
            reporter = getattr(self._sink, "record_drop", None)
            if callable(reporter):
                reporter(self._record.component, "recorder_end_failed")
        return self._record
    def _end_recording_failed(self,status:SpanStatus,error_code:str|None=None) -> SpanRecord | None:
        """Logically close an active span when its end cannot be recorded."""
        with self._lock:
            if self._ended: return self._record
            if error_code is not None: _identifier(error_code,"error_code")
            if self._record is None:
                self._ended=True
                return None
            completed=datetime.now(UTC); duration=max(0.0,(time.monotonic()-self._started)*1000)
            self._record=replace(self._record,completed_at=completed,duration_ms=duration,status=status,error_code=error_code,attributes=self._attributes)
            self._ended=True
        reporter = getattr(self._sink, "record_end_failure", None)
        if callable(reporter):
            try: reporter(self._record)
            except Exception: pass
        return self._record
    def end_ok(self): return self._end(SpanStatus.OK)
    def end_error(self,error_code="UNHANDLED_ERROR"): return self._end(SpanStatus.ERROR,error_code)
    def end_cancelled(self,error_code="CANCELLED"): return self._end(SpanStatus.CANCELLED,error_code)
    def end_timed_out(self,error_code="TIMED_OUT"): return self._end(SpanStatus.TIMED_OUT,error_code)

class SpanRecorder(Protocol):
    def start_span(self, *, trace_id:str, run_id:str, component:str, operation:str, step_id:str|None=None, parent_context:TraceContext|None=None) -> SpanHandle: ...
    def snapshot(self) -> tuple[SpanRecord,...]: ...
    def flush(self, timeout_seconds:float|None=None) -> None: ...
    def close(self, timeout_seconds:float|None=None) -> None: ...

@dataclass(frozen=True, slots=True)
class SpanRecorderHealth:
    active_span_count: int
    completed_span_count: int
    dropped_span_count: int
    start_failures: int = 0
    end_failures: int = 0
    flush_failures: int = 0
    status: str = "HEALTHY"
    last_safe_error_code: str | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.active_span_count, "active_span_count"),
            (self.completed_span_count, "completed_span_count"),
            (self.dropped_span_count, "dropped_span_count"),
            (self.start_failures, "start_failures"),
            (self.end_failures, "end_failures"),
            (self.flush_failures, "flush_failures"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.status not in {"HEALTHY", "DEGRADED"}:
            raise ValueError("status must be HEALTHY or DEGRADED")
        if self.last_safe_error_code is not None:
            _identifier(self.last_safe_error_code, "last_safe_error_code")

class InMemorySpanRecorder:
    _DROP_REASONS = frozenset({
        "recorder_start_failed", "recorder_end_failed", "adapter_failed",
        "flush_failed", "queue_full",
    })
    def __init__(self, *, metrics_recorder=None) -> None:
        self._records=[]; self._lock=threading.Lock(); self._closed=False
        self._active: dict[str, SpanHandle] = {}
        self.dropped_spans=0
        self._start_failures=0; self._end_failures=0; self._flush_failures=0
        self._last_safe_error_code: str | None = None
        self._metrics_recorder=metrics_recorder
    def start_span(self, *, trace_id, run_id, component, operation, step_id=None, parent_context=None):
        parent=parent_context if parent_context is not None else current_trace_context()
        if parent is not None and (parent.trace_id != trace_id or parent.run_id != run_id): raise ValueError("parent trace/run mismatch")
        context=TraceContext(trace_id,uuid4().hex,parent.span_id if parent else None,run_id,step_id)
        record=SpanRecord(trace_id,context.span_id,context.parent_span_id,run_id,step_id,component,operation,datetime.now(UTC))
        with self._lock:
            if self._closed:
                self._record_drop_locked(component, "recorder_start_failed")
                return SpanHandle(None,None,None,time.monotonic())
            handle=SpanHandle(context,record,self,time.monotonic())
            self._active[context.span_id]=handle
            return handle
    def record(self,record):
        with self._lock:
            self._active.pop(record.span_id, None)
            if self._closed: self._record_drop_locked(record.component, "recorder_end_failed")
            else: self._records.append(record)
    def record_end_failure(self, record):
        with self._lock:
            self._active.pop(record.span_id, None)
            self._record_drop_locked(record.component, "recorder_end_failed")
            self._end_failures += 1
            self._last_safe_error_code = "TRACE_SPAN_END_FAILED"
    def _record_drop_locked(self, component, reason):
        self.dropped_spans+=1
        metrics=self._metrics_recorder
        if metrics is not None:
            try:
                metrics.increment_counter(
                    "runtime_trace_dropped_spans_total",
                    labels={"component": component, "reason": reason},
                )
            except Exception:
                pass
    def record_drop(self, component, reason):
        if reason not in self._DROP_REASONS:
            raise ValueError("unsupported dropped span reason")
        _identifier(component, "component")
        with self._lock: self._record_drop_locked(component, reason)
    def record_fault(self, kind, safe_error_code):
        if kind not in {"start", "flush"}:
            raise ValueError("unsupported trace fault kind")
        _identifier(safe_error_code, "safe_error_code")
        with self._lock:
            if kind == "start": self._start_failures += 1
            else: self._flush_failures += 1
            self._last_safe_error_code = safe_error_code
    def snapshot(self):
        with self._lock: return tuple(self._records)
    def health_snapshot(self):
        with self._lock:
            return SpanRecorderHealth(
                active_span_count=len(self._active),
                completed_span_count=len(self._records),
                dropped_span_count=self.dropped_spans,
                start_failures=self._start_failures,
                end_failures=self._end_failures,
                flush_failures=self._flush_failures,
                status=(
                    "DEGRADED"
                    if self.dropped_spans
                    or self._start_failures
                    or self._end_failures
                    or self._flush_failures
                    else "HEALTHY"
                ),
                last_safe_error_code=self._last_safe_error_code,
            )
    def flush(self,timeout_seconds=None): return None
    def close(self,timeout_seconds=None):
        with self._lock:
            self._closed=True
            handles=tuple(self._active.values())
        for handle in handles:
            handle.end_cancelled("RECORDER_CLOSED")


class _FaultInjectingSpanHandle:
    """Run-owned end seam around one real shared-recorder handle."""

    def __init__(self, handle, controller, cancellation_token, component):
        self._handle = handle
        self._controller = controller
        self._cancellation_token = cancellation_token
        self._component = component

    @property
    def context(self): return self._handle.context
    @property
    def ended(self): return self._handle.ended
    def set_safe_attribute(self, key, value):
        return self._handle.set_safe_attribute(key, value)
    def _end(self, status, error_code=None):
        if self._handle.ended:
            return self._handle._record
        try:
            _execute_trace_fault(
                self._controller,
                FaultPoint.TRACE_BEFORE_SPAN_END,
                component=self._component,
                operation_kind="SPAN_END",
                cancellation_token=self._cancellation_token,
            )
        except (InjectedFaultError, RunCancelledError):
            return self._handle._end_recording_failed(status, error_code)
        return self._handle._end(status, error_code)
    def end_ok(self): return self._end(SpanStatus.OK)
    def end_error(self,error_code="UNHANDLED_ERROR"): return self._end(SpanStatus.ERROR,error_code)
    def end_cancelled(self,error_code="CANCELLED"): return self._end(SpanStatus.CANCELLED,error_code)
    def end_timed_out(self,error_code="TIMED_OUT"): return self._end(SpanStatus.TIMED_OUT,error_code)


class OperationScopedSpanRecorder:
    """Run-owned facade; the application recorder never caches the controller."""

    def __init__(
        self,
        recorder: SpanRecorder,
        *,
        fault_controller: FaultInjectionController,
        cancellation_token: CancellationToken | None = None,
    ) -> None:
        if not isinstance(fault_controller, FaultInjectionController):
            raise TypeError("fault_controller must be FaultInjectionController")
        self._recorder = recorder
        self._fault_controller = fault_controller
        self._cancellation_token = cancellation_token

    def start_span(self, **kwargs):
        component = kwargs.get("component", "runtime")
        try:
            _execute_trace_fault(
                self._fault_controller,
                FaultPoint.TRACE_BEFORE_SPAN_START,
                component=component,
                operation_kind="SPAN_START",
                cancellation_token=self._cancellation_token,
            )
        except RunCancelledError:
            self._record_start_failure(component, "TRACE_SPAN_START_CANCELLED")
            return SpanHandle(None,None,None,time.monotonic())
        except InjectedFaultError:
            self._record_start_failure(component, "TRACE_SPAN_START_FAILED")
            return SpanHandle(None,None,None,time.monotonic())
        handle = self._recorder.start_span(**kwargs)
        if handle.context is None:
            return handle
        return _FaultInjectingSpanHandle(
            handle,
            self._fault_controller,
            self._cancellation_token,
            component,
        )

    def _record_start_failure(self, component, error_code):
        reporter = getattr(self._recorder, "record_drop", None)
        if callable(reporter):
            try: reporter(component, "recorder_start_failed")
            except Exception: pass
        health = getattr(self._recorder, "record_fault", None)
        if callable(health):
            try: health("start", error_code)
            except Exception: pass

    def snapshot(self): return self._recorder.snapshot()
    def health_snapshot(self): return self._recorder.health_snapshot()
    def flush(self, timeout_seconds=None):
        started = time.monotonic()
        try:
            _execute_trace_fault(
                self._fault_controller,
                FaultPoint.TRACE_BEFORE_FLUSH,
                component="trace_recorder",
                operation_kind="TRACE_FLUSH",
                cancellation_token=self._cancellation_token,
                timeout_seconds=timeout_seconds,
            )
        except RunCancelledError:
            self._record_flush_failure("TRACE_FLUSH_CANCELLED")
            raise TraceOperationError("TRACE_FLUSH_CANCELLED") from None
        except TimeoutError:
            self._record_flush_failure("TRACE_FLUSH_TIMEOUT")
            raise TraceOperationError("TRACE_FLUSH_TIMEOUT") from None
        except InjectedFaultError:
            self._record_flush_failure("TRACE_FLUSH_FAILED")
            raise TraceOperationError("TRACE_FLUSH_FAILED") from None
        remaining = (
            None
            if timeout_seconds is None
            else max(0.0, float(timeout_seconds) - (time.monotonic() - started))
        )
        return self._recorder.flush(remaining)
    def _record_flush_failure(self, error_code):
        reporter = getattr(self._recorder, "record_fault", None)
        if callable(reporter):
            try: reporter("flush", error_code)
            except Exception: pass
    def close(self, timeout_seconds=None):
        # The run facade does not own the application-scoped recorder lifecycle.
        return None


def _execute_trace_fault(
    controller: FaultInjectionController | None,
    point: FaultPoint,
    *,
    component: str,
    operation_kind: str,
    cancellation_token: CancellationToken | None,
    timeout_seconds: float | None = None,
) -> None:
    if cancellation_token is not None:
        cancellation_token.raise_if_cancelled()
    if controller is None:
        return
    if timeout_seconds is not None and (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or timeout_seconds < 0
    ):
        raise ValueError("timeout_seconds must be a non-negative number or None")
    started = time.monotonic()

    def raise_if_cancelled_or_timed_out() -> None:
        if cancellation_token is not None:
            cancellation_token.raise_if_cancelled()
        if (
            timeout_seconds is not None
            and time.monotonic() - started >= float(timeout_seconds)
        ):
            raise TimeoutError

    controller.execute_blocking_if_matched(
        FaultMatchContext(
            fault_point=point,
            component=component,
            operation_kind=operation_kind,
        ),
        raise_if_cancelled=raise_if_cancelled_or_timed_out,
        allowed_actions={
            FaultAction.RAISE_TYPED_ERROR,
            FaultAction.DELAY,
            FaultAction.BLOCK_UNTIL_RELEASED,
        },
    )

class NoopSpanRecorder(InMemorySpanRecorder):
    def start_span(self, **kwargs):
        return SpanHandle(None,None,None,time.monotonic())
    def record(self,record): return None
    def snapshot(self): return ()
    def health_snapshot(self): return SpanRecorderHealth(0,0,0)

class OpenTelemetryCompatibleSpanAdapter(InMemorySpanRecorder):
    """Local adapter exposing OTel-shaped dictionaries without an exporter."""
    def export_snapshot(self):
        return tuple({"trace_id":r.trace_id,"span_id":r.span_id,"parent_span_id":r.parent_span_id,"name":r.operation,"start_time":r.started_at,"end_time":r.completed_at,"status":r.status.value,"attributes":dict(r.attributes)} for r in self.snapshot())

def start_span_safely(recorder: SpanRecorder, **kwargs) -> SpanHandle:
    """Isolate a broken recorder without changing or retrying runtime work."""
    try:
        return recorder.start_span(**kwargs)
    except Exception:
        reporter = getattr(recorder, "record_drop", None)
        if callable(reporter):
            try: reporter(kwargs.get("component", "runtime"), "recorder_start_failed")
            except Exception: pass
        return NoopSpanRecorder().start_span(**kwargs)

@contextmanager
def activate_span(handle: SpanHandle) -> Iterator[SpanHandle]:
    token=install_trace_context(handle.context)
    try: yield handle
    except TimeoutError: handle.end_timed_out(); raise
    except BaseException as exc:
        if exc.__class__.__name__ == "CancelledError": handle.end_cancelled()
        else: handle.end_error()
        raise
    else: handle.end_ok()
    finally: reset_trace_context(token)
