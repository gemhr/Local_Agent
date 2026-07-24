#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""单个 Run 的线程安全预算账本；不保存任何业务正文。"""
from __future__ import annotations
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from enum import Enum
from math import isfinite
from threading import Lock
from time import perf_counter as monotonic
from typing import Callable
from uuid import uuid4

_DIMENSIONS = (
    "step_starts",
    "model_calls",
    "remote_model_calls",
    "tool_calls",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cost_units",
    "retries",
    "retrieval_calls",
    "embedding_calls",
    "vector_queries",
    "keyword_queries",
    "document_reads",
    "context_chars",
)
_LIMITS = {
    "step_starts": "max_step_starts",
    "model_calls": "max_model_calls",
    "remote_model_calls": "max_remote_model_calls",
    "tool_calls": "max_tool_calls",
    "input_tokens": "max_input_tokens",
    "output_tokens": "max_output_tokens",
    "total_tokens": "max_total_tokens",
    "cost_units": "max_cost_units",
    "retries": "max_retries",
    "retrieval_calls": "max_retrieval_calls",
    "embedding_calls": "max_embedding_calls",
    "vector_queries": "max_vector_queries",
    "keyword_queries": "max_keyword_queries",
    "document_reads": "max_document_reads",
    "context_chars": "max_context_chars",
}

@dataclass(frozen=True, slots=True)
class RunBudget:
    max_step_starts: int|None=None; max_model_calls: int|None=None; max_remote_model_calls: int|None=None; max_tool_calls: int|None=None
    max_input_tokens: int|None=None; max_output_tokens: int|None=None; max_total_tokens: int|None=None; max_cost_units: int|None=None
    max_elapsed_seconds: float|None=None; max_concurrency: int|None=None; max_retries: int|None=None
    max_retrieval_calls: int|None=None; max_embedding_calls: int|None=None; max_vector_queries: int|None=None; max_keyword_queries: int|None=None
    max_document_reads: int|None=None; max_context_chars: int|None=None
    def __post_init__(self):
        for item in fields(self):
            value = getattr(self, item.name)
            if value is None: continue
            if item.name == "max_elapsed_seconds":
                if isinstance(value, bool) or not isinstance(value, (int,float)) or not isfinite(value) or value <= 0: raise ValueError("max_elapsed_seconds 必须是有限正数")
            elif item.name == "max_concurrency":
                if isinstance(value, bool) or not isinstance(value, int) or value <= 0: raise ValueError("max_concurrency 必须是正整数")
            elif isinstance(value, bool) or not isinstance(value, int) or value < 0: raise ValueError(f"{item.name} 必须是非负整数或 None")

@dataclass(frozen=True, slots=True)
class BudgetUsage:
    step_starts:int=0; model_calls:int=0; remote_model_calls:int=0; tool_calls:int=0; input_tokens:int=0; output_tokens:int=0; total_tokens:int=0; cost_units:int=0; retries:int=0
    retrieval_calls:int=0; embedding_calls:int=0; vector_queries:int=0; keyword_queries:int=0; document_reads:int=0; context_chars:int=0
    _allow_independent_total: bool=False
    def __post_init__(self):
        for name in _DIMENSIONS:
            value=getattr(self,name)
            if isinstance(value,bool) or not isinstance(value,int) or value<0: raise ValueError(f"{name} 必须是非负整数")
        if not self._allow_independent_total and self.total_tokens < self.input_tokens + self.output_tokens: raise ValueError("total_tokens 不得小于 input_tokens + output_tokens")
    def plus(self, other:"BudgetUsage")->"BudgetUsage": return BudgetUsage(**{n:getattr(self,n)+getattr(other,n) for n in _DIMENSIONS})
    def minus(self, other:"BudgetUsage")->"BudgetUsage": return BudgetUsage(**{n:getattr(self,n)-getattr(other,n) for n in _DIMENSIONS})

class UsageSource(str,Enum): ACTUAL="ACTUAL"; ESTIMATED="ESTIMATED"
@dataclass(frozen=True,slots=True)
class BudgetReservation:
    reservation_id:str; reservation_type:str; step_id:str|None; reserved_usage:BudgetUsage; created_at:datetime
@dataclass(frozen=True,slots=True)
class BudgetSnapshot:
    run_budget:RunBudget; committed_usage:BudgetUsage; reserved_usage:BudgetUsage; remaining:BudgetUsage; elapsed_seconds:float; remaining_time_seconds:float|None; active_reservation_count:int; exhausted_dimensions:tuple[str,...]; generated_at:datetime
class BudgetExceededError(RuntimeError):
    def __init__(self, dimension:str, requested:int|float, remaining:int|float|None, snapshot:BudgetSnapshot, *, time:bool=False):
        self.error_code="TIME_BUDGET_EXHAUSTED" if time else "BUDGET_EXHAUSTED"; self.dimension=dimension; self.requested=requested; self.remaining=remaining; self.snapshot=snapshot
        super().__init__("时间预算已耗尽" if time else "预算额度不足")
class BudgetReservationError(RuntimeError): pass

class BudgetLedger:
    """一个实例只属于一个 Run；锁内仅执行同步算术和状态转换。"""
    def __init__(self,budget:RunBudget, *, deadline_remaining:Callable[[],float|None]|None=None):
        self.budget=budget; self._deadline_remaining=deadline_remaining; self._started=monotonic(); self._lock=Lock(); self._committed=BudgetUsage(); self._reservations:dict[str,BudgetReservation]={}
    def _reserved(self):
        value=BudgetUsage()
        for r in self._reservations.values(): value=value.plus(r.reserved_usage)
        return value
    def _snapshot_locked(self):
        reserved=self._reserved(); elapsed=monotonic()-self._started; candidates=[]
        if self.budget.max_elapsed_seconds is not None: candidates.append(max(0.0,self.budget.max_elapsed_seconds-elapsed))
        if self._deadline_remaining is not None:
            rem=self._deadline_remaining()
            if rem is not None: candidates.append(max(0.0,rem))
        rt=min(candidates) if candidates else None
        remaining={}
        for dim in _DIMENSIONS:
            limit=getattr(self.budget,_LIMITS[dim]); remaining[dim]=max(0,limit-getattr(self._committed,dim)-getattr(reserved,dim)) if limit is not None else 0
        exhausted=tuple(dim for dim in _DIMENSIONS if getattr(self.budget,_LIMITS[dim]) is not None and remaining[dim]==0)
        if rt is not None and rt<=0: exhausted+=("elapsed_seconds",)
        return BudgetSnapshot(self.budget,self._committed,reserved,BudgetUsage(**remaining, _allow_independent_total=True),elapsed,rt,len(self._reservations),exhausted,datetime.now(UTC))
    def snapshot(self):
        with self._lock: return self._snapshot_locked()
    def reserve(self, usage:BudgetUsage, *, reservation_type:str, step_id:str|None=None):
        with self._lock:
            snap=self._snapshot_locked()
            if snap.remaining_time_seconds is not None and snap.remaining_time_seconds<=0: raise BudgetExceededError("elapsed_seconds",1,0,snap,time=True)
            for dim in _DIMENSIONS:
                limit=getattr(self.budget,_LIMITS[dim]); requested=getattr(usage,dim)
                if limit is not None and getattr(self._committed,dim)+getattr(snap.reserved_usage,dim)+requested>limit: raise BudgetExceededError(dim,requested,getattr(snap.remaining,dim),snap)
            r=BudgetReservation(uuid4().hex,reservation_type,step_id,usage,datetime.now(UTC)); self._reservations[r.reservation_id]=r; return r
    def commit(self,reservation:BudgetReservation, actual_usage:BudgetUsage|None=None, *, usage_source:UsageSource=UsageSource.ACTUAL):
        with self._lock:
            stored=self._reservations.get(reservation.reservation_id)
            if stored != reservation: raise BudgetReservationError("Reservation 未知、已结算或已释放")
            actual=actual_usage if actual_usage is not None else reservation.reserved_usage
            # 调用发生且未知实际值时调用方传 None，保守结算预留。
            # 实际值小于预留时原子退款；大于预留时在同一把锁内补差并重新校验，
            # 不允许已完成调用把任何有限预算推到上限之外。
            self._reservations.pop(reservation.reservation_id)
            snapshot_without_current=self._snapshot_locked()
            for dim in _DIMENSIONS:
                limit=getattr(self.budget,_LIMITS[dim])
                requested=getattr(actual,dim)
                if limit is not None and getattr(self._committed,dim)+getattr(snapshot_without_current.reserved_usage,dim)+requested>limit:
                    self._reservations[reservation.reservation_id]=reservation
                    snapshot=self._snapshot_locked()
                    raise BudgetExceededError(dim,requested,getattr(snapshot.remaining,dim),snapshot)
            self._committed=self._committed.plus(actual); return self._snapshot_locked()
    def release(self,reservation:BudgetReservation):
        with self._lock:
            stored=self._reservations.pop(reservation.reservation_id,None)
            if stored != reservation: raise BudgetReservationError("Reservation 未知、已结算或已释放")
            return self._snapshot_locked()

class BudgetPolicy:
    """只根据快照作可行性判断，不改账本。"""
    @staticmethod
    def feasible(snapshot:BudgetSnapshot, usage:BudgetUsage)->bool:
        if snapshot.remaining_time_seconds is not None and snapshot.remaining_time_seconds<=0:return False
        return all(getattr(snapshot.run_budget,_LIMITS[d]) is None or getattr(usage,d)<=getattr(snapshot.remaining,d) for d in _DIMENSIONS)

class BudgetedModelStream:
    """将惰性流的首次底层迭代定义为请求开始边界。

    在首次迭代前关闭会 release；一旦进入底层 next，即使抛错或中断也以
    Actual（若提供）或完整保守预留 commit。``total_tokens`` 是 Provider 的权威
    总量，可包含未拆分额外 token；没有 Provider total 时使用 input + output。
    """
    def __init__(self, stream, ledger: BudgetLedger, reservation: BudgetReservation, actual_usage_getter=None):
        self._stream=iter(stream); self._ledger=ledger; self._reservation=reservation; self._getter=actual_usage_getter; self._started=False; self._closed=False
    def __iter__(self): return self
    def __next__(self):
        if self._closed: raise StopIteration
        self._started=True
        try: return next(self._stream)
        except BaseException:
            self.close(); raise
    def close(self):
        if self._closed: return
        self._closed=True
        try:
            close=getattr(self._stream,"close",None)
            if close: close()
        finally:
            if self._started:
                actual=self._getter(self._stream) if self._getter else None
                self._ledger.commit(self._reservation, actual, usage_source=UsageSource.ACTUAL if actual else UsageSource.ESTIMATED)
            else: self._ledger.release(self._reservation)
