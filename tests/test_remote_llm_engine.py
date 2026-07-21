from dataclasses import dataclass

import pytest

from core.agent_router import AgentRouter
from core.llm_engine import RemoteLLMEngine


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

    monkeypatch.setattr("core.llm_engine.requests.post", fake_post)
    return captured


def test_deepseek_thinking_enabled_is_explicit(monkeypatch) -> None:
    captured = _capture_request(monkeypatch)
    engine = RemoteLLMEngine(
        "https://api.deepseek.com", "deepseek-v4-flash", enable_thinking=True
    )

    assert list(engine.generate([{"role": "user", "content": "hi"}])) == ["ok"]
    assert captured["json"]["thinking"] == {"type": "enabled"}
    assert captured["json"]["reasoning_effort"] == "high"


def test_deepseek_thinking_disabled_is_explicit(monkeypatch) -> None:
    captured = _capture_request(monkeypatch)
    engine = RemoteLLMEngine(
        "https://api.deepseek.com/v1", "deepseek-v4-flash", enable_thinking=False
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
    engine = RemoteLLMEngine("https://api.deepseek.com", "deepseek-v4-flash")

    with pytest.raises(RuntimeError, match="truncated before producing final content"):
        list(engine.generate([{"role": "user", "content": "hi"}], max_tokens=24))


class RecordingLLM:
    def __init__(self) -> None:
        self.calls = []

    def generate(self, messages, **kwargs):
        self.calls.append(kwargs)
        yield "keyword"


def test_knowledge_rewrite_uses_128_tokens_and_disables_thinking() -> None:
    llm = RecordingLLM()
    router = AgentRouter(llm_engine=llm, memory_manager=object())

    assert router._rewrite_knowledge_query("question") == "keyword"
    assert llm.calls[0]["max_tokens"] == 128
    assert llm.calls[0]["enable_thinking"] is False
