"""仅保留仍有效的纯合同/本地测试与共享 fixture；废弃 agent-only 节点见 .ai/archive/tests/scoped_memory_lifecycle/。"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from core.memory_manager import MemoryManager
from core.runtime import (
    CoordinatedRuntimeFactory,
    FaultPoint,
    RunStatus,
    RuntimeEventType,
)
from tests._event_fault_fixtures import event_controller
from tests._wp3_fixtures import delegated_json, make_wp3_services
from tests.test_wp3_history_boundary import FakeModel, make_real_router


def delivered_specialist_content(memory: MemoryManager) -> list[str]:
    return [
        message["content"]
        for message in memory.get_chat_history("core_router", ascending=True)
        if message["role"] == "assistant"
    ]
