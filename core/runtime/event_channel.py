#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""单 Run、内存级、有界且阻塞 Producer 的 Runtime Event Channel。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
import hashlib
import threading
from typing import AsyncIterator, Protocol

from core.runtime.cancellation import CancellationToken
from core.runtime.event_journal import (
    JournalAppendStatus,
    JournalRecord,
    RunEventJournal,
)
from core.runtime.events import RuntimeEvent, RuntimeEventDraft
from core.runtime.fault_injection import FaultInjectionController
from core.runtime.fault_injection_contract import (
    FaultAction,
    FaultMatchContext,
    FaultPoint,
    InjectedFaultCode,
    InjectedFaultError,
)


class ObservabilityRecordSubmitter(Protocol):
    def try_submit(self, record: JournalRecord) -> bool: ...

    async def submit(
        self,
        record: JournalRecord,
        *,
        fault_controller: FaultInjectionController | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> bool: ...


class EventChannelState(str, Enum):
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    ABORTED = "ABORTED"


class EventChannelConsumerOwner(str, Enum):
    """The sole right to remove entries from one channel."""

    TRANSPORT = "TRANSPORT"
    DRAIN = "DRAIN"
    RELEASED = "RELEASED"
    ABORTED = "ABORTED"


class EventChannelClosedError(RuntimeError):
    """Channel 不再接受事件时抛出。"""


class EventPublicationStage(str, Enum):
    BEFORE_JOURNAL_APPEND = "BEFORE_JOURNAL_APPEND"
    AFTER_JOURNAL_APPEND = "AFTER_JOURNAL_APPEND"
    BEFORE_CHANNEL_ENQUEUE = "BEFORE_CHANNEL_ENQUEUE"


@dataclass(frozen=True, slots=True)
class EventPublicationEvidence:
    """Payload-free identity facts for one failed publication attempt."""

    event_id: str
    sequence: int
    event_type: str
    publication_stage: EventPublicationStage
    partially_persisted: bool

    def __post_init__(self) -> None:
        for value, name in (
            (self.event_id, "event_id"),
            (self.event_type, "event_type"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence <= 0
        ):
            raise ValueError("sequence must be a positive integer")
        if not isinstance(self.publication_stage, EventPublicationStage):
            raise TypeError("publication_stage must be EventPublicationStage")
        if type(self.partially_persisted) is not bool:
            raise TypeError("partially_persisted must be bool")


class EventPublicationError(Exception):
    """Safe publication failure retaining no RuntimeEvent or payload."""

    def __init__(
        self,
        *,
        evidence: EventPublicationEvidence,
        fault_point: FaultPoint,
        fault_code: InjectedFaultCode,
    ) -> None:
        self.error_code = (
            "EVENT_PUBLICATION_PARTIALLY_PERSISTED"
            if evidence.partially_persisted
            else "EVENT_PUBLICATION_FAILED"
        )
        self.safe_message = (
            "Runtime Event publication partially persisted"
            if evidence.partially_persisted
            else "Runtime Event publication failed"
        )
        self.evidence = evidence
        self.fault_point = fault_point
        self.fault_code = fault_code
        super().__init__(self.safe_message)

    @property
    def partially_persisted(self) -> bool:
        return self.evidence.partially_persisted

    def __repr__(self) -> str:
        return (
            "EventPublicationError("
            f"error_code={self.error_code!r}, "
            f"evidence={self.evidence!r}, "
            f"fault_point={self.fault_point.value!r}, "
            f"fault_code={self.fault_code.value!r}, "
            f"partially_persisted={self.partially_persisted!r})"
        )


class JournalWatermarkError(RuntimeError):
    """Safe fail-closed marker for Channel/Journal sequence disagreement."""

    error_code = "JOURNAL_WATERMARK_MISMATCH"


_END = object()


class _RuntimeEventTransportConsumer(AsyncIterator[RuntimeEvent]):
    """Explicit lease so even pre-first-read aclose releases ownership."""

    def __init__(self, channel: "RuntimeEventChannel") -> None:
        self._channel = channel
        self._active = True

    def __aiter__(self) -> "_RuntimeEventTransportConsumer":
        return self

    async def __anext__(self) -> RuntimeEvent:
        if not self._active:
            raise StopAsyncIteration
        try:
            await self._channel._before_receive()
            item = await self._channel._queue.get()
        except BaseException:
            self._release(completed=False)
            raise
        if item is _END:
            self._release(
                completed=(
                    self._channel.state is not EventChannelState.ABORTED
                )
            )
            raise StopAsyncIteration
        if self._channel.state is EventChannelState.ABORTED:
            self._release(completed=False)
            raise StopAsyncIteration
        if not isinstance(item, RuntimeEvent):  # pragma: no cover
            self._release(completed=False)
            raise RuntimeError("Event Channel 收到未知内部条目")
        return item

    async def aclose(self) -> None:
        self._release(completed=False)

    def _release(self, *, completed: bool) -> None:
        if not self._active:
            return
        self._active = False
        self._channel._release_transport(completed=completed)


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
        fault_controller: FaultInjectionController | None = None,
    ) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("capacity 必须是正整数且不能是 bool")
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id 必须是非空字符串")
        if fault_controller is not None and not isinstance(
            fault_controller, FaultInjectionController
        ):
            raise TypeError(
                "fault_controller must be FaultInjectionController or None"
            )
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
        self._fault_controller = fault_controller
        self._sequence = (
            journal.last_sequence(run_id) or 0 if journal is not None else 0
        )
        self._publications_in_flight = 0
        self._cancellation_token = cancellation_token
        self._consumer_owner = EventChannelConsumerOwner.RELEASED
        self._consumer_lock = threading.Lock()
        self._transport_completed = False
        self._drain_completed = False
        self._drain_handoff_pending = False
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
        return (
            self.consumer_owner
            is not EventChannelConsumerOwner.RELEASED
        )

    @property
    def consumer_owner(self) -> EventChannelConsumerOwner:
        with self._consumer_lock:
            return self._consumer_owner

    @property
    def publications_in_flight(self) -> int:
        return self._publications_in_flight

    async def capture_journal_watermark(self) -> int:
        """Capture the existing sequence owner under the publish lock."""
        async with self._publish_lock:
            channel_sequence = self._sequence
            if self._journal is not None:
                journal_sequence = self._journal.last_sequence(self.run_id) or 0
                if journal_sequence != channel_sequence:
                    raise JournalWatermarkError(
                        "journal and channel watermarks do not match"
                    )
            return channel_sequence

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
        self._publications_in_flight += 1
        try:
            async with self._publish_lock:
                # 与 close 竞争时再次校验；只有通过此处的调用才是 accepted Publisher。
                self._ensure_open()
                sequence = self._sequence + 1
                event = RuntimeEvent.from_draft(draft, sequence)
                if self._journal is not None:
                    if event.event_type.value == "RUN_COMPLETED":
                        await self._execute_publication_fault(
                            FaultPoint.JOURNAL_BEFORE_TERMINAL_APPEND,
                            event,
                            partially_persisted=False,
                        )
                    else:
                        await self._execute_publication_fault(
                            FaultPoint.EVENT_BEFORE_JOURNAL_APPEND,
                            event,
                            partially_persisted=False,
                        )
                    append_status = self._journal.append(event)
                    self._sequence = sequence
                    await self._execute_publication_fault(
                        FaultPoint.EVENT_AFTER_JOURNAL_APPEND,
                        event,
                        partially_persisted=True,
                    )
                    if (
                        append_status is JournalAppendStatus.APPENDED
                        and self._observability_dispatcher is not None
                    ):
                        try:
                            record = JournalRecord.from_event(event)
                            submit = getattr(
                                self._observability_dispatcher,
                                "submit",
                                None,
                            )
                            if callable(submit):
                                await submit(
                                    record,
                                    fault_controller=self._fault_controller,
                                    cancellation_token=self._cancellation_token,
                                )
                            else:
                                self._observability_dispatcher.try_submit(record)
                        except Exception:
                            # Observability 永远不能改变 Journal 或 Runtime Transport。
                            pass
                await self._execute_publication_fault(
                    FaultPoint.EVENT_BEFORE_CHANNEL_ENQUEUE,
                    event,
                    partially_persisted=self._journal is not None,
                )
                await self._put_interruptibly(
                    event, ignore_run_cancellation=ignore_run_cancellation
                )
                if self._journal is None:
                    self._sequence = sequence
                return event
        finally:
            self._publications_in_flight -= 1

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
        # Wake a transport or drain consumer that is blocked on an empty queue.
        self._queue.put_nowait(_END)
        self._end_enqueued = True
        with self._consumer_lock:
            self._consumer_owner = EventChannelConsumerOwner.ABORTED

    def __aiter__(self) -> AsyncIterator[RuntimeEvent]:
        self._claim_consumer(EventChannelConsumerOwner.TRANSPORT)
        return _RuntimeEventTransportConsumer(self)

    async def drain_to_discard(self) -> None:
        """Consume queued events internally without adapting or publishing them.

        This is used only after the transport consumer has stopped.  It keeps a
        bounded producer moving until the producer closes the channel.
        """
        with self._consumer_lock:
            if self._state == EventChannelState.ABORTED:
                self._consumer_owner = EventChannelConsumerOwner.ABORTED
                return
            if self._drain_completed:
                return
            if self._transport_completed:
                raise RuntimeError(
                    "transport consumer already completed normally"
                )
            if self._consumer_owner is not EventChannelConsumerOwner.RELEASED:
                raise RuntimeError(
                    "RuntimeEventChannel consumer ownership is not released"
                )
            if self._drain_handoff_pending:
                raise RuntimeError(
                    "RuntimeEventChannel drain handoff is already pending"
                )
            # Reserve the handoff while keeping the visible owner RELEASED.
            # A late transport cannot reacquire during a fault delay/block.
            self._drain_handoff_pending = True
        try:
            await self._execute_channel_fault(
                FaultPoint.CHANNEL_BEFORE_DRAIN_HANDOFF,
                operation_kind="DRAIN_HANDOFF",
            )
            with self._consumer_lock:
                if self._state == EventChannelState.ABORTED:
                    self._consumer_owner = EventChannelConsumerOwner.ABORTED
                    self._drain_handoff_pending = False
                    return
                if self._consumer_owner is not EventChannelConsumerOwner.RELEASED:
                    raise RuntimeError(
                        "RuntimeEventChannel consumer ownership changed during handoff"
                    )
                self._consumer_owner = EventChannelConsumerOwner.DRAIN
                self._drain_handoff_pending = False
        except BaseException:
            with self._consumer_lock:
                self._drain_handoff_pending = False
            raise
        completed = False
        try:
            while True:
                await self._before_receive()
                item = await self._queue.get()
                if item is _END or self._state == EventChannelState.ABORTED:
                    completed = True
                    return
        finally:
            with self._consumer_lock:
                if self._consumer_owner is EventChannelConsumerOwner.DRAIN:
                    self._consumer_owner = EventChannelConsumerOwner.RELEASED
                if completed:
                    self._drain_completed = True

    def _claim_consumer(self, owner: EventChannelConsumerOwner) -> None:
        with self._consumer_lock:
            if self._state == EventChannelState.ABORTED:
                self._consumer_owner = EventChannelConsumerOwner.ABORTED
                raise RuntimeError("RuntimeEventChannel is aborted")
            if self._drain_handoff_pending:
                raise RuntimeError(
                    "RuntimeEventChannel drain handoff is pending"
                )
            if self._consumer_owner is not EventChannelConsumerOwner.RELEASED:
                raise RuntimeError("RuntimeEventChannel 只支持一个 Consumer")
            if self._transport_completed or self._drain_completed:
                raise RuntimeError(
                    "RuntimeEventChannel consumer lifecycle is complete"
                )
            self._consumer_owner = owner

    def _release_transport(self, *, completed: bool) -> None:
        with self._consumer_lock:
            if (
                self._consumer_owner
                is EventChannelConsumerOwner.TRANSPORT
            ):
                self._consumer_owner = EventChannelConsumerOwner.RELEASED
            if completed:
                self._transport_completed = True

    def _ensure_open(self) -> None:
        if self._state != EventChannelState.OPEN:
            raise EventChannelClosedError(
                f"Event Channel 已处于 {self._state.value}，不能继续 publish"
            )

    async def _execute_publication_fault(
        self,
        point: FaultPoint,
        event: RuntimeEvent,
        *,
        partially_persisted: bool,
    ) -> None:
        controller = self._fault_controller
        if controller is None:
            return
        try:
            await controller.execute_if_matched(
                FaultMatchContext(
                    fault_point=point,
                    component="event_channel",
                    run_id_digest=_safe_digest(self.run_id),
                    step_id=event.step_id,
                    event_type=event.event_type.value,
                    operation_kind=(
                        "CHANNEL_ENQUEUE"
                        if point is FaultPoint.EVENT_BEFORE_CHANNEL_ENQUEUE
                        else "JOURNAL_APPEND"
                    ),
                ),
                allowed_actions={
                    FaultAction.RAISE_TYPED_ERROR,
                    FaultAction.DELAY,
                    FaultAction.BLOCK_UNTIL_RELEASED,
                },
            )
        except InjectedFaultError as exc:
            stage = {
                FaultPoint.EVENT_AFTER_JOURNAL_APPEND: (
                    EventPublicationStage.AFTER_JOURNAL_APPEND
                ),
                FaultPoint.EVENT_BEFORE_CHANNEL_ENQUEUE: (
                    EventPublicationStage.BEFORE_CHANNEL_ENQUEUE
                ),
            }.get(point, EventPublicationStage.BEFORE_JOURNAL_APPEND)
            raise EventPublicationError(
                evidence=EventPublicationEvidence(
                    event_id=event.event_id,
                    sequence=event.sequence,
                    event_type=event.event_type.value,
                    publication_stage=stage,
                    partially_persisted=partially_persisted,
                ),
                fault_point=point,
                fault_code=exc.code,
            ) from None

    async def _before_receive(self) -> None:
        await self._execute_channel_fault(
            FaultPoint.CHANNEL_BEFORE_RECEIVE,
            operation_kind="CHANNEL_RECEIVE",
        )

    async def _execute_channel_fault(
        self,
        point: FaultPoint,
        *,
        operation_kind: str,
    ) -> None:
        controller = self._fault_controller
        if controller is None or not controller.enabled:
            return
        fault_task = asyncio.create_task(
            controller.execute_if_matched(
                FaultMatchContext(
                    fault_point=point,
                    component="event_channel",
                    run_id_digest=_safe_digest(self.run_id),
                    operation_kind=operation_kind,
                ),
                allowed_actions={
                    FaultAction.RAISE_TYPED_ERROR,
                    FaultAction.DELAY,
                    FaultAction.BLOCK_UNTIL_RELEASED,
                },
            )
        )
        abort_task = asyncio.create_task(self._abort_event.wait())
        cancellation_task: asyncio.Task[None] | None = None
        if self._cancellation_token is not None:
            cancellation_task = asyncio.create_task(
                self._cancellation_token.wait_cancelled()
            )
        waiters = {fault_task, abort_task}
        if cancellation_task is not None:
            waiters.add(cancellation_task)
        try:
            done, _ = await asyncio.wait(
                waiters, return_when=asyncio.FIRST_COMPLETED
            )
            if fault_task in done:
                await fault_task
                return
            fault_task.cancel()
            await asyncio.gather(fault_task, return_exceptions=True)
            if abort_task in done:
                raise EventChannelClosedError("Event Channel 已 abort")
            assert self._cancellation_token is not None
            self._cancellation_token.raise_if_cancelled()
        finally:
            for task in waiters:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

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


def _safe_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
