#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""单 Run、内存级、有界且阻塞 Producer 的 Runtime Event Channel。"""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import AsyncIterator, Protocol

from core.runtime.cancellation import CancellationToken
from core.runtime.event_journal import (
    JournalAppendStatus,
    JournalRecord,
    RunEventJournal,
)
from core.runtime.events import RuntimeEvent, RuntimeEventDraft


class ObservabilityRecordSubmitter(Protocol):
    def try_submit(self, record: JournalRecord) -> bool: ...


class EventChannelState(str, Enum):
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    ABORTED = "ABORTED"


class EventChannelClosedError(RuntimeError):
    """Channel 不再接受事件时抛出。"""


_END = object()


class RuntimeEventChannel:
    """每次 Run 独占的单 Consumer 有界异步队列。"""

    def __init__(
        self,
        capacity: int,
        *,
        run_id: str,
        cancellation_token: CancellationToken | None = None,
        journal: RunEventJournal | None = None,
        observability_dispatcher: ObservabilityRecordSubmitter | None = None,
    ) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("capacity 必须是正整数且不能是 bool")
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id 必须是非空字符串")
        self.capacity = capacity
        self.run_id = run_id
        self._queue: asyncio.Queue[RuntimeEvent | object] = asyncio.Queue(
            maxsize=capacity
        )
        self._state = EventChannelState.OPEN
        self._publish_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._abort_event = asyncio.Event()
        self._journal = journal
        self._observability_dispatcher = observability_dispatcher
        self._sequence = (
            journal.last_sequence(run_id) or 0 if journal is not None else 0
        )
        self._cancellation_token = cancellation_token
        self._consumer_attached = False
        self._end_enqueued = False

    @property
    def state(self) -> EventChannelState:
        return self._state

    @property
    def buffered_count(self) -> int:
        # End Sentinel 只属于 Channel 控制面，不计入用户可见事件数量。
        return max(0, self._queue.qsize() - int(self._end_enqueued))

    @property
    def is_closed(self) -> bool:
        return self._state in {EventChannelState.CLOSED, EventChannelState.ABORTED}

    @property
    def consumer_attached(self) -> bool:
        return self._consumer_attached

    async def publish(
        self,
        draft: RuntimeEventDraft,
        *,
        ignore_run_cancellation: bool = False,
    ) -> RuntimeEvent:
        """在同一发布锁内按 Journal-first 顺序持久化并入队。

        Channel 仍是 per-run sequence 的唯一所有者。Journal 成功后即消费
        该 sequence；若随后 Transport 失败，记录保留且序号不会被复用。
        """
        if not isinstance(draft, RuntimeEventDraft):
            raise TypeError("publish 只接受 RuntimeEventDraft")
        if draft.run_id != self.run_id:
            raise ValueError("事件 run_id 与 Channel 所属 Run 不一致")
        # close 一旦进入 CLOSING，新调用无需排在 in-flight Publisher 后等待。
        self._ensure_open()
        async with self._publish_lock:
            # 与 close 竞争时再次校验；只有通过此处的调用才是 accepted Publisher。
            self._ensure_open()
            sequence = self._sequence + 1
            event = RuntimeEvent.from_draft(draft, sequence)
            if self._journal is not None:
                append_status = self._journal.append(event)
                self._sequence = sequence
                if (
                    append_status is JournalAppendStatus.APPENDED
                    and self._observability_dispatcher is not None
                ):
                    try:
                        self._observability_dispatcher.try_submit(
                            JournalRecord.from_event(event)
                        )
                    except Exception:
                        # Observability 永远不能改变 Journal 或 Runtime Transport。
                        pass
            await self._put_interruptibly(
                event, ignore_run_cancellation=ignore_run_cancellation
            )
            if self._journal is None:
                self._sequence = sequence
            return event

    async def close(self) -> None:
        """幂等正常关闭；保留已排队事件，并把 End Sentinel 放在最后。"""
        async with self._close_lock:
            if self._state in {EventChannelState.CLOSED, EventChannelState.ABORTED}:
                return
            # 先拒绝尚未通过 OPEN 校验的新 Publisher。已经持有 publish lock 的
            # Publisher 是 accepted/in-flight Publisher，close 必须等待它退出。
            self._state = EventChannelState.CLOSING
            async with self._publish_lock:
                if self._state == EventChannelState.ABORTED:
                    return
                try:
                    await self._put_end_interruptibly()
                except EventChannelClosedError:
                    return
                if self._state != EventChannelState.ABORTED:
                    self._state = EventChannelState.CLOSED

    async def abort(self) -> None:
        """不可恢复地关闭传输并解除所有阻塞 Publisher。"""
        # CLOSED 与 ABORTED 都是稳定终态；竞争时先完成的终态生效。
        if self._state in {EventChannelState.CLOSED, EventChannelState.ABORTED}:
            return
        self._state = EventChannelState.ABORTED
        self._abort_event.set()
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._end_enqueued = False

    def __aiter__(self) -> AsyncIterator[RuntimeEvent]:
        if self._consumer_attached:
            raise RuntimeError("RuntimeEventChannel 只支持一个 Consumer")
        self._consumer_attached = True
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[RuntimeEvent]:
        while True:
            if self._state == EventChannelState.ABORTED and self._queue.empty():
                return
            item = await self._queue.get()
            if item is _END:
                return
            if self._state == EventChannelState.ABORTED:
                return
            if not isinstance(item, RuntimeEvent):  # pragma: no cover - 内部不变量
                raise RuntimeError("Event Channel 收到未知内部条目")
            yield item

    def _ensure_open(self) -> None:
        if self._state != EventChannelState.OPEN:
            raise EventChannelClosedError(
                f"Event Channel 已处于 {self._state.value}，不能继续 publish"
            )

    async def _put_interruptibly(
        self,
        event: RuntimeEvent,
        *,
        ignore_run_cancellation: bool,
    ) -> None:
        put_task = asyncio.create_task(self._queue.put(event))
        abort_task = asyncio.create_task(self._abort_event.wait())
        cancel_task: asyncio.Task[None] | None = None
        if self._cancellation_token is not None and not ignore_run_cancellation:
            cancel_task = asyncio.create_task(
                self._cancellation_token.wait_cancelled()
            )
        waiters = {put_task, abort_task}
        if cancel_task is not None:
            waiters.add(cancel_task)
        try:
            done, _ = await asyncio.wait(
                waiters, return_when=asyncio.FIRST_COMPLETED
            )
            if put_task in done and self._state != EventChannelState.ABORTED:
                return
            if self._state == EventChannelState.ABORTED:
                while True:
                    try:
                        self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                raise EventChannelClosedError("Event Channel 已 abort")
            put_task.cancel()
            await asyncio.gather(put_task, return_exceptions=True)
            if abort_task in done:
                raise EventChannelClosedError("Event Channel 已 abort")
            assert self._cancellation_token is not None
            self._cancellation_token.raise_if_cancelled()
        finally:
            for task in waiters:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

    async def _put_end_interruptibly(self) -> None:
        put_task = asyncio.create_task(self._queue.put(_END))
        abort_task = asyncio.create_task(self._abort_event.wait())
        try:
            done, _ = await asyncio.wait(
                {put_task, abort_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if put_task in done and self._state != EventChannelState.ABORTED:
                self._end_enqueued = True
                return
            put_task.cancel()
            await asyncio.gather(put_task, return_exceptions=True)
            raise EventChannelClosedError("Event Channel 已 abort")
        finally:
            if not put_task.done():
                put_task.cancel()
            if not abort_task.done():
                abort_task.cancel()
            await asyncio.gather(
                put_task, abort_task, return_exceptions=True
            )
