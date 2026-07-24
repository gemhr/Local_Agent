import asyncio
import unittest

from core.runtime import (
    EventChannelClosedError,
    EventEmitterSyncError,
    OutputDeltaPayload,
    RunCompletedPayload,
    RunEventEmitter,
    RunStartedPayload,
    RuntimeEventChannel,
    RuntimeEventDraft,
    RuntimeEventType,
    StepCompletedPayload,
    StepStartedPayload,
)


class RuntimeEventSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def test_envelope_schema_identity_utc_and_safe_serialization(self):
        channel = RuntimeEventChannel(4, run_id="run-a")
        event = await channel.publish(
            RuntimeEventDraft(
                run_id="run-a",
                trace_id="trace-a",
                event_type=RuntimeEventType.RUN_STARTED,
                component="coordinator",
                payload=RunStartedPayload("RUNNING"),
            )
        )
        self.assertEqual(event.schema_version, 1)
        self.assertTrue(event.event_id)
        self.assertEqual(event.sequence, 1)
        self.assertEqual(event.emitted_at.utcoffset().total_seconds(), 0)
        safe = event.to_safe_dict()
        self.assertEqual(safe["run_id"], "run-a")
        self.assertEqual(safe["trace_id"], "trace-a")

    async def test_event_ids_are_unique(self):
        channel = RuntimeEventChannel(4, run_id="run-a")
        events = [
            await channel.publish(
                RuntimeEventDraft(
                    "run-a",
                    "trace-a",
                    RuntimeEventType.RUN_STARTED,
                    "coordinator",
                    RunStartedPayload("RUNNING"),
                )
            )
            for _ in range(2)
        ]
        self.assertNotEqual(events[0].event_id, events[1].event_id)

    async def test_payload_type_must_match_event_type(self):
        with self.assertRaises(TypeError):
            RuntimeEventDraft(
                "run-a",
                "trace-a",
                RuntimeEventType.RUN_STARTED,
                "coordinator",
                StepStartedPayload("RUNNING"),
            )

    async def test_output_delta_text_is_hidden_by_default(self):
        channel = RuntimeEventChannel(2, run_id="run-a")
        event = await channel.publish(
            RuntimeEventDraft(
                "run-a",
                "trace-a",
                RuntimeEventType.OUTPUT_DELTA,
                "executor",
                OutputDeltaPayload("private visible answer"),
                "step-a",
                1,
            )
        )
        safe = event.to_safe_dict()
        self.assertEqual(safe["payload"], {"text_length": 22})
        self.assertEqual(
            set(safe),
            {"run_id", "sequence", "event_type", "step_id", "payload"},
        )
        self.assertNotIn("private visible answer", str(safe))
        self.assertEqual(
            event.to_safe_dict(include_output=True)["payload"]["text"],
            "private visible answer",
        )


class RuntimeEventSequenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_global_sequence_is_unique_under_concurrency(self):
        channel = RuntimeEventChannel(32, run_id="run-a")
        emitter = RunEventEmitter(
            run_id="run-a", trace_id="trace-a", channel=channel
        )

        async def publish(index):
            return await emitter.emit(
                RuntimeEventType.RUN_STARTED,
                RunStartedPayload(f"RUNNING-{index}"),
                component="test",
            )

        events = await asyncio.gather(*(publish(i) for i in range(20)))
        self.assertEqual(
            sorted(event.sequence for event in events), list(range(1, 21))
        )

    async def test_runs_have_independent_sequences(self):
        channel_a = RuntimeEventChannel(2, run_id="run-a")
        channel_b = RuntimeEventChannel(2, run_id="run-b")
        event_a = await channel_a.publish(
            RuntimeEventDraft(
                "run-a",
                "trace-a",
                RuntimeEventType.RUN_STARTED,
                "test",
                RunStartedPayload("RUNNING"),
            )
        )
        event_b = await channel_b.publish(
            RuntimeEventDraft(
                "run-b",
                "trace-b",
                RuntimeEventType.RUN_STARTED,
                "test",
                RunStartedPayload("RUNNING"),
            )
        )
        self.assertEqual((event_a.sequence, event_b.sequence), (1, 1))

    async def test_step_sequence_and_interleaving(self):
        channel = RuntimeEventChannel(16, run_id="run-a")
        emitter = RunEventEmitter(
            run_id="run-a", trace_id="trace-a", channel=channel
        )
        step_a = emitter.for_step("a")
        step_b = emitter.for_step("b")
        events = [
            await step_a.emit(
                RuntimeEventType.STEP_STARTED,
                StepStartedPayload("RUNNING"),
                component="test",
            ),
            await step_b.emit(
                RuntimeEventType.STEP_STARTED,
                StepStartedPayload("RUNNING"),
                component="test",
            ),
            await step_a.emit(
                RuntimeEventType.STEP_COMPLETED,
                StepCompletedPayload("SUCCEEDED"),
                component="test",
                close=True,
            ),
            await step_b.emit(
                RuntimeEventType.STEP_COMPLETED,
                StepCompletedPayload("SUCCEEDED"),
                component="test",
                close=True,
            ),
            await emitter.emit(
                RuntimeEventType.RUN_COMPLETED,
                RunCompletedPayload("SUCCEEDED", "COMPLETED"),
                component="test",
            ),
        ]
        self.assertEqual([event.sequence for event in events], [1, 2, 3, 4, 5])
        self.assertEqual(
            [event.step_sequence for event in events if event.step_id == "a"],
            [1, 2],
        )
        self.assertEqual(
            [event.step_sequence for event in events if event.step_id == "b"],
            [1, 2],
        )

    async def test_step_emitter_rejects_after_completed(self):
        channel = RuntimeEventChannel(4, run_id="run-a")
        emitter = RunEventEmitter(
            run_id="run-a", trace_id="trace-a", channel=channel
        ).for_step("step-a")
        await emitter.emit(
            RuntimeEventType.STEP_COMPLETED,
            StepCompletedPayload("SUCCEEDED"),
            component="test",
            close=True,
        )
        with self.assertRaises(RuntimeError):
            await emitter.emit(
                RuntimeEventType.OUTPUT_DELTA,
                OutputDeltaPayload("late"),
                component="test",
            )


class StepEmitterConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_cache_returns_one_identity(self):
        channel = RuntimeEventChannel(32, run_id="run-a")
        emitter = RunEventEmitter(
            run_id="run-a", trace_id="trace-a", channel=channel
        )
        gate = asyncio.Event()

        async def get_step():
            await gate.wait()
            return emitter.for_step("step-a")

        tasks = [asyncio.create_task(get_step()) for _ in range(32)]
        gate.set()
        references = await asyncio.gather(*tasks)
        self.assertEqual(len({id(item) for item in references}), 1)

    async def test_concurrent_publish_has_unique_monotonic_step_sequence(self):
        channel = RuntimeEventChannel(64, run_id="run-a")
        emitter = RunEventEmitter(
            run_id="run-a", trace_id="trace-a", channel=channel
        )
        step = emitter.for_step("step-a")
        events = await asyncio.gather(
            *(
                step.emit(
                    RuntimeEventType.OUTPUT_DELTA,
                    OutputDeltaPayload(str(index)),
                    component="test",
                )
                for index in range(32)
            )
        )
        self.assertEqual(
            sorted(event.step_sequence for event in events),
            list(range(1, 33)),
        )
        self.assertEqual(
            len({event.step_sequence for event in events}), len(events)
        )

    async def test_different_steps_have_independent_sequences(self):
        channel = RuntimeEventChannel(8, run_id="run-a")
        emitter = RunEventEmitter(
            run_id="run-a", trace_id="trace-a", channel=channel
        )
        event_a, event_b = await asyncio.gather(
            emitter.for_step("a").emit(
                RuntimeEventType.OUTPUT_DELTA,
                OutputDeltaPayload("a"),
                component="test",
            ),
            emitter.for_step("b").emit(
                RuntimeEventType.OUTPUT_DELTA,
                OutputDeltaPayload("b"),
                component="test",
            ),
        )
        self.assertEqual((event_a.step_sequence, event_b.step_sequence), (1, 1))

    async def test_concurrent_completed_succeeds_once_and_closes_all_references(self):
        channel = RuntimeEventChannel(16, run_id="run-a")
        parent = RunEventEmitter(
            run_id="run-a", trace_id="trace-a", channel=channel
        )
        references = [parent.for_step("step-a") for _ in range(12)]
        results = await asyncio.gather(
            *(
                reference.emit(
                    RuntimeEventType.STEP_COMPLETED,
                    StepCompletedPayload("SUCCEEDED"),
                    component="test",
                    close=True,
                )
                for reference in references
            ),
            return_exceptions=True,
        )
        succeeded = [item for item in results if not isinstance(item, BaseException)]
        failed = [item for item in results if isinstance(item, RuntimeError)]
        self.assertEqual(len(succeeded), 1)
        self.assertEqual(len(failed), 11)
        self.assertEqual(succeeded[0].step_sequence, 1)
        for reference in references:
            self.assertTrue(reference.is_closed)
            with self.assertRaises(RuntimeError):
                await reference.emit(
                    RuntimeEventType.OUTPUT_DELTA,
                    OutputDeltaPayload("late"),
                    component="test",
                )


class SynchronousEmitterBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_thread_sync_emit_succeeds(self):
        channel = RuntimeEventChannel(2, run_id="run-a")
        emitter = RunEventEmitter(
            run_id="run-a", trace_id="trace-a", channel=channel
        )
        event = await asyncio.to_thread(
            emitter.emit_from_worker,
            RuntimeEventType.RUN_STARTED,
            RunStartedPayload("RUNNING"),
            component="worker",
        )
        self.assertEqual(event.sequence, 1)

    async def test_owner_loop_sync_emit_fails_fast_with_safe_error(self):
        channel = RuntimeEventChannel(2, run_id="run-a")
        emitter = RunEventEmitter(
            run_id="run-a", trace_id="trace-a", channel=channel
        )
        with self.assertRaises(EventEmitterSyncError) as raised:
            emitter.emit_from_worker(
                RuntimeEventType.RUN_STARTED,
                RunStartedPayload("business正文"),
                component="owner",
            )
        self.assertEqual(
            raised.exception.error_code, "EMITTER_OWNER_LOOP_SYNC_CALL"
        )
        self.assertNotIn("business正文", str(raised.exception))

    async def test_async_emit_on_owner_loop_remains_supported(self):
        channel = RuntimeEventChannel(2, run_id="run-a")
        emitter = RunEventEmitter(
            run_id="run-a", trace_id="trace-a", channel=channel
        )
        event = await emitter.emit(
            RuntimeEventType.RUN_STARTED,
            RunStartedPayload("RUNNING"),
            component="owner",
        )
        self.assertEqual(event.sequence, 1)

    async def test_abort_unblocks_worker_waiting_on_backpressure(self):
        channel = RuntimeEventChannel(1, run_id="run-a")
        emitter = RunEventEmitter(
            run_id="run-a", trace_id="trace-a", channel=channel
        )
        await emitter.emit(
            RuntimeEventType.RUN_STARTED,
            RunStartedPayload("RUNNING"),
            component="owner",
        )
        worker = asyncio.create_task(
            asyncio.to_thread(
                emitter.emit_from_worker,
                RuntimeEventType.RUN_STARTED,
                RunStartedPayload("BLOCKED"),
                component="worker",
            )
        )
        await asyncio.sleep(0)
        self.assertFalse(worker.done())
        await channel.abort()
        with self.assertRaises(EventChannelClosedError):
            await asyncio.wait_for(worker, 0.2)

    async def test_closed_event_loop_returns_safe_error(self):
        closed_loop = asyncio.new_event_loop()
        channel = RuntimeEventChannel(1, run_id="run-a")
        emitter = RunEventEmitter(
            run_id="run-a",
            trace_id="trace-a",
            channel=channel,
            loop=closed_loop,
        )
        closed_loop.close()
        with self.assertRaises(EventEmitterSyncError) as raised:
            await asyncio.to_thread(
                emitter.emit_from_worker,
                RuntimeEventType.RUN_STARTED,
                RunStartedPayload("business正文"),
                component="worker",
            )
        self.assertEqual(
            raised.exception.error_code, "EMITTER_EVENT_LOOP_UNAVAILABLE"
        )
        self.assertNotIn("business正文", str(raised.exception))
