import json

import httpx
import pytest

from core.agent_router import AgentRouter
from core.llm_engine import RemoteLLMEngine
from core.runtime import BudgetLedger, GeneratorModelAdapter, RunBudget, create_run_context
from core.runtime.model_invocation import ModelAdapterInvocationError


def _capture_client(payload: dict | None = None, *, status_code: int = 200):
    captured = {}

    payload = payload or {"choices": [{"message": {"content": "ok"}}]}
    choice = (payload.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    if "tool_calls" in message:
        delta = {
            "tool_calls": [
                {**call, "index": index}
                for index, call in enumerate(message["tool_calls"])
            ]
        }
    else:
        delta = {"content": message.get("content", "")}
    stream_choice = {"delta": delta}
    if "finish_reason" in choice:
        stream_choice["finish_reason"] = choice["finish_reason"]
    wire = (
        f"data: {json.dumps({'choices': [stream_choice]}, separators=(',', ':'))}\n\n"
        "data: [DONE]\n\n"
    ).encode()

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            status_code,
            headers={"content-type": "text/event-stream"},
            content=wire,
            request=request,
        )

    return captured, httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)


def test_deepseek_thinking_enabled_is_explicit() -> None:
    captured, client = _capture_client()
    engine = RemoteLLMEngine(
        "https://api.deepseek.com",
        "deepseek-flash",
        enable_thinking=True,
        provider_kind="deepseek",
        client=client,
    )

    try:
        assert list(engine.generate([{"role": "user", "content": "hi"}])) == ["ok"]
        assert captured["json"]["thinking"] == {"type": "enabled"}
        assert captured["json"]["reasoning_effort"] == "high"
    finally:
        engine.close()


def test_deepseek_thinking_disabled_is_explicit() -> None:
    captured, client = _capture_client()
    engine = RemoteLLMEngine(
        "https://api.deepseek.com/v1",
        "deepseek-flash",
        enable_thinking=False,
        provider_kind="deepseek",
        client=client,
    )

    try:
        list(engine.generate([{"role": "user", "content": "hi"}]))
        assert captured["url"].endswith("/v1/chat/completions")
        assert captured["json"]["model"] == "deepseek-flash"
        assert captured["json"]["thinking"] == {"type": "disabled"}
        assert "reasoning_effort" not in captured["json"]
    finally:
        engine.close()


def test_deepseek_native_tool_call_sends_wire_and_normalizes() -> None:
    captured, client = _capture_client({"choices": [{"message": {"content": None, "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "get_system_status", "arguments": "{}"}}]}}]})
    engine = RemoteLLMEngine("https://api.deepseek.com", "deepseek-flash", provider_kind="deepseek", client=client)

    try:
        assert engine.supports_native_tool_calling() is True

        result = engine.generate_native(
            [{"role": "user", "content": "状态"}],
            tools=[{"type": "function", "function": {"name": "get_system_status", "parameters": {"type": "object"}}}],
        )

        assert captured["json"]["tool_choice"] == "auto"
        assert captured["json"]["tools"][0]["function"]["name"] == "get_system_status"
        assert result.native_tool_call.provider_tool_call_id == "call-1"
        assert result.native_tool_call.arguments_json == "{}"
        assert result.assistant_message["tool_calls"][0]["id"] == "call-1"
    finally:
        engine.close()


def test_non_native_local_adapter_preserves_plain_generation_without_tools() -> None:
    class LocalEngine:
        def generate(self, _messages, *, max_tokens):
            assert max_tokens == 16
            return iter(("legacy response",))

    adapter = GeneratorModelAdapter(LocalEngine())

    assert adapter.supports_native_tool_calling() is False
    assert adapter.invoke([{"role": "user", "content": "hi"}], max_tokens=16).output == "legacy response"


def test_permissive_fake_without_native_capability_cannot_silently_ignore_tools() -> None:
    class PermissiveFakeEngine:
        def __init__(self) -> None:
            self.calls = 0

        def generate(self, _messages, **_kwargs):
            self.calls += 1
            return iter(("must not be returned",))

    engine = PermissiveFakeEngine()
    adapter = GeneratorModelAdapter(engine)

    with pytest.raises(ModelAdapterInvocationError) as captured:
        adapter.invoke(
            [{"role": "user", "content": "hi"}],
            max_tokens=16,
            generation_options={"tools": [], "tool_choice": "auto"},
        )
    assert captured.value.safe_error_code == "NATIVE_TOOL_CALLING_UNSUPPORTED"
    assert engine.calls == 0


def test_deepseek_native_multiple_tool_calls_fail_closed() -> None:
    _captured, client = _capture_client({"choices": [{"message": {"tool_calls": [{"id": "one", "function": {"name": "a", "arguments": "{}"}}, {"id": "two", "function": {"name": "b", "arguments": "{}"}}]}}]})
    engine = RemoteLLMEngine("https://api.deepseek.com", "deepseek-flash", provider_kind="deepseek", client=client)

    try:
        with pytest.raises(RuntimeError) as captured:
            engine.generate_native([{"role": "user", "content": "x"}], tools=[])
        assert captured.value.safe_error_code == "REMOTE_NATIVE_TOOL_CALL_COUNT_INVALID"
    finally:
        engine.close()


def test_deepseek_parameters_are_not_sent_to_other_providers() -> None:
    captured, client = _capture_client()
    engine = RemoteLLMEngine(
        "https://example.test/v1", "Qwen3.5-27B", enable_thinking=False,
        client=client,
    )

    try:
        list(engine.generate([{"role": "user", "content": "hi"}]))
        assert "thinking" not in captured["json"]
        assert captured["json"]["chat_template_kwargs"] == {"enable_thinking": False}
    finally:
        engine.close()


class RecordingLLM:
    def __init__(self) -> None:
        self.calls = []

    def generate(self, messages, **kwargs):
        self.calls.append(kwargs)
        yield "keyword"


def test_knowledge_rewrite_uses_unified_model_contract_with_128_tokens() -> None:
    from tests.test_model_invocation_durable import _compiled_agent_registry

    llm = RecordingLLM()
    router = AgentRouter(llm_engine=llm, memory_manager=object(), agent_registry=_compiled_agent_registry())
    context, _source = create_run_context(entry_agent_id="knowledge_expert")
    context.attach_budget_ledger(BudgetLedger(RunBudget()))

    assert router._rewrite_knowledge_query("question", context, None) == "keyword"
    assert llm.calls[0]["max_tokens"] == 128
    assert llm.calls[0]["enable_thinking"] is False
    assert context.budget_ledger.snapshot().committed_usage.model_calls == 1


class FakeCloseable:
    def __init__(self, *, close_error: bool = False) -> None:
        self.close_calls = 0
        self.close_error = close_error

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error:
            raise RuntimeError("fake close failure")


def test_shutdown_close_errors_do_not_skip_other_engines() -> None:
    from server import _close_model_engines

    failing = FakeCloseable(close_error=True)
    healthy = FakeCloseable()

    errors = _close_model_engines({"first": failing, "second": healthy})

    assert errors == ("MODEL_ENGINE_CLOSE_FAILED",)
    assert failing.close_calls == 1
    assert healthy.close_calls == 1


@pytest.mark.parametrize(("usage", "counts", "reason"), [
    (None, (None, None, None), "USAGE_MISSING"),
    ({}, (None, None, None), "USAGE_EMPTY"),
    ([], (None, None, None), "USAGE_INVALID"),
    ({"prompt_tokens": 7, "total_tokens": 7}, (7, None, 7), "USAGE_FIELDS_MISSING"),
    ({"prompt_tokens": True, "completion_tokens": "3", "total_tokens": 4}, (None, None, 4), "USAGE_INVALID"),
    ({"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 6}, (2, 3, 6), "USAGE_TOTAL_MISMATCH"),
    ({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, (0, 0, 0), None),
])
def test_provider_usage_distinguishes_unknown_partial_and_zero(usage, counts, reason):
    from core.llm_engine import UsageDelta

    deltas = RemoteLLMEngine._normalize_sse_data(json.dumps({"usage": usage}))
    evidence = next((delta for delta in deltas if isinstance(delta, UsageDelta)), UsageDelta())
    assert (evidence.input_tokens, evidence.output_tokens, evidence.total_tokens) == counts
    assert evidence.reason == reason
    actual = evidence.actual_usage()
    if reason is None:
        assert (actual.input_tokens, actual.output_tokens, actual.total_tokens) == counts
    else:
        assert actual is None


@pytest.mark.asyncio
@pytest.mark.parametrize("tail", ["choices", "usage_only", "disconnect_after_usage", "disconnect_before_usage", "read_timeout", "eof_without_done"])
async def test_stream_usage_and_completion_are_independent(tail):
    from hashlib import sha256

    usage = {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12,
             "prompt_cache_hit_tokens": 4, "prompt_cache_miss_tokens": 3,
             "completion_tokens_details": {"reasoning_tokens": 2}}
    content = {"choices": [{"delta": {"content": "ok"}}], "usage": None}
    finish = {"choices": [{"delta": {}, "finish_reason": "stop"}]}
    usage_only = {"id": "response-123", "choices": [], "usage": usage}
    frames = [content, {**usage_only, **finish}] if tail == "choices" else [content, finish]
    if tail in {"usage_only", "disconnect_after_usage", "eof_without_done"}:
        frames.append(usage_only)
    if tail in {"choices", "usage_only"}:
        frames.append("[DONE]")

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for frame in frames:
                data = frame if frame == "[DONE]" else json.dumps(frame)
                yield ("data: " + data + "\n\n").encode()
            if tail.startswith("disconnect"):
                raise httpx.ReadError("connection cut")

    async def handle(request):
        if tail == "read_timeout":
            raise httpx.ReadTimeout("waiting for headers", request=request)
        return httpx.Response(200, stream=Stream(), request=request)

    messages = [{"role": "user", "content": "hi"}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        engine = RemoteLLMEngine("https://example.test", "deepseek-flash", provider_kind="deepseek", client=client)
        adapter = GeneratorModelAdapter(engine)
        if tail in {"choices", "usage_only"}:
            response = await adapter.ainvoke(messages, max_tokens=16)
            assert response.output == "ok" and response.finish_reason == "stop"
            assert response.actual_usage.total_tokens == 12
            evidence = response.provider_usage
            native = await adapter.ainvoke(messages, max_tokens=16, generation_options={"tools": []})
            assert native.provider_usage == evidence
            if tail == "choices":
                from core.runtime.circuit_breaker import ModelCircuitBreakerRegistry
                from core.runtime.model_invocation import ModelAdapterResolver, ModelInvocationRouter
                from tests.test_model_invocation import LOCAL, routing

                context, _ = create_run_context(entry_agent_id="test")
                ledger = BudgetLedger(RunBudget(max_model_calls=1))
                result = await ModelInvocationRouter().ainvoke(
                    run_context=context, budget_ledger=ledger, routing_decision=routing(LOCAL),
                    messages=messages, adapter_resolver=ModelAdapterResolver({LOCAL.profile_id: adapter}),
                    circuit_breaker_registry=ModelCircuitBreakerRegistry(), token_estimate=10, max_tokens=16)
                assert result.attempts[0].provider_usage == evidence
                assert ledger.snapshot().committed_usage.total_tokens == 12
                assert ledger.snapshot().committed_usage.model_calls == 1
        else:
            accepted = []

            async def accept(text):
                accepted.append(text)

            with pytest.raises(ModelAdapterInvocationError) as caught:
                await adapter.ainvoke(messages, max_tokens=16, on_delta=accept)
            assert caught.value.provider_started is True
            assert caught.value.provider_responded is (tail != "read_timeout")
            assert caught.value.output_started is (tail != "read_timeout")
            assert accepted == ([] if tail == "read_timeout" else ["ok"])
            evidence = caught.value.provider_usage
        if tail in {"disconnect_before_usage", "read_timeout"}:
            assert evidence.reason == "USAGE_MISSING"
            assert evidence.actual_usage() is None
        else:
            assert (evidence.cache_hit_tokens, evidence.cache_miss_tokens, evidence.reasoning_tokens) == (4, 3, 2)
            assert evidence.actual_usage().total_tokens == 12
            assert evidence.response_id_digest == sha256(b"response-123").hexdigest()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["before_dispatch", "headers", "usage", "usage_overrun", "token_after_usage", "deadline_after_usage"])
async def test_async_model_terminal_preserves_usage_and_settles_reservation(boundary):
    import asyncio

    from core.runtime.cancellation import RunCancelledError
    from core.runtime.circuit_breaker import ModelCircuitBreakerRegistry
    from core.runtime.context import RunDeadlineExceededError
    from core.runtime.model_invocation import ModelAdapterResolver, ModelInvocationRouter
    from tests.test_model_invocation import LOCAL, routing

    reached = asyncio.Event()
    stopped = asyncio.Event()
    calls = 0

    async def wait_for_stop():
        reached.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    reported = 51 if boundary == "usage_overrun" else 5

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            usage = {"prompt_tokens": 2, "completion_tokens": reported - 2, "total_tokens": reported}
            yield f'data: {json.dumps({"usage": usage})}\n\n'.encode()
            await wait_for_stop()

    async def handle(request):
        nonlocal calls
        calls += 1
        if boundary == "headers":
            await wait_for_stop()
        return httpx.Response(200, stream=Stream(), request=request)

    class Guard:
        async def check_async(self, *, admission=None):
            if admission is not None:
                await wait_for_stop()

    context, source = create_run_context(
        entry_agent_id="test", timeout_seconds=0.5 if boundary == "deadline_after_usage" else 10)
    if boundary == "before_dispatch":
        context.memory_guard = Guard()
    ledger = BudgetLedger(RunBudget(max_total_tokens=50))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        engine = RemoteLLMEngine("https://example.test", "deepseek-flash", provider_kind="deepseek", client=client)
        task = asyncio.create_task(ModelInvocationRouter().ainvoke(
            run_context=context, budget_ledger=ledger, routing_decision=routing(LOCAL),
            messages=({"role": "user", "content": "hello"},),
            adapter_resolver=ModelAdapterResolver({LOCAL.profile_id: GeneratorModelAdapter(engine)}),
            circuit_breaker_registry=ModelCircuitBreakerRegistry(), token_estimate=10, max_tokens=8))
        await asyncio.wait_for(reached.wait(), timeout=2)
        if boundary == "token_after_usage":
            source.cancel("REQUEST_CANCELLED")
            error = RunCancelledError
        elif boundary == "deadline_after_usage":
            error = RunDeadlineExceededError
        else:
            task.cancel()
            error = asyncio.CancelledError
        with pytest.raises(error) as caught:
            await asyncio.wait_for(task, timeout=2)
        snap = ledger.snapshot()
        assert snap.active_reservation_count == 0
        if boundary == "before_dispatch":
            assert calls == 0 and snap.committed_usage.total_tokens == 0
            assert caught.value.provider_started is False
        else:
            attempt, = caught.value.model_attempts
            assert calls == 1 and attempt.started and not attempt.succeeded
            assert snap.committed_usage.model_calls == 1
            if boundary == "headers":
                assert attempt.provider_usage.actual_usage() is None
                assert snap.committed_usage.total_tokens == 18
            else:
                assert caught.value.provider_usage.total_tokens == attempt.provider_usage.total_tokens == reported
                assert snap.committed_usage.total_tokens == reported
                if boundary == "usage_overrun":
                    assert caught.value.budget_error.error_code == "BUDGET_EXHAUSTED"
        assert stopped.is_set()


@pytest.mark.parametrize("reported_total", [17, 21, 25, 26])
def test_legacy_final_response_settles_actual_overrun(monkeypatch, reported_total):
    from core.runtime.budget import BudgetExceededError, BudgetUsage
    from tests.test_model_invocation import LOCAL
    from tests.test_model_invocation_durable import _compiled_agent_registry

    calls = 0

    async def handle(request):
        nonlocal calls
        calls += 1
        usage = {"prompt_tokens": reported_total - 5, "completion_tokens": 5, "total_tokens": reported_total}
        wire = 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
        wire += f'data: {json.dumps({"usage": usage})}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, content=wire, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    engine = RemoteLLMEngine("https://example.test", "deepseek-flash", provider_kind="deepseek", client=client)
    router = AgentRouter(engine, memory_manager=object(), agent_registry=_compiled_agent_registry(), max_tokens=8)
    messages = [{"role": "user", "content": "hello"}]
    monkeypatch.setattr(router, "_prepare_answer_messages", lambda **_: messages)
    monkeypatch.setattr(router, "_final_provider_messages", lambda *_, **__: messages)
    monkeypatch.setattr(router, "_select_model", lambda *_: (engine, LOCAL))
    monkeypatch.setattr(router, "_estimate_messages_tokens", lambda _: 13)
    context, _ = create_run_context(entry_agent_id="core_router")
    ledger = BudgetLedger(RunBudget(max_total_tokens=25))
    context.attach_budget_ledger(ledger)
    try:
        if reported_total > 25:
            with pytest.raises(BudgetExceededError) as caught:
                router._complete_final_response("core_router", "hello", run_context=context)
            assert caught.value.error_code == "BUDGET_EXHAUSTED"
            assert caught.value.snapshot.committed_usage.total_tokens == reported_total
            requested = BudgetUsage(input_tokens=1, total_tokens=1)
            with pytest.raises(BudgetExceededError):
                ledger.reserve(requested, reservation_type="model")
        else:
            assert router._complete_final_response("core_router", "hello", run_context=context) == "ok"
        snap = ledger.snapshot()
        assert calls == snap.committed_usage.model_calls == 1
        assert snap.committed_usage.total_tokens == reported_total
        assert snap.remaining.total_tokens == max(0, 25 - reported_total)
        assert snap.active_reservation_count == 0
    finally:
        engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("tail_id", [None, "bad id", "different-id"])
async def test_early_response_id_survives_unknown_usage_without_misattribution(tail_id):
    from hashlib import sha256
    from core.runtime.model_invocation import ModelInvocationRouter

    payloads = [
        {"id": "early-provider-id", "usage": None, "choices": [{"delta": {"content": "ok"}}]},
        {"id": tail_id, "choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    wire = "".join(f"data: {json.dumps(payload)}\n\n" for payload in payloads) + "data: [DONE]\n\n"

    async def handle(request):
        return httpx.Response(200, content=wire.encode(), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        engine = RemoteLLMEngine("https://example.test", "deepseek-flash", provider_kind="deepseek", client=client)
        result = await GeneratorModelAdapter(engine).ainvoke([{"role": "user", "content": "hi"}], max_tokens=10)
    assert result.actual_usage is None and result.provider_usage.reason == "USAGE_MISSING"
    evidence = ModelInvocationRouter._usage_evidence(None, result.provider_usage)
    expected = None if tail_id == "different-id" else sha256(b"early-provider-id").hexdigest()
    assert evidence["provider_usage"]["counts"]["response_id_digest"] == expected
    assert "early-provider-id" not in json.dumps(evidence)


@pytest.mark.asyncio
@pytest.mark.parametrize(("terminal", "cleanup"), [
    ("deadline", "blocked_read"),
    ("cancel", "blocked_close"),
    ("cancel", "closing_after_done"),
    ("cancel", "close_after_read_error"),
    ("cancel", "close_error"),
    ("deadline", "close_error"),
    ("cancel", "normal"),
])
async def test_http_cleanup_preserves_terminal_and_usage(terminal, cleanup):
    import asyncio
    from hashlib import sha256

    from core.runtime.circuit_breaker import ModelCircuitBreakerRegistry
    from core.runtime.context import RunDeadlineExceededError
    from core.runtime.model_invocation import ModelAdapterResolver, ModelInvocationRouter
    from core.runtime.retry import RetryExecutor, RetryPolicy
    from tests.test_model_invocation import LOCAL, REMOTE, routing

    waiting_for_tail = asyncio.Event()
    cleanup_entered = asyncio.Event()
    release_cleanup = asyncio.Event()
    closed = asyncio.Event()
    calls = closes = 0
    reading = False
    blocked_close = cleanup in {"blocked_close", "closing_after_done", "close_after_read_error"}
    blocked = cleanup == "blocked_read" or blocked_close

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal reading
            yield b'data: {"id":"provider-response","usage":{"prompt_tokens":2,"completion_tokens":3,"total_tokens":5}}\n\n'
            if cleanup == "closing_after_done":
                yield b"data: [DONE]\n\n"
                return
            reading = True
            waiting_for_tail.set()
            try:
                if cleanup == "close_after_read_error":
                    raise httpx.ReadTimeout("primary read timeout")
                await asyncio.Future()
            finally:
                try:
                    if cleanup == "blocked_read":
                        cleanup_entered.set()
                        await release_cleanup.wait()
                finally:
                    reading = False

        async def aclose(self):
            nonlocal closes
            closes += 1
            assert not reading, "response closed before its read task exited"
            try:
                if blocked_close:
                    cleanup_entered.set()
                    await release_cleanup.wait()
                if cleanup == "close_error":
                    raise httpx.CloseError("secondary close failure")
            finally:
                closed.set()

    async def handle(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, stream=Stream(), request=request)
        return httpx.Response(200, request=request,
            content=b'data: {"choices":[{"delta":{"content":"unexpected retry"}}]}\n\ndata: [DONE]\n\n')

    context, _ = create_run_context(entry_agent_id="test", timeout_seconds=0.5 if terminal == "deadline" else 10)
    ledger = BudgetLedger(RunBudget(max_total_tokens=100))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        engine = RemoteLLMEngine("https://example.test", "deepseek-flash", provider_kind="deepseek", client=client)
        adapter = GeneratorModelAdapter(engine)
        router = ModelInvocationRouter(retry_executor=RetryExecutor(
            RetryPolicy(max_attempts=2, base_delay_seconds=0, max_delay_seconds=0)))
        task = asyncio.create_task(router.ainvoke(
            run_context=context, budget_ledger=ledger, routing_decision=routing(LOCAL, REMOTE),
            messages=({"role": "user", "content": "hi"},),
            adapter_resolver=ModelAdapterResolver({LOCAL.profile_id: adapter, REMOTE.profile_id: adapter}),
            circuit_breaker_registry=ModelCircuitBreakerRegistry(), token_estimate=10, max_tokens=8))
        try:
            if cleanup in {"closing_after_done", "close_after_read_error"}:
                await asyncio.wait_for(cleanup_entered.wait(), timeout=2)
            else:
                await asyncio.wait_for(waiting_for_tail.wait(), timeout=2)
            if terminal == "cancel":
                task.cancel("external cancellation")
            if blocked:
                await asyncio.wait_for(cleanup_entered.wait(), timeout=2)
            done, _ = await asyncio.wait({task}, timeout=1)
            assert task in done, "terminal propagation waited for blocked HTTP cleanup"
            error = asyncio.CancelledError if terminal == "cancel" else RunDeadlineExceededError
            with pytest.raises(error) as caught:
                await task
            if terminal == "cancel":
                assert caught.value.args == ("external cancellation",)
            attempt, = caught.value.model_attempts
            assert attempt.started and not attempt.succeeded
            assert attempt.provider_usage.total_tokens == 5
            assert attempt.provider_usage.response_id_digest == sha256(b"provider-response").hexdigest()
            snapshot = ledger.snapshot()
            assert calls == snapshot.committed_usage.model_calls == 1
            assert snapshot.committed_usage.total_tokens == 5
            assert snapshot.active_reservation_count == 0
            if blocked:
                assert not closed.is_set()
                assert len(engine._http_cleanup_tasks) == 1
            else:
                assert closed.is_set()
        finally:
            release_cleanup.set()
            if cleanup_entered.is_set():
                await asyncio.wait_for(closed.wait(), timeout=2)
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=2)
            if engine._http_cleanup_tasks:
                _, pending = await asyncio.wait(tuple(engine._http_cleanup_tasks), timeout=2)
                assert not pending
        assert not engine._http_cleanup_tasks
        assert closes == calls == 1
        assert ledger.snapshot().committed_usage == snapshot.committed_usage


@pytest.mark.asyncio
@pytest.mark.parametrize("read_failed", [False, True])
async def test_http_close_error_preserves_primary_failure_contract(read_failed):
    from core.llm_engine import RemoteLLMError

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            if read_failed:
                raise httpx.ReadTimeout("primary timeout")
            yield b'data: [DONE]\n\n'

        async def aclose(self):
            raise httpx.CloseError("close failure")

    async def handle(request):
        return httpx.Response(200, stream=Stream(), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        engine = RemoteLLMEngine("https://example.test", "deepseek-flash", provider_kind="deepseek", client=client)
        with pytest.raises(RemoteLLMError) as caught:
            _ = [delta async for delta in engine.agenerate([{"role": "user", "content": "hi"}])]
    assert caught.value.safe_error_code == ("PROVIDER_TIMEOUT" if read_failed else "PROVIDER_CONNECTION_ERROR")


@pytest.mark.asyncio
async def test_http_cleanup_closes_late_response_after_headers_cancel():
    import asyncio

    waiting_for_headers = asyncio.Event()
    release_headers = asyncio.Event()
    calls = closes = 0

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            pytest.fail("a cancelled request must not read the late response")
            yield b""

        async def aclose(self):
            nonlocal closes
            closes += 1

    async def handle(request):
        nonlocal calls
        calls += 1
        waiting_for_headers.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            await release_headers.wait()
            return httpx.Response(200, stream=Stream(), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        engine = RemoteLLMEngine("https://example.test", "deepseek-flash", provider_kind="deepseek", client=client)
        task = asyncio.create_task(GeneratorModelAdapter(engine).ainvoke(
            [{"role": "user", "content": "hi"}], max_tokens=8))
        try:
            await asyncio.wait_for(waiting_for_headers.wait(), timeout=2)
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=1)
            assert task in done
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
            assert caught.value.provider_started is True
            assert caught.value.provider_responded is False
            assert caught.value.provider_usage.actual_usage() is None
            assert len(engine._http_cleanup_tasks) == 1 and closes == 0
        finally:
            release_headers.set()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=2)
            if engine._http_cleanup_tasks:
                _, pending = await asyncio.wait(tuple(engine._http_cleanup_tasks), timeout=2)
                assert not pending
        assert not engine._http_cleanup_tasks
        assert closes == calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["planner", "repair", "deadline", "cancel", "repair_cancel", "model_limit", "denied"])
async def test_fixed_plan_remote_text_tool_calls_use_step_owner_loop(tmp_path, monkeypatch, scenario):
    import asyncio
    from dataclasses import replace
    from types import SimpleNamespace

    from core.agent_platform.registry import AgentRegistrationBundle, compile_agent_catalog
    from core.runtime.agent_registry import DEFAULT_AGENT_REGISTRY
    from core.runtime.cancellation import RunCancelledError
    from core.runtime.context import RunDeadlineExceededError
    from core.runtime.event_channel import RuntimeEventChannel
    from core.runtime.event_emitter import RunEventEmitter
    from core.runtime.model_selection import ModelSelectionError
    from core.runtime.planning import TaskCapabilityRequirements
    from core.runtime.tool_governance import ToolGovernanceError, builtin_tool_permission_seed
    from core.runtime.tool_registry import ToolRegistry
    from server import _build_tool_governance
    from tests.test_model_invocation import REMOTE
    from tests.test_model_invocation_durable import _DurableRepository
    from tools import demo_workspace
    from tools.registry import register_all_tools

    monkeypatch.setattr(demo_workspace, "default_demo_workspace_root", lambda: str(tmp_path))
    (tmp_path / "project_note.txt").write_text("fixture workspace observation", encoding="utf-8")
    registry = ToolRegistry()
    register_all_tools(registry)
    builtin = DEFAULT_AGENT_REGISTRY
    grants = builtin_tool_permission_seed(registry.registered_names, frozenset(builtin.agent_ids))
    if scenario == "denied":
        grants["workspace_write_file"] -= {"core_router"}
    catalog = compile_agent_catalog(AgentRegistrationBundle(registrations=()),
        builtin_registrations=tuple(builtin.resolve(name) for name in builtin.agent_ids),
        actual_tool_names=registry.registered_names,
        actual_tool_registrations={r.descriptor.name: r for r in registry.startup_registrations},
        platform_tool_permission_seed=grants)
    registry.freeze()
    governance = _build_tool_governance(registry, catalog.agent_registry, tool_grants=grants)

    owner_loop = asyncio.get_running_loop()
    reading = asyncio.Event()
    cleanup_started = asyncio.Event()
    closed = asyncio.Event()
    release_gate = []
    provider_requests = []
    model_contexts = []
    needs_repair = scenario in {"repair", "repair_cancel"}
    stop_at = 2 if scenario == "repair_cancel" else 1

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            future = asyncio.get_running_loop().create_future()
            release_gate.append(future)
            yield b'data: {"usage":{"prompt_tokens":2,"completion_tokens":3,"total_tokens":5}}\n\n'
            owner_loop.call_soon_threadsafe(reading.set)
            try:
                await asyncio.Future()
            finally:
                owner_loop.call_soon_threadsafe(cleanup_started.set)
                while not future.done():
                    try:
                        await asyncio.shield(future)
                    except asyncio.CancelledError:
                        continue

        async def aclose(self):
            owner_loop.call_soon_threadsafe(closed.set)

    async def handle(request):
        body = json.loads(request.content)
        provider_requests.append((asyncio.get_running_loop(), body))
        number = len(provider_requests)
        if scenario in {"deadline", "cancel", "repair_cancel"} and number == stop_at:
            return httpx.Response(200, stream=Stream(), request=request)
        if scenario == "denied":
            output = 'CALL: workspace_write_file({"path":"denied.txt","content":"denied"})'
        elif number == 1 and needs_repair:
            output = "CALL: workspace_read_file({})"
        elif number <= 1 + needs_repair:
            output = 'CALL: workspace_read_file({"path":"project_note.txt"})'
        else:
            output = "fixture final answer"
        payload = {"choices": [{"delta": {"content": output}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 2, "completion_tokens": number, "total_tokens": 2 + number}}
        wire = f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n"
        return httpx.Response(200, content=wire.encode(), request=request)

    context, source = create_run_context(entry_agent_id="core_router",
        timeout_seconds=0.5 if scenario == "deadline" else 10)
    ledger = BudgetLedger(RunBudget(max_model_calls=1 if scenario == "model_limit" else 4, max_tool_calls=1))
    reservations = []
    reserve = ledger.reserve

    def record_reservation(usage, **kwargs):
        reservation = reserve(usage, **kwargs)
        if usage.model_calls:
            reservations.append(reservation)
        return reservation

    monkeypatch.setattr(ledger, "reserve", record_reservation)
    context.attach_budget_ledger(ledger)
    repository = _DurableRepository()
    context.attach_durable_lease(SimpleNamespace(run_id=context.run_id, fencing_token=1))
    context.attach_execution_repository(repository)
    channel = RuntimeEventChannel(128, run_id=context.run_id)
    emitter = RunEventEmitter(run_id=context.run_id, trace_id=context.trace_id, channel=channel).for_step("answer")
    query = "请读取 demo workspace 里的 project_note.txt，告诉我里面记录了什么。"

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        engine = RemoteLLMEngine("https://example.test", "mock-remote", client=client,
            timeout_seconds=0.7 if scenario == "deadline" else 10)
        agenerate = engine.agenerate

        def capture_context(*args, **kwargs):
            model_contexts.append(kwargs.get("run_context"))
            return agenerate(*args, **kwargs)

        monkeypatch.setattr(engine, "agenerate", capture_context)
        model = replace(REMOTE, provider_kind="openai_compatible", model_identity="mock-remote")
        router = AgentRouter(engine, memory_manager=object(), model_profiles=(model,),
            agent_registry=catalog.agent_registry, tool_registry=registry,
            tool_governance_service=governance, max_tokens=64)
        monkeypatch.setattr(router, "_build_messages", lambda **_: [
            {"role": "system", "content": "fixture system"}, {"role": "user", "content": query}])
        worker = asyncio.create_task(asyncio.to_thread(router.complete_single_agent, "core_router", query,
            run_context=context, capability_requirements=TaskCapabilityRequirements(requires_tools=True),
            event_emitter=emitter, persist=False, raise_security_denial=True))
        try:
            if scenario in {"deadline", "cancel", "repair_cancel"}:
                await asyncio.wait_for(reading.wait(), timeout=2)
                if scenario != "deadline":
                    source.cancel("REQUEST_CANCELLED")
                await asyncio.wait_for(cleanup_started.wait(), timeout=2)
                done, _ = await asyncio.wait({worker}, timeout=1)
                assert worker in done, "fixed-plan worker waited for HTTP cleanup"
                with pytest.raises(RunDeadlineExceededError if scenario == "deadline" else RunCancelledError):
                    await worker
                assert len(provider_requests) == stop_at
                assert repository.rows[-1].state == "UNKNOWN"
                assert repository.rows[-1].usage_evidence["total_tokens"] == 5
                assert not closed.is_set() and len(engine._http_cleanup_tasks) == 1
            elif scenario == "denied":
                with pytest.raises(ToolGovernanceError):
                    await asyncio.wait_for(worker, timeout=3)
                assert len(provider_requests) == 1
                assert not (tmp_path / "denied.txt").exists()
            elif scenario == "model_limit":
                with pytest.raises(ModelSelectionError, match="budget_unavailable"):
                    await asyncio.wait_for(worker, timeout=3)
                assert len(provider_requests) == 1
            else:
                assert await asyncio.wait_for(worker, timeout=3) == "fixture final answer"
                assert len(provider_requests) == 2 + needs_repair
                assert "fixture workspace observation" in json.dumps(provider_requests[-1][1]["messages"])
            assert all(loop is owner_loop for loop, _ in provider_requests)
            assert model_contexts == [context] * len(provider_requests)
            assert [row.model_attempt_number for row in repository.rows] == list(range(1, len(provider_requests) + 1))
            assert all(row.step_id == "answer" for row in repository.rows)
            planner = provider_requests[0][1]
            assert planner["temperature"] == 0.1 and planner["max_tokens"] == router.tool_plan_max_tokens
            assert planner["chat_template_kwargs"] == {"enable_thinking": False}
            assert "tools" not in planner
            if needs_repair:
                repair = provider_requests[1][1]
                assert repair["max_tokens"] == router.tool_plan_max_tokens and repair["temperature"] == 0.1
                assert repair["chat_template_kwargs"] == {"enable_thinking": False}
                assert "TOOL_VALIDATION_ERROR" in repair["messages"][0]["content"]
            budget = ledger.snapshot()
            assert budget.active_reservation_count == 0
            assert len(reservations) == budget.committed_usage.model_calls == len(provider_requests)
            assert budget.committed_usage.tool_calls == int(scenario in {"planner", "repair", "model_limit"})
            if scenario not in {"deadline", "cancel", "repair_cancel"}:
                reported_totals = [row.usage_evidence["total_tokens"] for row in repository.rows]
                assert reported_totals == list(range(3, 3 + len(provider_requests)))
                assert budget.committed_usage.total_tokens == sum(reported_totals)
        finally:
            for future in release_gate:
                future.get_loop().call_soon_threadsafe(future.set_result, None)
            await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), timeout=3)
            if engine._http_cleanup_tasks:
                _, pending = await asyncio.wait(tuple(engine._http_cleanup_tasks), timeout=2)
                assert not pending
            await channel.close()
        assert not engine._http_cleanup_tasks
        assert closed.is_set() == (scenario in {"deadline", "cancel", "repair_cancel"})
