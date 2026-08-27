from dataclasses import dataclass
import threading

import pytest

from core.agent_router import AgentRouter
from core.llm_engine import RemoteLLMEngine
from core.runtime import BudgetLedger, RunBudget, create_run_context


@dataclass
class FakeResponse:
    payload: dict
    status_code: int = 200
    text: str = ""

    def json(self) -> dict:
        return self.payload


def _capture_request(monkeypatch, payload: dict | None = None):
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return FakeResponse(payload or {"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("core.llm_engine.requests.Session.post", lambda self, url, **kwargs: fake_post(url, **kwargs))
    return captured


def test_deepseek_thinking_enabled_is_explicit(monkeypatch) -> None:
    captured = _capture_request(monkeypatch)
    engine = RemoteLLMEngine(
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        enable_thinking=True,
        provider_kind="deepseek",
    )

    assert list(engine.generate([{"role": "user", "content": "hi"}])) == ["ok"]
    assert captured["json"]["thinking"] == {"type": "enabled"}
    assert captured["json"]["reasoning_effort"] == "high"


def test_deepseek_thinking_disabled_is_explicit(monkeypatch) -> None:
    captured = _capture_request(monkeypatch)
    engine = RemoteLLMEngine(
        "https://api.deepseek.com/v1",
        "deepseek-v4-flash",
        enable_thinking=False,
        provider_kind="deepseek",
    )

    list(engine.generate([{"role": "user", "content": "hi"}]))

    assert captured["url"].endswith("/v1/chat/completions")
    assert captured["json"]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in captured["json"]


def test_deepseek_parameters_are_not_sent_to_other_providers(monkeypatch) -> None:
    captured = _capture_request(monkeypatch)
    engine = RemoteLLMEngine(
        "https://example.test/v1", "Qwen3.5-27B", enable_thinking=False
    )

    list(engine.generate([{"role": "user", "content": "hi"}]))

    assert "thinking" not in captured["json"]
    assert captured["json"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_empty_content_at_length_has_clear_truncation_error(monkeypatch) -> None:
    _capture_request(
        monkeypatch,
        {
            "choices": [
                {
                    "message": {"content": "", "reasoning_content": "thinking only"},
                    "finish_reason": "length",
                }
            ]
        },
    )
    engine = RemoteLLMEngine(
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        provider_kind="deepseek",
    )

    with pytest.raises(RuntimeError, match="truncated before producing final content") as captured:
        list(engine.generate([{"role": "user", "content": "hi"}], max_tokens=24))
    assert captured.value.safe_error_code == "REMOTE_OUTPUT_TRUNCATED"
    assert captured.value.model_failure_category == "CONTEXT_LIMIT_EXCEEDED"


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


def test_remote_client_explicitly_disables_hidden_retries() -> None:
    engine = RemoteLLMEngine("https://example.test", "model")

    for adapter in engine._session.adapters.values():
        assert adapter.max_retries.total == 0
        assert adapter.max_retries.read is False


class FakeSession:
    def __init__(self, *, close_error: bool = False) -> None:
        self.close_calls = 0
        self.close_error = close_error

    def mount(self, *_args) -> None:
        pass

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error:
            raise RuntimeError("fake close failure")


def test_remote_session_close_is_idempotent() -> None:
    session = FakeSession()
    engine = RemoteLLMEngine(
        "https://example.test",
        "model",
        session=session,
    )

    engine.close()
    engine.close()

    assert session.close_calls == 1


def test_remote_session_close_waits_for_active_call() -> None:
    class BlockingSession(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()
            self.closed = threading.Event()

        def post(self, *_args, **_kwargs):
            self.entered.set()
            self.release.wait(2)
            return FakeResponse(
                {"choices": [{"message": {"content": "ok"}}]}
            )

        def close(self) -> None:
            super().close()
            self.closed.set()

    session = BlockingSession()
    engine = RemoteLLMEngine(
        "https://example.test",
        "model",
        session=session,
    )
    call_thread = threading.Thread(
        target=lambda: list(
            engine.generate([{"role": "user", "content": "hi"}])
        )
    )
    close_thread = threading.Thread(target=engine.close)

    call_thread.start()
    assert session.entered.wait(1)
    close_thread.start()
    assert not session.closed.wait(0.1)
    session.release.set()
    call_thread.join(2)
    close_thread.join(2)

    assert session.closed.is_set()
    assert session.close_calls == 1


def test_shutdown_close_errors_do_not_skip_other_engines() -> None:
    from server import _close_model_engines

    failing = FakeSession(close_error=True)
    healthy = FakeSession()

    errors = _close_model_engines({"first": failing, "second": healthy})

    assert errors == ("MODEL_ENGINE_CLOSE_FAILED",)
    assert failing.close_calls == 1
    assert healthy.close_calls == 1
