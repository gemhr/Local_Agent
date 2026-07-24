import asyncio
import tempfile
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path

from core.agent_router import AgentRouter
from core.chat_service import ChatService
from core.memory_manager import MemoryManager
from core.runtime import (
    BudgetLedger,
    CancellationReason,
    GeneratorModelAdapter,
    ModelAdapterInvocationError,
    ModelAdapterResolver,
    ModelAdapterResponse,
    ModelCircuitBreakerRegistry,
    ModelCircuitBreakerConfig,
    ModelCostProfile,
    ModelFailureCategory,
    ModelInvocationRouter,
    ModelProfile,
    ModelProfileId,
    ModelRoutingCandidate,
    ModelRoutingDecision,
    OutputDeltaPayload,
    RunCompletedPayload,
    RunBudget,
    RunEventEmitter,
    RunStartedPayload,
    RunStatus,
    RuntimeEventChannel,
    RuntimeEventType,
    RoutingAdjustment,
    StepCompletedPayload,
    StepStartedPayload,
    process_run_registry,
    create_run_context,
)


class FakeModel:
    def __init__(self, output="coordinated answer", error=None):
        self.output = output
        self.error = error
        self.calls = 0

    def generate(self, messages, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        yield self.output


class RuntimeEventIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def make_service(self, directory, model, *, capacity=32):
        memory = MemoryManager(str(Path(directory) / "memory.db"))
        router = AgentRouter(
            llm_engine=model,
            memory_manager=memory,
            orchestration_enabled=False,
        )
        return ChatService(router, event_channel_capacity=capacity)

    async def test_real_coordinated_path_has_complete_order_and_output_delta(self):
        with tempfile.TemporaryDirectory() as directory:
            model = FakeModel()
            service = self.make_service(directory, model)
            events = [
                event
                async for event in service.stream_coordinated_agent_events(
                    "core_router", "question", persist=False
                )
            ]
        types = [event.event_type for event in events]
        self.assertEqual(
            types,
            [
                RuntimeEventType.RUN_STARTED,
                RuntimeEventType.STEP_STARTED,
                RuntimeEventType.MODEL_STARTED,
                RuntimeEventType.MODEL_COMPLETED,
                RuntimeEventType.OUTPUT_DELTA,
                RuntimeEventType.STEP_COMPLETED,
                RuntimeEventType.RUN_COMPLETED,
            ],
        )
        self.assertEqual(
            [event.sequence for event in events],
            list(range(1, len(events) + 1)),
        )
        output = next(
            event for event in events if event.event_type == RuntimeEventType.OUTPUT_DELTA
        )
        self.assertEqual(output.payload, OutputDeltaPayload("coordinated answer"))
        self.assertNotIn("[[ORCH]]", output.payload.text)
        self.assertEqual(model.calls, 1)

    async def test_compatibility_method_reads_output_from_event_channel(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(directory, FakeModel("one complete answer"))
            output, result = await service.run_coordinated_agent(
                "core_router", "question", persist=False
            )
        self.assertEqual(output, "one complete answer")
        self.assertEqual(result.status, RunStatus.SUCCEEDED)

    async def test_real_text_adapter_path_keeps_output_plain(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(directory, FakeModel("plain answer"))
            chunks = [
                chunk
                async for chunk in service.stream_coordinated_agent_text(
                    "core_router", "question", persist=False
                )
            ]
        self.assertTrue(chunks[0].startswith("[[ORCH]]"))
        self.assertIn("plain answer", chunks)
        self.assertFalse(
            next(chunk for chunk in chunks if chunk == "plain answer").startswith(
                "[[ORCH]]"
            )
        )

    async def test_failure_emits_error_then_exactly_one_run_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(
                directory, FakeModel(error=RuntimeError("provider secret"))
            )
            events = [
                event
                async for event in service.stream_coordinated_agent_events(
                    "core_router", "question", persist=False
                )
            ]
        types = [event.event_type for event in events]
        self.assertIn(RuntimeEventType.ERROR, types)
        self.assertEqual(types[-1], RuntimeEventType.RUN_COMPLETED)
        self.assertEqual(types.count(RuntimeEventType.RUN_COMPLETED), 1)
        safe = str([event.to_safe_dict() for event in events])
        self.assertNotIn("provider secret", safe)

    async def test_retry_and_fallback_attempt_metadata(self):
        def profile(profile_id, remote):
            return ModelProfile(
                profile_id,
                8192,
                64,
                True,
                True,
                True,
                True,
                2 if remote else 1,
                2 if remote else 1,
                ModelCostProfile(profile_id, remote, 1, 1, 1, 1),
                remote,
                f"breaker:{profile_id.value}",
            )

        local = profile(ModelProfileId.LOCAL_FAST, False)
        remote = profile(ModelProfileId.REMOTE_ADVANCED, True)
        decision = ModelRoutingDecision(
            ModelProfileId.REMOTE_ADVANCED,
            ModelProfileId.LOCAL_FAST,
            (
                ModelRoutingCandidate(
                    local,
                    local.effective_breaker_key,
                    RoutingAdjustment.NONE,
                    "SAFE",
                ),
                ModelRoutingCandidate(
                    remote,
                    remote.effective_breaker_key,
                    RoutingAdjustment.ESCALATE_TO_REMOTE,
                    "SAFE",
                ),
            ),
            512,
            False,
        )

        class Adapter:
            def __init__(self, outcomes):
                self.outcomes = list(outcomes)

            def invoke(self, messages, *, max_tokens):
                outcome = self.outcomes.pop(0)
                if isinstance(outcome, BaseException):
                    raise outcome
                return ModelAdapterResponse(outcome)

        failures = [
            ModelAdapterInvocationError(
                ModelFailureCategory.TRANSIENT_PROVIDER_FAILURE
            )
            for _ in range(3)
        ]
        context, _source = create_run_context(entry_agent_id="agent")
        ledger = BudgetLedger(
            RunBudget(), deadline_remaining=context.remaining_seconds
        )
        context.attach_budget_ledger(ledger)
        channel = RuntimeEventChannel(16, run_id=context.run_id)
        emitter = RunEventEmitter(
            run_id=context.run_id,
            trace_id=context.trace_id,
            channel=channel,
        )
        await asyncio.to_thread(
            ModelInvocationRouter().invoke,
            run_context=context,
            budget_ledger=ledger,
            routing_decision=decision,
            messages=({"role": "user", "content": "not serialized"},),
            adapter_resolver=ModelAdapterResolver(
                {
                    ModelProfileId.LOCAL_FAST: Adapter(failures),
                    ModelProfileId.REMOTE_ADVANCED: Adapter(["fallback"]),
                }
            ),
            circuit_breaker_registry=ModelCircuitBreakerRegistry(),
            token_estimate=10,
            max_tokens=20,
            event_emitter=emitter.for_step("answer"),
        )
        await channel.close()
        events = [event async for event in channel]
        started = [
            event.payload
            for event in events
            if event.event_type == RuntimeEventType.MODEL_STARTED
        ]
        self.assertEqual(
            [(item.candidate_index, item.retry_index) for item in started],
            [(0, 0), (0, 1), (0, 2), (1, 0)],
        )
        self.assertNotIn("not serialized", str(events))

    async def test_consumer_close_aborts_bounded_channel_and_cleans_registry(self):
        run_id = uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(directory, FakeModel(), capacity=1)
            stream = service.stream_coordinated_agent_events(
                "core_router", "question", run_id=run_id, persist=False
            )
            first = await anext(stream)
            self.assertEqual(first.event_type, RuntimeEventType.RUN_STARTED)
            self.assertIsNotNone(process_run_registry.get(run_id))
            await stream.aclose()
        self.assertIsNone(process_run_registry.get(run_id))

    async def test_user_cancel_first_wins_over_disconnect_cleanup(self):
        run_id = uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(directory, FakeModel(), capacity=1)
            stream = service.stream_coordinated_agent_events(
                "core_router", "question", run_id=run_id, persist=False
            )
            await anext(stream)
            handle = process_run_registry.get(run_id)
            self.assertIsNotNone(handle)
            handle.cancellation_source.cancel(CancellationReason.USER_CANCELLED)
            await stream.aclose()
            self.assertEqual(
                handle.cancellation_source.token.reason,
                CancellationReason.USER_CANCELLED,
            )

    async def test_user_cancellation_emits_terminal_facts_when_consumer_remains(self):
        run_id = uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(directory, FakeModel(), capacity=1)
            stream = service.stream_coordinated_agent_events(
                "core_router", "question", run_id=run_id, persist=False
            )
            events = [await anext(stream)]
            process_run_registry.cancel(run_id, CancellationReason.USER_CANCELLED)
            events.extend([event async for event in stream])
        types = [event.event_type for event in events]
        self.assertIn(RuntimeEventType.CANCELLATION, types)
        self.assertEqual(types[-1], RuntimeEventType.RUN_COMPLETED)
        self.assertEqual(events[-1].payload.status, RunStatus.CANCELLED.value)


class ModelStartedTimingTests(unittest.IsolatedAsyncioTestCase):
    def make_profile_and_decision(self):
        profile = ModelProfile(
            ModelProfileId.LOCAL_FAST,
            8192,
            64,
            True,
            True,
            True,
            True,
            1,
            1,
            ModelCostProfile(ModelProfileId.LOCAL_FAST, False, 1, 1, 1, 1),
            False,
            "breaker:local",
        )
        decision = ModelRoutingDecision(
            ModelProfileId.LOCAL_FAST,
            ModelProfileId.LOCAL_FAST,
            (
                ModelRoutingCandidate(
                    profile,
                    profile.effective_breaker_key,
                    RoutingAdjustment.NONE,
                    "SAFE",
                ),
            ),
            512,
            False,
        )
        return profile, decision

    async def invoke(
        self,
        adapter_builder,
        *,
        budget=None,
        registry=None,
        resolver_override=None,
    ):
        profile, decision = self.make_profile_and_decision()
        context, _source = create_run_context(entry_agent_id="agent")
        ledger = BudgetLedger(
            budget or RunBudget(),
            deadline_remaining=context.remaining_seconds,
        )
        context.attach_budget_ledger(ledger)
        channel = RuntimeEventChannel(16, run_id=context.run_id)
        emitter = RunEventEmitter(
            run_id=context.run_id,
            trace_id=context.trace_id,
            channel=channel,
        )
        adapter = adapter_builder(channel) if adapter_builder is not None else None
        resolver = resolver_override or ModelAdapterResolver(
            {profile.profile_id: adapter}
        )
        error = None
        result = None
        try:
            result = await asyncio.to_thread(
                ModelInvocationRouter().invoke,
                run_context=context,
                budget_ledger=ledger,
                routing_decision=decision,
                messages=({"role": "user", "content": "redacted"},),
                adapter_resolver=resolver,
                circuit_breaker_registry=registry
                or ModelCircuitBreakerRegistry(),
                token_estimate=10,
                max_tokens=20,
                event_emitter=emitter.for_step("answer"),
            )
        except BaseException as exc:
            error = exc
        await channel.close()
        events = [event async for event in channel]
        return result, error, events, adapter, profile

    async def test_generator_adapter_has_one_pair_and_started_before_provider(self):
        class Engine:
            def __init__(self, channel):
                self.channel = channel
                self.started_visible_before_generate = False

            def generate(self, messages, **kwargs):
                self.started_visible_before_generate = (
                    self.channel.buffered_count == 1
                )
                yield "ok"

        engines = []

        def build(channel):
            engine = Engine(channel)
            engines.append(engine)
            return GeneratorModelAdapter(engine)

        result, error, events, _adapter, _profile = await self.invoke(build)
        self.assertIsNone(error)
        self.assertEqual(result.output, "ok")
        self.assertTrue(engines[0].started_visible_before_generate)
        self.assertEqual(
            [event.event_type for event in events],
            [RuntimeEventType.MODEL_STARTED, RuntimeEventType.MODEL_COMPLETED],
        )

    async def test_no_callback_adapter_started_is_published_before_invoke(self):
        class Adapter:
            def __init__(self, channel):
                self.channel = channel
                self.entered_at = None
                self.returned_at = None
                self.started_visible_on_entry = False

            def invoke(self, messages, *, max_tokens):
                self.entered_at = datetime.now(UTC)
                self.started_visible_on_entry = self.channel.buffered_count == 1
                self.returned_at = datetime.now(UTC)
                return ModelAdapterResponse("ok")

        result, error, events, adapter, _profile = await self.invoke(Adapter)
        self.assertIsNone(error)
        self.assertEqual(result.output, "ok")
        self.assertTrue(adapter.started_visible_on_entry)
        started, completed = events
        self.assertEqual(started.event_type, RuntimeEventType.MODEL_STARTED)
        self.assertEqual(completed.event_type, RuntimeEventType.MODEL_COMPLETED)
        self.assertLessEqual(started.emitted_at, adapter.entered_at)
        self.assertGreaterEqual(completed.emitted_at, adapter.returned_at)
        self.assertLess(started.emitted_at, completed.emitted_at)

    async def test_adapter_resolution_failure_has_no_started(self):
        profile, _decision = self.make_profile_and_decision()
        result, error, events, _adapter, _profile = await self.invoke(
            None,
            resolver_override=ModelAdapterResolver({}),
        )
        self.assertIsNone(result)
        self.assertIsNotNone(error)
        self.assertEqual(events, [])
        self.assertEqual(profile.profile_id, ModelProfileId.LOCAL_FAST)

    async def test_budget_failure_has_no_started(self):
        class Adapter:
            def __init__(self, channel):
                self.calls = 0

            def invoke(self, messages, *, max_tokens):
                self.calls += 1
                return ModelAdapterResponse("unused")

        result, error, events, adapter, _profile = await self.invoke(
            Adapter, budget=RunBudget(max_model_calls=0)
        )
        self.assertIsNone(result)
        self.assertIsNotNone(error)
        self.assertEqual(adapter.calls, 0)
        self.assertEqual(events, [])

    async def test_circuit_open_has_no_started(self):
        class Adapter:
            def __init__(self, channel):
                self.calls = 0

            def invoke(self, messages, *, max_tokens):
                self.calls += 1
                return ModelAdapterResponse("unused")

        profile, _decision = self.make_profile_and_decision()
        registry = ModelCircuitBreakerRegistry(
            ModelCircuitBreakerConfig(failure_threshold=1)
        )
        permit = registry.get(profile.effective_breaker_key).acquire_permission()
        permit.record_failure()
        result, error, events, adapter, _profile = await self.invoke(
            Adapter, registry=registry
        )
        self.assertIsNone(result)
        self.assertIsNotNone(error)
        self.assertEqual(adapter.calls, 0)
        self.assertEqual(events, [])

    async def test_adapter_exception_has_started_and_failed_completed(self):
        class Adapter:
            def __init__(self, channel):
                self.channel = channel
                self.started_visible_on_entry = False

            def invoke(self, messages, *, max_tokens):
                self.started_visible_on_entry = self.channel.buffered_count == 1
                raise ModelAdapterInvocationError(
                    ModelFailureCategory.SAFETY_BLOCKED,
                    provider_started=False,
                )

        result, error, events, adapter, _profile = await self.invoke(Adapter)
        self.assertIsNone(result)
        self.assertIsNotNone(error)
        self.assertTrue(adapter.started_visible_on_entry)
        self.assertEqual(
            [event.event_type for event in events],
            [RuntimeEventType.MODEL_STARTED, RuntimeEventType.MODEL_COMPLETED],
        )
        self.assertFalse(events[1].payload.succeeded)
        self.assertEqual(
            (
                events[0].payload.candidate_index,
                events[0].payload.retry_index,
            ),
            (
                events[1].payload.candidate_index,
                events[1].payload.retry_index,
            ),
        )
        self.assertLess(events[0].emitted_at, events[1].emitted_at)


class ParallelEventOrderingTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_steps_interleave_without_same_step_reordering(self):
        channel = RuntimeEventChannel(16, run_id="parallel-run")
        emitter = RunEventEmitter(
            run_id="parallel-run", trace_id="trace", channel=channel
        )
        a_started = asyncio.Event()
        b_started = asyncio.Event()

        async def step_a():
            step = emitter.for_step("a")
            await step.emit(
                RuntimeEventType.STEP_STARTED,
                StepStartedPayload("RUNNING"),
                component="fake",
            )
            a_started.set()
            await b_started.wait()
            await step.emit(
                RuntimeEventType.OUTPUT_DELTA,
                OutputDeltaPayload("a"),
                component="fake",
            )
            await step.emit(
                RuntimeEventType.STEP_COMPLETED,
                StepCompletedPayload("SUCCEEDED"),
                component="fake",
                close=True,
            )

        async def step_b():
            await a_started.wait()
            step = emitter.for_step("b")
            await step.emit(
                RuntimeEventType.STEP_STARTED,
                StepStartedPayload("RUNNING"),
                component="fake",
            )
            b_started.set()
            await asyncio.sleep(0)
            await step.emit(
                RuntimeEventType.OUTPUT_DELTA,
                OutputDeltaPayload("b"),
                component="fake",
            )
            await step.emit(
                RuntimeEventType.STEP_COMPLETED,
                StepCompletedPayload("SUCCEEDED"),
                component="fake",
                close=True,
            )

        await emitter.emit(
            RuntimeEventType.RUN_STARTED,
            RunStartedPayload("RUNNING"),
            component="fake",
        )
        await asyncio.gather(step_a(), step_b())
        await emitter.emit(
            RuntimeEventType.RUN_COMPLETED,
            RunCompletedPayload("SUCCEEDED", "COMPLETED"),
            component="fake",
        )
        await channel.close()
        events = [event async for event in channel]
        self.assertEqual(
            [event.sequence for event in events], list(range(1, len(events) + 1))
        )
        self.assertEqual(events[-1].event_type, RuntimeEventType.RUN_COMPLETED)
        for step_id in ("a", "b"):
            step_events = [event for event in events if event.step_id == step_id]
            self.assertEqual(
                [event.step_sequence for event in step_events], [1, 2, 3]
            )
            self.assertEqual(
                [event.event_type for event in step_events],
                [
                    RuntimeEventType.STEP_STARTED,
                    RuntimeEventType.OUTPUT_DELTA,
                    RuntimeEventType.STEP_COMPLETED,
                ],
            )
