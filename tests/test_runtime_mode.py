from __future__ import annotations

import pytest

from core.runtime import ChatRuntimeMode, ChatRuntimeSelector
from core.settings import Settings


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("legacy", ChatRuntimeMode.LEGACY),
        (" LEGACY ", ChatRuntimeMode.LEGACY),
        ("CoOrDiNaTeD", ChatRuntimeMode.COORDINATED),
        ("", ChatRuntimeMode.LEGACY),
        ("   ", ChatRuntimeMode.LEGACY),
        (None, ChatRuntimeMode.LEGACY),
    ],
)
def test_runtime_mode_normalizes_only_known_values(raw, expected) -> None:
    assert ChatRuntimeMode.parse(raw) is expected


def test_runtime_mode_rejects_unknown_value() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        ChatRuntimeMode.parse("automatic")


def test_settings_uses_exact_mode_key_and_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("CHAT_RUNTIME_MODE", "coordinated")
    assert Settings.load().chat_runtime_mode is ChatRuntimeMode.COORDINATED
    monkeypatch.setenv("CHAT_RUNTIME_MODE", "legacy-if-coordinated-fails")
    with pytest.raises(ValueError, match="CHAT_RUNTIME_MODE"):
        Settings.load()


def test_selector_is_an_immutable_request_snapshot(monkeypatch) -> None:
    selector = ChatRuntimeSelector(ChatRuntimeMode.LEGACY)
    monkeypatch.setenv("CHAT_RUNTIME_MODE", "COORDINATED")
    assert selector.capture() is ChatRuntimeMode.LEGACY
    assert selector.capture() is ChatRuntimeMode.LEGACY
