from datetime import UTC, datetime

from core.advanced_memory import AdvancedMemoryStore, MemoryOrigin, SemanticMemoryRecord
from core.memory_manager import MEMORY_SCHEMA_VERSION, MemoryManager, memory_preflight
from core.runtime.memory_authorization import MemoryAccessPrincipal
from core.runtime.project_memory import (
    ProjectIdentity, ProjectMemoryGrant, ProjectMemoryPermission, ProjectSemanticMemoryService,
    ProjectSemanticMemoryStore,
)


def _service(tmp_path):
    path = tmp_path / "memory.db"
    MemoryManager(db_path=str(path))
    private = AdvancedMemoryStore(str(path))
    return private, ProjectSemanticMemoryService(ProjectSemanticMemoryStore(str(path)), private), path


def _grant(agent="agent-a", *permissions):
    return ProjectMemoryGrant("project-p", agent, frozenset(permissions))


def test_project_write_read_forget_are_grant_and_scope_bound(tmp_path):
    _, service, path = _service(tmp_path)
    writer = MemoryAccessPrincipal("agent-a")
    write = _grant("agent-a", ProjectMemoryPermission.WRITE)
    created = service.write(requester=writer, project=ProjectIdentity("project-p"), grant=write, logical_key="database", canonical_text="database is PostgreSQL", payload={"value": "PostgreSQL"}, run_id="r1")
    assert created.outcome == "CREATED"
    records, read = service.read(requester=writer, project=ProjectIdentity("project-p"), grant=_grant("agent-a", ProjectMemoryPermission.READ))
    assert read.allowed and [record.logical_key for record in records] == ["database"]
    denied, decision = service.read(requester=MemoryAccessPrincipal("agent-b"), project=ProjectIdentity("project-p"), grant=None)
    assert denied == () and not decision.allowed
    assert service.forget(requester=writer, project=ProjectIdentity("project-p"), grant=write, logical_key="database").affected_count == 0
    assert service.forget(requester=writer, project=ProjectIdentity("project-p"), grant=_grant("agent-a", ProjectMemoryPermission.FORGET), logical_key="database").affected_count == 1
    assert memory_preflight(str(path)).target_version == str(MEMORY_SCHEMA_VERSION)


def test_project_conflict_and_explicit_private_promotion_are_safe(tmp_path):
    private, service, _ = _service(tmp_path)
    owner = MemoryAccessPrincipal("agent-a")
    source = SemanticMemoryRecord(memory_id="private-1", agent_id="agent-a", memory_scope="direct", canonical_text="database is SQLite", payload={"value": "SQLite"}, logical_key="database", origin=MemoryOrigin("FINAL", "r1", "e1", "agent-a", "direct"), created_at=datetime.now(UTC), updated_at=datetime.now(UTC))
    private.create(source)
    grant = _grant("agent-a", ProjectMemoryPermission.WRITE, ProjectMemoryPermission.PROMOTE)
    promoted = service.promote(requester=owner, project=ProjectIdentity("project-p"), grant=grant, source_memory_id="private-1", source_owner_agent_id="agent-a", source_scope="direct", run_id="r2")
    assert promoted.outcome == "CREATED" and promoted.record.source_memory_id == "private-1"
    assert private.get_by_memory_id("private-1").agent_id == "agent-a"
    replay = service.promote(requester=owner, project=ProjectIdentity("project-p"), grant=grant, source_memory_id="private-1", source_owner_agent_id="agent-a", source_scope="direct", run_id="r2")
    assert replay.outcome == "NO_CHANGE"
    foreign = service.promote(requester=MemoryAccessPrincipal("agent-b"), project=ProjectIdentity("project-p"), grant=_grant("agent-b", ProjectMemoryPermission.WRITE, ProjectMemoryPermission.PROMOTE), source_memory_id="private-1", source_owner_agent_id="agent-a", source_scope="direct", run_id="r2")
    assert foreign.outcome == "DENIED"
    conflict = service.write(requester=owner, project=ProjectIdentity("project-p"), grant=grant, logical_key="database", canonical_text="database is PostgreSQL", payload={"value": "PostgreSQL"}, run_id="r3")
    assert conflict.outcome == "CONFLICT"
    assert service.write(requester=owner, project=ProjectIdentity("project-p"), grant=grant, logical_key="database", canonical_text="database is PostgreSQL", payload={"value": "PostgreSQL"}, run_id="r3", supersede=True).outcome == "SUPERSEDED"
