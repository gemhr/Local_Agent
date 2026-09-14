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
        "deepseek-v4-flash",
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
        "deepseek-v4-flash",
        enable_thinking=False,
        provider_kind="deepseek",
        client=client,
    )

    try:
        list(engine.generate([{"role": "user", "content": "hi"}]))
        assert captured["url"].endswith("/v1/chat/completions")
        assert captured["json"]["thinking"] == {"type": "disabled"}
        assert "reasoning_effort" not in captured["json"]
    finally:
        engine.close()


def test_deepseek_native_tool_call_sends_wire_and_normalizes() -> None:
    captured, client = _capture_client({"choices": [{"message": {"content": None, "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "get_system_status", "arguments": "{}"}}]}}]})
    engine = RemoteLLMEngine("https://api.deepseek.com", "deepseek-v4-flash", provider_kind="deepseek", client=client)

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
    engine = RemoteLLMEngine("https://api.deepseek.com", "deepseek-v4-flash", provider_kind="deepseek", client=client)

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
    llm = RecordingLLM()
    router = AgentRouter(llm_engine=llm, memory_manager=object())
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
