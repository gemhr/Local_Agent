#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Run-scoped typed StepResultStore.

Ownership:
    - one store per dynamic Run;
    - only the minimal result completion skeleton writes;
    - consumers read through a dependency-scoped ACL only;
    - the Store is sealed before Run terminal cleanup and cleared afterwards.

Raw content is held in memory only and never enters repr, logs, events,
snapshot, journal or trace.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
import threading

from core.runtime.planning import OutputPolicy, Plan
from core.runtime.state import AgentState, StepStatus
from core.runtime.scheduler import StepClaim
from core.runtime.step_result import ResultContentType, StepResult


class StepResultStoreErrorCode(str, Enum):
    UNKNOWN_PRODUCER = "UNKNOWN_PRODUCER"
    DUPLICATE_WRITE = "DUPLICATE_WRITE"
    STORE_SEALED = "STORE_SEALED"
    STORE_CLEARED = "STORE_CLEARED"
    PREPARED_NOT_READABLE = "PREPARED_NOT_READABLE"
    PRODUCER_NOT_SUCCEEDED = "PRODUCER_NOT_SUCCEEDED"
    CONSUMER_NOT_CLAIMED = "CONSUMER_NOT_CLAIMED"
    CONSUMER_NOT_DEPENDENT = "CONSUMER_NOT_DEPENDENT"
    ENTRY_NOT_READABLE = "ENTRY_NOT_READABLE"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    READ_NOT_ALLOWED = "READ_NOT_ALLOWED"
    CAPACITY_EXCEEDED = "CAPACITY_EXCEEDED"
    INVALID_LIMIT = "INVALID_LIMIT"


class StepResultStoreError(LookupError):
    """Stable safe error without raw content or instruction text."""

    def __init__(
        self,
        error_code: StepResultStoreErrorCode,
        safe_message: str,
    ) -> None:
        self.error_code = error_code
        self.safe_message = safe_message
        super().__init__(f"{safe_message} (error_code={error_code.value})")


class StoreEntryStatus(str, Enum):
    PREPARED = "PREPARED"
    READABLE = "READABLE"


class StoreStatus(str, Enum):
    OPEN = "OPEN"
    SEALED = "SEALED"
    CLEARED = "CLEARED"


class DependencyResultEntry:
    """One read-only dependency entry delivered to a consumer.

    Raw content is allowed here (dependency result view), but repr and
    serialization stay redacted.
    """

    __slots__ = (
        "_step_id",
        "_producer_agent_id",
        "_content_type",
        "_content",
        "_complete",
        "_locked",
    )

    def __init__(
        self,
        step_id: str,
        producer_agent_id: str,
        content_type: ResultContentType,
        content: str,
        complete: bool,
    ) -> None:
        object.__setattr__(self, "_step_id", step_id)
        object.__setattr__(self, "_producer_agent_id", producer_agent_id)
        object.__setattr__(self, "_content_type", content_type)
        object.__setattr__(self, "_content", content)
        object.__setattr__(self, "_complete", complete)
        object.__setattr__(self, "_locked", True)

    def __setattr__(self, name, value) -> None:
        if getattr(self, "_locked", False):
            raise AttributeError("DependencyResultEntry 是不可变对象")
        object.__setattr__(self, name, value)

    @property
    def step_id(self) -> str:
        return self._step_id

    @property
    def producer_agent_id(self) -> str:
        return self._producer_agent_id

    @property
    def content_type(self) -> ResultContentType:
        return self._content_type

    @property
    def content(self) -> str:
        return self._content

    @property
    def complete(self) -> bool:
        return self._complete

    @property
    def char_count(self) -> int:
        return len(self._content)

    def __repr__(self) -> str:
        return (
            "DependencyResultEntry("
            f"step_id={self.step_id!r}, "
            f"producer_agent_id={self.producer_agent_id!r}, "
            f"content_type={self.content_type.value!r}, "
            f"char_count={self.char_count}, complete={self.complete!r}, "
            "content=<redacted>)"
        )

    def __getstate__(self):
        raise TypeError("DependencyResultEntry 不允许序列化")


class DependencyResultView:
    """Stable-ordered read-only view over explicit dependencies."""

    __slots__ = ("_entries", "_by_step", "_locked")

    def __init__(self, entries: tuple[DependencyResultEntry, ...]) -> None:
        mapping: dict[str, DependencyResultEntry] = {}
        for entry in entries:
            if not isinstance(entry, DependencyResultEntry):
                raise TypeError("dependency view 只能包含 DependencyResultEntry")
            if entry.step_id in mapping:
                raise ValueError("dependency view 不允许重复 step_id")
            mapping[entry.step_id] = entry
        object.__setattr__(self, "_entries", entries)
        object.__setattr__(self, "_by_step", mapping)
        object.__setattr__(self, "_locked", True)

    def __setattr__(self, name, value) -> None:
        if getattr(self, "_locked", False):
            raise AttributeError("DependencyResultView 是不可变对象")
        object.__setattr__(self, name, value)

    @property
    def entries(self) -> tuple[DependencyResultEntry, ...]:
        return self._entries

    def __iter__(self):
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __getitem__(self, index):
        return self._entries[index]

    def get(self, step_id: str) -> DependencyResultEntry | None:
        return self._by_step.get(step_id)

    def __repr__(self) -> str:
        return (
            "DependencyResultView("
            f"count={len(self._entries)}, entries=<redacted>)"
        )

    def __getstate__(self):
        raise TypeError("DependencyResultView 不允许序列化")


class _StoreEntry:
    """Internal store entry; safe repr, raw content never leaves memory."""

    __slots__ = ("step_id", "producer_agent_id", "content_type", "content", "complete", "status", "created_at", "readable_at")

    def __init__(
        self,
        *,
        step_id: str,
        producer_agent_id: str,
        content_type: ResultContentType,
        content: str,
        complete: bool,
        created_at: datetime,
    ) -> None:
        self.step_id = step_id
        self.producer_agent_id = producer_agent_id
        self.content_type = content_type
        self.content = content
        self.complete = complete
        self.status = StoreEntryStatus.PREPARED
        self.created_at = created_at
        self.readable_at: datetime | None = None

    def __repr__(self) -> str:
        return (
            "_StoreEntry("
            f"step_id={self.step_id!r}, "
            f"producer_agent_id={self.producer_agent_id!r}, "
            f"status={self.status.value}, content=<redacted>)"
        )

    def to_dependency_entry(self) -> DependencyResultEntry:
        return DependencyResultEntry(
            self.step_id,
            self.producer_agent_id,
            self.content_type,
            self.content,
            self.complete,
        )


class StepResultStore:
    """Once-write, dependency-scoped, capacity-limited result store."""

    def __init__(
        self,
        plan: Plan,
        *,
        run_id: str,
        per_result_chars: int = 20_000,
        run_total_chars: int = 60_000,
        max_entries: int = 16,
    ) -> None:
        if not isinstance(plan, Plan):
            raise TypeError("StepResultStore 需要冻结的 Plan")
        for value, name in (
            (per_result_chars, "per_result_chars"),
            (run_total_chars, "run_total_chars"),
            (max_entries, "max_entries"),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
            ):
                raise StepResultStoreError(
                    StepResultStoreErrorCode.INVALID_LIMIT,
                    f"{name} 必须是正整数",
                )
        if run_total_chars < per_result_chars:
            raise StepResultStoreError(
                StepResultStoreErrorCode.INVALID_LIMIT,
                "run_total_chars 不得小于 per_result_chars",
            )
        if not isinstance(run_id, str) or not run_id.strip():
            raise StepResultStoreError(
                StepResultStoreErrorCode.INVALID_LIMIT,
                "run_id 不能为空",
            )
        self._plan = plan
        self._run_id = run_id
        self._per_result_chars = per_result_chars
        self._run_total_chars = run_total_chars
        self._max_entries = max_entries
        self._entries: dict[str, _StoreEntry] = {}
        self._status = StoreStatus.OPEN
        self._lock = threading.RLock()

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def status(self) -> StoreStatus:
        with self._lock:
            return self._status

    @property
    def is_sealed(self) -> bool:
        return self.status is StoreStatus.SEALED

    @property
    def is_cleared(self) -> bool:
        return self.status is StoreStatus.CLEARED

    def __repr__(self) -> str:
        with self._lock:
            return (
                "StepResultStore("
                f"run_id={self.run_id!r}, status={self._status.value}, "
                f"entries={len(self._entries)})"
            )

    def _require_open(self) -> None:
        if self._status is StoreStatus.SEALED:
            raise StepResultStoreError(
                StepResultStoreErrorCode.STORE_SEALED,
                "Store 已 seal，拒绝读写",
            )
        if self._status is StoreStatus.CLEARED:
            raise StepResultStoreError(
                StepResultStoreErrorCode.STORE_CLEARED,
                "Store 已 clear，拒绝读写",
            )

    def _plan_step(self, step_id: str):
        for step in self._plan.steps:
            if step.step_id == step_id:
                return step
        return None

    def write_prepared(
        self,
        entry: StepResult,
        *,
        expected_agent_id: str,
    ) -> None:
        """Validate and store one PREPARED entry; once-write per logical Step."""
        if not isinstance(entry, StepResult):
            raise StepResultStoreError(
                StepResultStoreErrorCode.UNKNOWN_PRODUCER,
                "write_prepared 需要 StepResult",
            )
        with self._lock:
            self._require_open()
            plan_step = self._plan_step(entry.step_id)
            if plan_step is None:
                raise StepResultStoreError(
                    StepResultStoreErrorCode.UNKNOWN_PRODUCER,
                    "producer Step 不属于当前 Plan",
                )
            if plan_step.preferred_agent != expected_agent_id:
                raise StepResultStoreError(
                    StepResultStoreErrorCode.IDENTITY_MISMATCH,
                    "expected agent 与 Plan 不一致",
                )
            if entry.producer_agent_id != expected_agent_id:
                raise StepResultStoreError(
                    StepResultStoreErrorCode.IDENTITY_MISMATCH,
                    "result producer 与 claim/Plan 不一致",
                )
            if entry.step_id in self._entries:
                raise StepResultStoreError(
                    StepResultStoreErrorCode.DUPLICATE_WRITE,
                    "每个 logical Step 只允许一次成功写入",
                )
            if len(entry.content) > self._per_result_chars:
                raise StepResultStoreError(
                    StepResultStoreErrorCode.CAPACITY_EXCEEDED,
                    "单结果大小超过上限",
                )
            total = sum(len(item.content) for item in self._entries.values())
            if total + len(entry.content) > self._run_total_chars:
                raise StepResultStoreError(
                    StepResultStoreErrorCode.CAPACITY_EXCEEDED,
                    "Run 结果总字符数超过上限",
                )
            if len(self._entries) >= self._max_entries:
                raise StepResultStoreError(
                    StepResultStoreErrorCode.CAPACITY_EXCEEDED,
                    "Store 条目数量超过上限",
                )
            self._entries[entry.step_id] = _StoreEntry(
                step_id=entry.step_id,
                producer_agent_id=entry.producer_agent_id,
                content_type=entry.content_type,
                content=entry.content,
                complete=entry.complete,
                created_at=datetime.now(UTC),
            )

    def mark_readable(self, step_id: str, agent_state: AgentState) -> None:
        """PREPARED -> READABLE only after producer Step is SUCCEEDED."""
        if not isinstance(agent_state, AgentState):
            raise TypeError("mark_readable 需要 AgentState")
        with self._lock:
            self._require_open()
            store_entry = self._entries.get(step_id)
            if store_entry is None:
                raise StepResultStoreError(
                    StepResultStoreErrorCode.UNKNOWN_PRODUCER,
                    "没有可标记的 PREPARED entry",
                )
            if store_entry.status is StoreEntryStatus.READABLE:
                raise StepResultStoreError(
                    StepResultStoreErrorCode.DUPLICATE_WRITE,
                    "entry 已 READABLE，禁止重复提交",
                )
            step_state = agent_state.steps.get(step_id)
            if step_state is None or step_state.status is not StepStatus.SUCCEEDED:
                raise StepResultStoreError(
                    StepResultStoreErrorCode.PRODUCER_NOT_SUCCEEDED,
                    "producer Step 尚未 SUCCEEDED",
                )
            store_entry.status = StoreEntryStatus.READABLE
            store_entry.readable_at = datetime.now(UTC)

    def dependency_view_for(
        self,
        consumer_claim: StepClaim,
        agent_state: AgentState,
    ) -> DependencyResultView:
        """Return explicit dependencies in compiled depends_on order."""
        if not isinstance(consumer_claim, StepClaim):
            raise TypeError("dependency_view_for 需要 StepClaim")
        if not isinstance(agent_state, AgentState):
            raise TypeError("dependency_view_for 需要 AgentState")
        with self._lock:
            self._require_open()
            if consumer_claim.plan_id != self._plan.plan_id:
                raise StepResultStoreError(
                    StepResultStoreErrorCode.CONSUMER_NOT_CLAIMED,
                    "consumer claim 不属于当前 Plan",
                )
            plan_step = self._plan_step(consumer_claim.step_id)
            if plan_step is None:
                raise StepResultStoreError(
                    StepResultStoreErrorCode.CONSUMER_NOT_CLAIMED,
                    "consumer Step 不属于当前 Plan",
                )
            if plan_step.preferred_agent != consumer_claim.preferred_agent:
                raise StepResultStoreError(
                    StepResultStoreErrorCode.CONSUMER_NOT_CLAIMED,
                    "consumer claim agent 与 Plan 不一致",
                )
            if not plan_step.depends_on:
                raise StepResultStoreError(
                    StepResultStoreErrorCode.READ_NOT_ALLOWED,
                    "consumer 没有显式依赖，禁止读取结果",
                )
            entries: list[DependencyResultEntry] = []
            for producer_id in plan_step.depends_on:
                producer_step = self._plan_step(producer_id)
                if producer_step is None:
                    raise StepResultStoreError(
                        StepResultStoreErrorCode.UNKNOWN_PRODUCER,
                        "依赖 producer Step 不属于当前 Plan",
                    )
                store_entry = self._entries.get(producer_id)
                if store_entry is None:
                    raise StepResultStoreError(
                        StepResultStoreErrorCode.ENTRY_NOT_READABLE,
                        "缺少 required producer result",
                    )
                if store_entry.status is not StoreEntryStatus.READABLE:
                    raise StepResultStoreError(
                        StepResultStoreErrorCode.ENTRY_NOT_READABLE,
                        "producer result 尚未 READABLE",
                    )
                producer_state = agent_state.steps.get(producer_id)
                if (
                    producer_state is None
                    or producer_state.status is not StepStatus.SUCCEEDED
                ):
                    raise StepResultStoreError(
                        StepResultStoreErrorCode.PRODUCER_NOT_SUCCEEDED,
                        "producer Step 尚未 SUCCEEDED",
                    )
                if store_entry.producer_agent_id != producer_step.preferred_agent:
                    raise StepResultStoreError(
                        StepResultStoreErrorCode.IDENTITY_MISMATCH,
                        "producer identity 与 Plan 不一致",
                    )
                entries.append(store_entry.to_dependency_entry())
            return DependencyResultView(tuple(entries))

    def has_readable(self, step_id: str) -> bool:
        """Safe existence check used by the Coordinator final-output guard."""
        with self._lock:
            if self._status is not StoreStatus.OPEN:
                return False
            store_entry = self._entries.get(step_id)
            return (
                store_entry is not None
                and store_entry.status is StoreEntryStatus.READABLE
            )

    def read_final_content(self, step_id: str) -> str:
        """Delivered-only read for the run-level final Memory writer.

        Only the unique final StepResult may be read, only while the entry is
        READABLE and the Store is OPEN. This is the sole read escape hatch and
        it is consumed exclusively by the delivered-only Memory commit owner.
        """
        with self._lock:
            if self._status is not StoreStatus.OPEN:
                raise StepResultStoreError(
                    StepResultStoreErrorCode.STORE_SEALED,
                    "Store 已 seal，拒绝读取 final 正文",
                )
            plan_step = self._plan_step(step_id)
            if plan_step is None:
                raise StepResultStoreError(
                    StepResultStoreErrorCode.UNKNOWN_PRODUCER,
                    "final Step 不属于当前 Plan",
                )
            if plan_step.output_policy is OutputPolicy.INTERNAL:
                raise StepResultStoreError(
                    StepResultStoreErrorCode.READ_NOT_ALLOWED,
                    "INTERNAL Step 正文不得读取用于交付",
                )
            store_entry = self._entries.get(step_id)
            if (
                store_entry is None
                or store_entry.status is not StoreEntryStatus.READABLE
            ):
                raise StepResultStoreError(
                    StepResultStoreErrorCode.ENTRY_NOT_READABLE,
                    "final entry 尚未 READABLE",
                )
            return store_entry.content

    def entry_count(self) -> int:
        with self._lock:
            return len(self._entries)

    def seal(self) -> None:
        """Reject new writes and reads; cleanup happens at a safe point."""
        with self._lock:
            if self._status is StoreStatus.CLEARED:
                raise StepResultStoreError(
                    StepResultStoreErrorCode.STORE_CLEARED,
                    "Store 已 clear，不能再 seal",
                )
            self._status = StoreStatus.SEALED

    def clear(self) -> None:
        """Idempotently release all raw content after Run terminal cleanup."""
        with self._lock:
            if self._status is StoreStatus.CLEARED:
                return
            self._status = StoreStatus.CLEARED
            self._entries.clear()


__all__ = [
    "DependencyResultEntry",
    "DependencyResultView",
    "StepResultStore",
    "StepResultStoreError",
    "StepResultStoreErrorCode",
    "StoreEntryStatus",
    "StoreStatus",
]
