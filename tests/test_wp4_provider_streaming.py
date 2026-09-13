import asyncio
from datetime import UTC, datetime
from functools import partial
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time

import pytest

from core.llm_engine import (
    Finish,
    RemoteLLMError,
    RemoteLLMEngine,
    TextDelta,
    ToolCallDelta,
)
from core.runtime import create_run_context
from core.runtime.cancellation import CancellationReason, RunCancelledError
from core.runtime.context import RunDeadlineExceededError
from core.runtime import (
    AgentState,
    AgentStateMachine,
    BudgetLedger,
    DeliveryStatus,
    GeneratorModelAdapter,
    InMemoryRunEventJournal,
    ModelAdapterInvocationError,
    ModelAdapterResolver,
    ModelAdapterResponse,
    ModelCircuitBreakerRegistry,
    ModelCostProfile,
    ModelFailureCategory,
    ModelInvocationChainError,
    ModelInvocationRouter,
    ModelProfile,
    ModelProfileId,
    ModelRoutingCandidate,
    ModelRoutingDecision,
    OutputGate,
    ResultContentType,
    RetryExecutor,
    RetryPolicy,
    RoutingAdjustment,
    RunEventEmitter,
    RunEventType,
    RunBudget,
    RunStateEvent,
    RuntimeEventChannel,
    RuntimeEventType,
    StepClaim,
    StepEventType,
    StepResult,
    StepResultStore,
    StepStateEvent,
    TaskCapabilityRequirements,
)
from core.runtime.planning import ExecutionKind, OutputPolicy, Plan, PlanSource, PlanStep
from tests._runtime_assembly_fixtures import FakeDispatcher


class _SSEHandler(BaseHTTPRequestHandler):
    server_version = "LocalAgentWP4/1"

    def do_POST(self):  # noqa: N802 - stdlib handler contract
        length = int(self.headers.get("Content-Length", "0"))
        self.server.request_body = self.rfile.read(length)
        self.server.request_started.set()
        try:
            if self.server.header_delay:
                time.sleep(self.server.header_delay)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for chunk, delay in self.server.chunks:
                wire = chunk if isinstance(chunk, bytes) else chunk.encode()
                self.wfile.write(f"{len(wire):X}\r\n".encode())
                self.wfile.write(wire)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                if delay:
                    time.sleep(delay)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self.server.client_disconnected.set()

    def log_message(self, *_args):
        return


class _SSEServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, chunks, *, header_delay=0):
        super().__init__(("127.0.0.1", 0), _SSEHandler)
        self.chunks = chunks
        self.header_delay = header_delay
        self.request_started = threading.Event()
        self.client_disconnected = threading.Event()
        self.request_body = b""


class _Server:
    def __init__(self, chunks, *, header_delay=0):
        self.httpd = _SSEServer(chunks, header_delay=header_delay)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.httpd.server_port}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(2)


def _event(payload):
    if payload == "[DONE]":
        value = payload
    else:
        value = json.dumps(payload, separators=(",", ":"))
    return f"data: {value}\n\n".encode()


@pytest.mark.asyncio
async def test_real_http_sse_fragmented_and_multiple_events_are_normalized():
    first = _event({"choices": [{"delta": {"content": "A"}}]})
    second = _event({"choices": [{"delta": {"content": "B"}}]})
    wire = first + second + _event("[DONE]")
    server = _Server([(wire[:7], 0), (wire[7:31], 0), (wire[31:], 0)])
    engine = RemoteLLMEngine(server.url, "test-model", timeout_seconds=2, trust_env=False)
    client = engine._client
    try:
        deltas = [
            delta
            async for delta in engine.agenerate(
                [{"role": "user", "content": "hello"}]
            )
        ]
        assert [delta.text for delta in deltas if isinstance(delta, TextDelta)] == ["A", "B"]
        assert isinstance(deltas[-1], Finish)
        assert json.loads(server.httpd.request_body)["stream"] is True
    finally:
        await engine.aclose()
        assert client.is_closed
        server.close()


@pytest.mark.asyncio
async def test_real_http_streaming_tool_arguments_are_assembled_before_return():
    first_tool_delta = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-1",
                            "function": {"name": "lookup", "arguments": '{"a":'},
                        }
                    ]
                }
            }
        ]
    }
    second_tool_delta = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {"index": 0, "function": {"arguments": "1}"}}
                    ]
                }
            }
        ]
    }
    events = [
        _event(first_tool_delta),
        _event(second_tool_delta),
        _event({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
        _event("[DONE]"),
    ]
    wire = b"".join(events)
    server = _Server([(wire[:13], 0), (wire[13:43], 0), (wire[43:], 0)])
    engine = RemoteLLMEngine(server.url, "deepseek-model", provider_kind="deepseek", timeout_seconds=2, trust_env=False)
    try:
        result = await engine.agenerate_native(
            [{"role": "user", "content": "lookup"}],
            tools=[{"type": "function", "function": {"name": "lookup"}}],
        )
        assert result.native_tool_call is not None
        assert result.native_tool_call.arguments_json == '{"a":1}'
    finally:
        await engine.aclose()
        server.close()


@pytest.mark.asyncio
async def test_cancel_closes_inflight_http_stream_and_stops_provider_iteration():
    chunks = [
        (_event({"choices": [{"delta": {"content": "A"}}]}), 0.8),
        (_event({"choices": [{"delta": {"content": "B"}}]}), 0.8),
        (_event({"choices": [{"delta": {"content": "C"}}]}), 0),
    ]
    server = _Server(chunks)
    engine = RemoteLLMEngine(server.url, "test-model", timeout_seconds=5, trust_env=False)
    context, source = create_run_context(entry_agent_id="test", timeout_seconds=5)
    accepted = []

    async def consume():
        async for delta in engine.agenerate(
            [{"role": "user", "content": "hello"}], run_context=context
        ):
            if isinstance(delta, TextDelta):
                accepted.append(delta.text)

    task = asyncio.create_task(consume())
    assert await asyncio.to_thread(server.httpd.request_started.wait, 1)
    await asyncio.sleep(0.1)
    source.cancel(CancellationReason.REQUEST_CANCELLED)
    with pytest.raises(RunCancelledError):
        await asyncio.wait_for(task, 1)
    assert accepted == ["A"]
    await engine.aclose()
    server.close()


@pytest.mark.asyncio
async def test_midstream_connection_end_is_typed_and_not_completed():
    server = _Server([(_event({"choices": [{"delta": {"content": "A"}}]}), 0)])
    engine = RemoteLLMEngine(server.url, "test-model", timeout_seconds=2, trust_env=False)
    try:
        with pytest.raises(RemoteLLMError) as raised:
            async for _delta in engine.agenerate(
                [{"role": "user", "content": "hello"}]
            ):
                pass
        assert raised.value.safe_error_code == "PROVIDER_STREAM_INCOMPLETE"
        assert raised.value.output_started is True
    finally:
        await engine.aclose()
        server.close()


@pytest.mark.asyncio
async def test_run_deadline_bounds_slow_http_stream():
    server = _Server([(_event({"choices": [{"delta": {}}]}), 1.0)])
    engine = RemoteLLMEngine(server.url, "test-model", timeout_seconds=30, trust_env=False)
    context, _source = create_run_context(entry_agent_id="test", timeout_seconds=0.2)
    started = time.monotonic()
    try:
        with pytest.raises((RunDeadlineExceededError, RuntimeError)):
            async for _delta in engine.agenerate(
                [{"role": "user", "content": "hello"}], run_context=context
            ):
                pass
        assert time.monotonic() - started < 1.0
    finally:
        await engine.aclose()
        server.close()


@pytest.mark.asyncio
async def test_cancel_before_response_headers_interrupts_http_request():
    server = _Server([(_event("[DONE]"), 0)], header_delay=1.0)
    engine = RemoteLLMEngine(server.url, "test-model", timeout_seconds=5, trust_env=False)
    context, source = create_run_context(entry_agent_id="test", timeout_seconds=5)

    async def consume():
        return [
            delta
            async for delta in engine.agenerate(
                [{"role": "user", "content": "hello"}], run_context=context
            )
        ]

    task = asyncio.create_task(consume())
    try:
        assert await asyncio.to_thread(server.httpd.request_started.wait, 1)
        started = time.monotonic()
        source.cancel(CancellationReason.REQUEST_CANCELLED)
        with pytest.raises(RunCancelledError):
            await asyncio.wait_for(task, 0.5)
        assert time.monotonic() - started < 0.5
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await engine.aclose()
        server.close()


@pytest.mark.asyncio
async def test_total_invocation_cap_is_not_renewed_per_stream_read():
    empty = _event({"choices": [{"delta": {}}]})
    server = _Server([(empty, 0.15), (empty, 0.15), (_event("[DONE]"), 0)])
    engine = RemoteLLMEngine(server.url, "test-model", timeout_seconds=0.2, trust_env=False)
    started = time.monotonic()
    try:
        with pytest.raises(RemoteLLMError) as raised:
            async for _delta in engine.agenerate(
                [{"role": "user", "content": "hello"}]
            ):
                pass
        assert raised.value.safe_error_code == "PROVIDER_TIMEOUT"
        assert raised.value.model_failure_category == "PROVIDER_TIMEOUT"
        assert raised.value.deadline_exceeded is False
        assert time.monotonic() - started < 0.5
    finally:
        await engine.aclose()
        server.close()


@pytest.mark.asyncio
async def test_malformed_json_after_output_preserves_output_started():
    wire = _event({"choices": [{"delta": {"content": "A"}}]}) + b"data: {bad}\n\n"
    server = _Server([(wire, 0)])
    engine = RemoteLLMEngine(server.url, "test-model", timeout_seconds=2, trust_env=False)
    accepted = []
    try:
        with pytest.raises(RemoteLLMError) as raised:
            async for delta in engine.agenerate(
                [{"role": "user", "content": "hello"}]
            ):
                if isinstance(delta, TextDelta):
                    accepted.append(delta.text)
        assert accepted == ["A"]
        assert raised.value.safe_error_code == "PROVIDER_PROTOCOL_ERROR"
        assert raised.value.output_started is True
    finally:
        await engine.aclose()
        server.close()


@pytest.mark.asyncio
async def test_invalid_utf8_is_typed_provider_protocol_error():
    wire = _event({"choices": [{"delta": {"content": "A"}}]}) + b"data: \xff\n\n"
    server = _Server([(wire, 0)])
    engine = RemoteLLMEngine(server.url, "test-model", timeout_seconds=2, trust_env=False)
    accepted = []
    try:
        with pytest.raises(RemoteLLMError) as raised:
            async for delta in engine.agenerate(
                [{"role": "user", "content": "hello"}]
            ):
                if isinstance(delta, TextDelta):
                    accepted.append(delta.text)
        assert accepted == ["A"]
        assert raised.value.safe_error_code == "PROVIDER_PROTOCOL_ERROR"
        assert raised.value.output_started is True
    finally:
        await engine.aclose()
        server.close()


@pytest.mark.asyncio
async def test_finish_delta_is_emitted_exactly_once():
    wire = b"".join(
        [
            _event({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
            _event("[DONE]"),
        ]
    )
    server = _Server([(wire, 0)])
    engine = RemoteLLMEngine(server.url, "test-model", timeout_seconds=2, trust_env=False)
    try:
        deltas = [
            delta
            async for delta in engine.agenerate(
                [{"role": "user", "content": "hello"}]
            )
        ]
        finishes = [delta for delta in deltas if isinstance(delta, Finish)]
        assert len(finishes) == 1
        assert finishes[0].reason == "stop"
    finally:
        await engine.aclose()
        server.close()


def _tool_event(index, *, call_id=None, name=None, arguments=None):
    call = {"index": index, "function": {}}
    if call_id is not None:
        call["id"] = call_id
    if name is not None:
        call["function"]["name"] = name
    if arguments is not None:
        call["function"]["arguments"] = arguments
    return _event({"choices": [{"delta": {"tool_calls": [call]}}]})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("events", "expected_code"),
    [
        (
            [
                _tool_event(0, call_id="call-1", name="lookup", arguments="{"),
                _tool_event(0, call_id="call-2", arguments="}"),
                _event("[DONE]"),
            ],
            "REMOTE_NATIVE_TOOL_CALL_INVALID",
        ),
        (
            [
                _tool_event(0, call_id="call-1", name="lookup", arguments="{}"),
                _tool_event(1, call_id="call-2", name="lookup", arguments="{}"),
                _event("[DONE]"),
            ],
            "REMOTE_NATIVE_TOOL_CALL_COUNT_INVALID",
        ),
        (
            [_tool_event(0, name="lookup", arguments="{}"), _event("[DONE]")],
            "REMOTE_NATIVE_TOOL_CALL_INVALID",
        ),
        (
            [
                _tool_event(0, call_id="call-1", name="lookup", arguments="{"),
                _event("[DONE]"),
            ],
            "REMOTE_NATIVE_TOOL_CALL_INVALID",
        ),
    ],
)
async def test_streaming_tool_call_invalid_final_states_fail_closed(events, expected_code):
    server = _Server([(b"".join(events), 0)])
    engine = RemoteLLMEngine(server.url, "deepseek-model", provider_kind="deepseek", timeout_seconds=2, trust_env=False)
    try:
        with pytest.raises(RemoteLLMError) as raised:
            await engine.agenerate_native(
                [{"role": "user", "content": "lookup"}],
                tools=[{"type": "function", "function": {"name": "lookup"}}],
            )
        assert raised.value.safe_error_code == expected_code
        assert raised.value.output_started is True
    finally:
        await engine.aclose()
        server.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [None, "0", True, -1])
async def test_streaming_tool_call_invalid_index_is_protocol_error(index):
    wire = _tool_event(index, call_id="call-1", name="lookup", arguments="{}")
    server = _Server([(wire, 0)])
    engine = RemoteLLMEngine(server.url, "deepseek-model", provider_kind="deepseek", timeout_seconds=2, trust_env=False)
    try:
        with pytest.raises(RemoteLLMError) as raised:
            await engine.agenerate_native(
                [{"role": "user", "content": "lookup"}],
                tools=[{"type": "function", "function": {"name": "lookup"}}],
            )
        assert raised.value.safe_error_code == "PROVIDER_PROTOCOL_ERROR"
    finally:
        await engine.aclose()
        server.close()


@pytest.mark.asyncio
async def test_legacy_session_seam_does_not_allocate_async_client():
    class LegacySession:
        def __init__(self):
            self.close_calls = 0

        def mount(self, *_args):
            return None

        def close(self):
            self.close_calls += 1

    session = LegacySession()
    engine = RemoteLLMEngine("https://example.test", "test-model", session=session)
    assert engine._client is None

    await engine.aclose()

    assert session.close_calls == 1


def _profile(profile_id, remote=False):
    return ModelProfile(
        profile_id,
        8192,
        128,
        True,
        True,
        True,
        True,
        2 if remote else 1,
        2 if remote else 1,
        ModelCostProfile(profile_id, remote, 1, 1, 1, 10),
        remote,
        f"wp4:{profile_id.value}",
    )


def _routing(*profiles):
    initial = profiles[0]
    return ModelRoutingDecision(
        None,
        initial.profile_id,
        tuple(
            ModelRoutingCandidate(
                profile,
                profile.effective_breaker_key,
                (
                    RoutingAdjustment.ESCALATE_TO_REMOTE
                    if not initial.effective_is_remote and profile.effective_is_remote
                    else RoutingAdjustment.NONE
                ),
                "WP4_TEST",
            )
            for profile in profiles
        ),
        1,
    )


class _AsyncAdapter:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def supports_native_tool_calling(self):
        return False

    async def ainvoke(self, _messages, *, max_tokens, on_delta=None, **_kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            if bool(getattr(outcome, "output_started", False)) and on_delta is not None:
                await on_delta("accepted")
            raise outcome
        if on_delta is not None:
            await on_delta(str(outcome))
        return ModelAdapterResponse(str(outcome))


class _AsyncStreamEngine:
    def __init__(self, *, text: str = "", fail_before: bool = False, fail_after: bool = False):
        self.text = text
        self.fail_before = fail_before
        self.fail_after = fail_after
        self.calls = 0

    def supports_native_tool_calling(self):
        return False

    async def agenerate(self, _messages, **_kwargs):
        self.calls += 1
        if self.fail_before:
            raise RemoteLLMError(
                "pre-output failure",
                model_failure_category="TRANSIENT_PROVIDER_FAILURE",
                safe_error_code="PROVIDER_CONNECTION_ERROR",
            )
        if self.text:
            yield TextDelta(self.text)
        if self.fail_after:
            raise RemoteLLMError(
                "mid-stream failure",
                model_failure_category="TRANSIENT_PROVIDER_FAILURE",
                safe_error_code="PROVIDER_CONNECTION_ERROR",
                output_started=True,
            )
        yield Finish("stop")


@pytest.mark.asyncio
async def test_async_router_retries_before_first_accepted_output():
    profile = _profile(ModelProfileId.LOCAL_FAST)
    adapter = _AsyncAdapter(
        [
            ModelAdapterInvocationError(
                ModelFailureCategory.TRANSIENT_PROVIDER_FAILURE,
                provider_started=True,
                provider_responded=False,
            ),
            "ok",
        ]
    )
    context, _source = create_run_context(entry_agent_id="test")
    ledger = BudgetLedger(RunBudget(), deadline_remaining=context.remaining_seconds)
    result = await ModelInvocationRouter(
        retry_executor=RetryExecutor(RetryPolicy(base_delay_seconds=0, max_delay_seconds=0))
    ).ainvoke(
        run_context=context,
        budget_ledger=ledger,
        routing_decision=_routing(profile),
        messages=(),
        adapter_resolver=ModelAdapterResolver({profile.profile_id: adapter}),
        circuit_breaker_registry=ModelCircuitBreakerRegistry(),
        token_estimate=1,
        max_tokens=1,
    )
    assert result.output == "ok"
    assert adapter.calls == 2


@pytest.mark.asyncio
async def test_async_router_allows_fallback_before_first_accepted_output():
    first = _profile(ModelProfileId.LOCAL_FAST)
    second = _profile(ModelProfileId.REMOTE_ADVANCED, remote=True)
    first_adapter = _AsyncAdapter(
        [
            ModelAdapterInvocationError(
                ModelFailureCategory.TRANSIENT_PROVIDER_FAILURE,
                provider_started=True,
                provider_responded=False,
            )
        ]
    )
    second_adapter = _AsyncAdapter(["fallback"])
    context, _source = create_run_context(entry_agent_id="test")
    ledger = BudgetLedger(RunBudget(), deadline_remaining=context.remaining_seconds)
    result = await ModelInvocationRouter(
        retry_executor=RetryExecutor(
            RetryPolicy(max_attempts=1, base_delay_seconds=0, max_delay_seconds=0)
        )
    ).ainvoke(
        run_context=context,
        budget_ledger=ledger,
        routing_decision=_routing(first, second),
        messages=(),
        adapter_resolver=ModelAdapterResolver(
            {first.profile_id: first_adapter, second.profile_id: second_adapter}
        ),
        circuit_breaker_registry=ModelCircuitBreakerRegistry(),
        token_estimate=1,
        max_tokens=1,
    )
    assert result.output == "fallback"
    assert (first_adapter.calls, second_adapter.calls) == (1, 1)


@pytest.mark.asyncio
async def test_async_router_fail_stops_after_accepted_output_without_fallback():
    first = _profile(ModelProfileId.LOCAL_FAST)
    second = _profile(ModelProfileId.REMOTE_ADVANCED, remote=True)
    first_adapter = _AsyncAdapter(
        [
            ModelAdapterInvocationError(
                ModelFailureCategory.TRANSIENT_PROVIDER_FAILURE,
                provider_started=True,
                provider_responded=False,
                output_started=True,
            )
        ]
    )
    second_adapter = _AsyncAdapter(["must-not-run"])
    context, _source = create_run_context(entry_agent_id="test")
    ledger = BudgetLedger(RunBudget(), deadline_remaining=context.remaining_seconds)
    with pytest.raises(ModelInvocationChainError):
        await ModelInvocationRouter(
            retry_executor=RetryExecutor(RetryPolicy(base_delay_seconds=0, max_delay_seconds=0))
        ).ainvoke(
            run_context=context,
            budget_ledger=ledger,
            routing_decision=_routing(first, second),
            messages=(),
            adapter_resolver=ModelAdapterResolver(
                {first.profile_id: first_adapter, second.profile_id: second_adapter}
            ),
            circuit_breaker_registry=ModelCircuitBreakerRegistry(),
            token_estimate=1,
            max_tokens=1,
        )
    assert first_adapter.calls == 1
    assert second_adapter.calls == 0


@pytest.mark.asyncio
async def test_production_sync_router_async_provider_falls_back_only_before_acceptance():
    first = _profile(ModelProfileId.LOCAL_FAST)
    second = _profile(ModelProfileId.REMOTE_ADVANCED, remote=True)
    first_engine = _AsyncStreamEngine(fail_before=True)
    second_engine = _AsyncStreamEngine(text="fallback")
    context, _source = create_run_context(entry_agent_id="test")
    ledger = BudgetLedger(RunBudget(), deadline_remaining=context.remaining_seconds)
    owner_loop = asyncio.get_running_loop()
    accepted = []

    async def accept(text):
        accepted.append(text)

    def submit(coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, owner_loop).result()

    result = await asyncio.to_thread(
        partial(
            ModelInvocationRouter(
                retry_executor=RetryExecutor(
                    RetryPolicy(max_attempts=1, base_delay_seconds=0, max_delay_seconds=0)
                )
            ).invoke,
            run_context=context,
            budget_ledger=ledger,
            routing_decision=_routing(first, second),
            messages=(),
            adapter_resolver=ModelAdapterResolver(
                {
                    first.profile_id: GeneratorModelAdapter(first_engine),
                    second.profile_id: GeneratorModelAdapter(second_engine),
                }
            ),
            circuit_breaker_registry=ModelCircuitBreakerRegistry(),
            token_estimate=1,
            max_tokens=1,
            async_submit=submit,
            on_output_delta=accept,
        )
    )
    assert result.output == "fallback"
    assert accepted == ["fallback"]
    assert (first_engine.calls, second_engine.calls) == (1, 1)


@pytest.mark.asyncio
async def test_production_sync_router_fail_stops_after_runtime_acceptance():
    first = _profile(ModelProfileId.LOCAL_FAST)
    second = _profile(ModelProfileId.REMOTE_ADVANCED, remote=True)
    first_engine = _AsyncStreamEngine(text="A", fail_after=True)
    second_engine = _AsyncStreamEngine(text="must-not-run")
    context, _source = create_run_context(entry_agent_id="test")
    ledger = BudgetLedger(RunBudget(), deadline_remaining=context.remaining_seconds)
    owner_loop = asyncio.get_running_loop()
    accepted = []

    async def accept(text):
        accepted.append(text)

    def submit(coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, owner_loop).result()

    with pytest.raises(ModelInvocationChainError):
        await asyncio.to_thread(
            partial(
                ModelInvocationRouter().invoke,
                run_context=context,
                budget_ledger=ledger,
                routing_decision=_routing(first, second),
                messages=(),
                adapter_resolver=ModelAdapterResolver(
                    {
                        first.profile_id: GeneratorModelAdapter(first_engine),
                        second.profile_id: GeneratorModelAdapter(second_engine),
                    }
                ),
                circuit_breaker_registry=ModelCircuitBreakerRegistry(),
                token_estimate=1,
                max_tokens=1,
                async_submit=submit,
                on_output_delta=accept,
            )
        )
    assert accepted == ["A"]
    assert first_engine.calls == 1
    assert second_engine.calls == 0


@pytest.mark.asyncio
async def test_production_async_provider_reaches_runtime_output_gate_incrementally():
    """真实 HTTP provider 的首个 delta 在请求完成前进入 bounded Runtime channel。"""
    chunks = [
        (_event({"choices": [{"delta": {"content": "A"}}]}), 0.4),
        (_event({"choices": [{"delta": {"content": "B"}}]}), 0),
        (_event({"choices": [{"delta": {}, "finish_reason": "stop"}]}), 0),
        (_event("[DONE]"), 0),
    ]
    server = _Server(chunks)
    engine = RemoteLLMEngine(
        server.url, "test-model", timeout_seconds=2, trust_env=False
    )
    context, source = create_run_context(entry_agent_id="test", timeout_seconds=2)
    ledger = BudgetLedger(RunBudget(), deadline_remaining=context.remaining_seconds)
    context.attach_budget_ledger(ledger)
    journal = InMemoryRunEventJournal()
    channel = RuntimeEventChannel(
        4,
        run_id=context.run_id,
        cancellation_token=source.token,
        journal=journal,
        observability_dispatcher=FakeDispatcher(),
    )
    emitter = RunEventEmitter(
        run_id=context.run_id,
        trace_id=context.trace_id,
        channel=channel,
    )
    capabilities = TaskCapabilityRequirements()
    plan = Plan(
        "wp4-runtime-stream",
        1,
        "stream",
        (
            PlanStep(
                "answer",
                "answer",
                "answer",
                (),
                "done",
                "core_router",
                capabilities,
                ExecutionKind.AGENT,
                OutputPolicy.FINAL_PASSTHROUGH,
            ),
        ),
        datetime.now(UTC),
        PlanSource.DETERMINISTIC,
    )
    claim = StepClaim(
        plan.plan_id,
        plan.version,
        "answer",
        datetime.now(UTC),
        capabilities,
        "core_router",
    )
    state = AgentState.for_run_context(context.run_id)
    machine = AgentStateMachine()
    machine.register_plan_step(state, step_id="answer", name="answer")
    machine.apply_run_event(state, RunStateEvent(RunEventType.STARTED))
    machine.apply_step_event(
        state,
        StepStateEvent(
            StepEventType.STARTED, "answer", occurred_at=datetime.now(UTC)
        ),
    )
    store = StepResultStore(plan, run_id=context.run_id)
    gate = OutputGate(
        plan=plan,
        store=store,
        event_emitter=emitter,
        state_getter=lambda: state,
        run_active=lambda: True,
    )
    profile = _profile(ModelProfileId.REMOTE_ADVANCED, remote=True)
    router = ModelInvocationRouter()
    invocation = partial(
        router.invoke,
        run_context=context,
        budget_ledger=ledger,
        routing_decision=_routing(profile),
        messages=({"role": "user", "content": "hello"},),
        adapter_resolver=ModelAdapterResolver(
            {profile.profile_id: GeneratorModelAdapter(engine)}
        ),
        circuit_breaker_registry=ModelCircuitBreakerRegistry(),
        token_estimate=1,
        max_tokens=8,
        event_emitter=emitter.for_step("answer"),
        async_submit=emitter.submit_coroutine_from_worker,
        on_output_delta=gate.stream_sink(claim),
    )
    task = asyncio.create_task(asyncio.to_thread(invocation))
    iterator = channel.__aiter__()
    output_events = []
    try:
        while not output_events:
            event = await asyncio.wait_for(anext(iterator), 1)
            if event.event_type is RuntimeEventType.OUTPUT_DELTA:
                output_events.append(event)
        assert output_events[0].payload.text == "A"
        assert not task.done()

        result = await asyncio.wait_for(task, 2)
        while len(output_events) < 2:
            event = await asyncio.wait_for(anext(iterator), 1)
            if event.event_type is RuntimeEventType.OUTPUT_DELTA:
                output_events.append(event)
        assert result.output == "AB"
        assert [event.payload.text for event in output_events] == ["A", "B"]

        machine.apply_step_event(
            state,
            StepStateEvent(
                StepEventType.SUCCEEDED,
                "answer",
                occurred_at=datetime.now(UTC),
            ),
        )
        step_result = StepResult(
            "answer", "core_router", ResultContentType.TEXT, result.output
        )
        store.write_prepared(step_result, expected_agent_id="core_router")
        store.mark_readable("answer", state)
        attempt = await gate.attempt_publish(claim=claim, result=step_result)
        assert attempt.delivery_status is DeliveryStatus.DELIVERED
        records = journal.read_after(context.run_id, 0, 100)
        assert sum(
            record.event_type == RuntimeEventType.OUTPUT_DELTA.value
            for record in records
        ) == 2
    finally:
        await iterator.aclose()
        await channel.abort()
        await engine.aclose()
        server.close()
