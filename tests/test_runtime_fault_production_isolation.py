from __future__ import annotations

import ast
import inspect
from dataclasses import fields
from pathlib import Path

import server
from core.chat_service import ChatService
from core.runtime import CoordinatedRuntimeFactory
from core.settings import Settings


def test_settings_and_http_request_have_no_fault_enablement_surface() -> None:
    settings_fields = {item.name.lower() for item in fields(Settings)}
    request_fields = {name.lower() for name in server.ChatRequest.model_fields}

    assert not any("fault" in name or "chaos" in name for name in settings_fields)
    assert not any("fault" in name or "chaos" in name for name in request_fields)


def test_production_chat_path_does_not_supply_a_fault_controller() -> None:
    source = inspect.getsource(ChatService._stream_factory_coordinated_events)
    signature = inspect.signature(CoordinatedRuntimeFactory.create_run_scope)

    assert "fault_controller=" not in source
    assert signature.parameters["fault_controller"].default is None


def test_fault_module_has_no_global_controller_or_contextvar() -> None:
    source_path = Path(inspect.getfile(CoordinatedRuntimeFactory)).with_name(
        "fault_injection.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    module_calls = []
    for node in tree.body:
        value = node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
        if isinstance(value, ast.Call):
            module_calls.append(ast.unparse(value.func))

    assert "ContextVar" not in source_path.read_text(encoding="utf-8")
    assert not any("FaultInjectionController" in call for call in module_calls)
