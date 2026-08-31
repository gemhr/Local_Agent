"""WP7-E v4 HTTP contract: typed controls, real authorities, safe evidence."""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import server
from core.advanced_memory import AdvancedMemoryStore
from core.chat_service import ChatService
from core.memory_manager import MemoryManager
from core.runtime import ChatRuntimeSelector, CoordinatedRuntimeFactory
from tests._runtime_assembly_fixtures import make_services
from tests.test_episodic_evaluation_harness import EpisodicEvalFakeModel
from tests.test_wp3_history_boundary import direct_json, make_real_router


def _service(tmp_path):
    db_path = tmp_path / "memory.db"
    memory = MemoryManager(db_path=str(db_path))
    services = make_services(snapshot_enabled=False)
    router = make_real_router(memory, model=EpisodicEvalFakeModel(direct_json()))
    factory = CoordinatedRuntimeFactory(router, services, event_channel_capacity=32)
    return ChatService(router, coordinated_runtime_factory=factory, run_registry=services.run_registry), AdvancedMemoryStore(str(db_path))


def _payload(run_id: str, control: dict) -> server.RuntimeEvaluationExecuteV4Request:
    return server.RuntimeEvaluationExecuteV4Request(
        agent_id="core_router", query="SQLite deployment", run_id=run_id,
        timeout_seconds=30.0, evaluation_control=control,
    )


def test_v4_schema_rejects_untyped_project_inputs() -> None:
    with pytest.raises(ValidationError):
        _payload(uuid.uuid4().hex, {"requester_agent_id": "agent-a", "project_identity": {"project_id": "p"}, "project_grants": [{"project_id": "p", "agent_id": "agent-a", "permissions": ["ADMIN"]}]})
    with pytest.raises(ValidationError):
        _payload(uuid.uuid4().hex, {"requester_agent_id": "agent-a", "operation": {"operation": "SHARED_EPISODIC"}})


@pytest.mark.asyncio
async def test_v4_foreign_private_read_is_denied_and_safe(tmp_path, monkeypatch) -> None:
    service, _ = _service(tmp_path)
    monkeypatch.setattr(server, "chat_service", service)
    secret = "api_key=do-not-return"
    response = await server.runtime_evaluation_execute_v4_endpoint(_payload(uuid.uuid4().hex, {
        "requester_agent_id": "agent-b",
        "private_fixtures": [{"fixture_ref": "private-a", "owner_agent_id": "agent-a", "logical_key": "database", "canonical_text": secret}],
        "operation": {"operation": "PRIVATE_READ", "target_owner_agent_id": "agent-a"},
    }))
    body = json.loads(response.body)
    assert body["authorization"]["decision"] == "DENY"
    assert body["private_retrieval"]["candidate_count"] == 0
    assert body["private_retrieval"]["selected_count"] == 0
    assert body["private_retrieval"]["injected_count"] == 0
    assert secret not in response.body.decode("utf-8")


@pytest.mark.asyncio
async def test_v4_project_grant_and_promotion_return_safe_facts(tmp_path, monkeypatch) -> None:
    service, store = _service(tmp_path)
    monkeypatch.setattr(server, "chat_service", service)
    run_id = uuid.uuid4().hex
    promoted = await server.runtime_evaluation_execute_v4_endpoint(_payload(run_id, {
        "requester_agent_id": "agent-a",
        "project_identity": {"project_id": "project-p"},
        "project_grants": [{"project_id": "project-p", "agent_id": "agent-a", "permissions": ["WRITE", "PROMOTE", "READ"]}],
        "private_fixtures": [{"fixture_ref": "private-a", "owner_agent_id": "agent-a", "logical_key": "database", "canonical_text": "SQLite deployment"}],
        "operation": {"operation": "PRIVATE_TO_PROJECT_PROMOTION", "target_owner_agent_id": "agent-a", "source_memory_id": "wp7-fixture-" + uuid.uuid5(uuid.NAMESPACE_URL, "private-a").hex},
    }))
    body = json.loads(promoted.body)
    assert body["promotion"]["decision"] == "ALLOW"
    assert body["promotion"]["provenance_complete"] is True
    assert body["promotion"]["resulting_project_memory_ref"]
    assert store.get_by_memory_id(body["promotion"]["source_private_memory_ref"]).agent_id == "agent-a"

    denied = await server.runtime_evaluation_execute_v4_endpoint(_payload(uuid.uuid4().hex, {
        "requester_agent_id": "agent-b", "project_identity": {"project_id": "project-p"},
        "project_grants": [{"project_id": "project-p", "agent_id": "agent-b", "permissions": ["READ"]}],
        "operation": {"operation": "PROJECT_FORGET", "logical_key": "database"},
    }))
    denied_body = json.loads(denied.body)
    assert denied_body["mutation"]["affected_count"] == 0
    assert denied_body["mutation"]["outcome"] == "DENIED"


@pytest.mark.asyncio
async def test_v4_deterministic_multi_agent_reports_step_owner_and_visibility(tmp_path, monkeypatch) -> None:
    service, _ = _service(tmp_path)
    monkeypatch.setattr(server, "chat_service", service)
    response = await server.runtime_evaluation_execute_v4_endpoint(_payload(uuid.uuid4().hex, {
        "requester_agent_id": "core_router", "deterministic_multi_agent": True,
    }))
    body = json.loads(response.body)
    assert body["status"] == "SUCCEEDED"
    assert body["specialist_formation"]
    assert all(item["verified_performer"] == item["episode_owner"] for item in body["specialist_formation"])
    assert body["invocation_visibility"][0]["private_bundle_present"] is False
    assert body["invocation_visibility"][1]["dependency_result_present"] is True
