from __future__ import annotations

import pytest

from core.runtime import ChatRuntimeMode
from core.settings import Settings


def test_production_settings_reject_legacy_runtime_configuration(
    monkeypatch,
) -> None:
    monkeypatch.delenv("CHAT_RUNTIME_MODE", raising=False)
    assert Settings.load().chat_runtime_mode is ChatRuntimeMode.COORDINATED
    monkeypatch.setenv("CHAT_RUNTIME_MODE", "LEGACY")
    with pytest.raises(ValueError, match="CHAT_RUNTIME_MODE"):
        Settings.load()
