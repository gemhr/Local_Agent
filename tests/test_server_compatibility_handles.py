import pytest
from fastapi import HTTPException
from fastapi import FastAPI

import server
from core.runtime import ApplicationRuntimeServices


def test_lifespan_publishes_and_clears_identical_application_handles() -> None:
    app = FastAPI()
    service = object()
    services = object()

    server._publish_compatibility_handles(app, service, services)
    assert server.chat_service is app.state.chat_service is service
    assert (
        server.application_runtime_services
        is app.state.runtime_services
        is services
    )

    server._clear_compatibility_handles(app)
    assert server.chat_service is app.state.chat_service is None
    assert (
        server.application_runtime_services
        is app.state.runtime_services
        is None
    )


def test_closed_module_handle_cannot_be_reused(monkeypatch) -> None:
    monkeypatch.setattr(server, "chat_service", None)
    with pytest.raises(HTTPException) as caught:
        server.require_service()
    assert caught.value.status_code == 503


def test_application_handle_type_does_not_admit_run_or_operation_owners() -> None:
    fields = set(ApplicationRuntimeServices.__dataclass_fields__)
    assert not fields.intersection(
        {"run_context", "agent_state", "event_channel", "fault_controller"}
    )
