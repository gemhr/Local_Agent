"""PROJECT Semantic Memory 的最小 durable governance boundary（WP7-D）。

本模块不从 prompt、Memory 正文或 Planner 推导 project identity/grant。调用方
必须提供 request-bound ``ProjectIdentity`` 与 code-owned typed grant；SQLite 是
持久化 primitive，``ProjectSemanticMemoryService`` 是唯一授权/mutation facade。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from uuid import uuid4

from core.advanced_memory import AdvancedMemoryStore, MemoryStatus, MemoryType, SemanticMemoryRecord
from core.runtime.memory_authorization import MemoryAccessAuthorizer, MemoryAccessPrincipal


class ProjectMemoryPermission(str, Enum):
    READ = "READ"
    WRITE = "WRITE"
    FORGET = "FORGET"
    PROMOTE = "PROMOTE"


class ProjectMemoryReason(str, Enum):
    ALLOW = "ALLOW"
    PROJECT_IDENTITY_MISSING = "PROJECT_IDENTITY_MISSING"
    PROJECT_SCOPE_MISMATCH = "PROJECT_SCOPE_MISMATCH"
    PROJECT_READ_DENIED = "PROJECT_READ_DENIED"
    PROJECT_WRITE_DENIED = "PROJECT_WRITE_DENIED"
    PROJECT_FORGET_DENIED = "PROJECT_FORGET_DENIED"
    PROJECT_PROMOTION_DENIED = "PROJECT_PROMOTION_DENIED"
    PROJECT_SEMANTIC_CONFLICT = "PROJECT_SEMANTIC_CONFLICT"
    UNSUPPORTED_SHARED_EPISODIC = "UNSUPPORTED_SHARED_EPISODIC"
    PRIVATE_SOURCE_DENIED = "PRIVATE_SOURCE_DENIED"


@dataclass(frozen=True, slots=True)
class ProjectIdentity:
    project_id: str
    def __post_init__(self) -> None:
        if not isinstance(self.project_id, str) or not self.project_id.strip():
            raise ValueError("project_id 必须是可信的非空 request-bound identity")
        object.__setattr__(self, "project_id", self.project_id.strip())


@dataclass(frozen=True, slots=True)
class ProjectMemoryGrant:
    project_id: str
    agent_id: str
    permissions: frozenset[ProjectMemoryPermission]
    def __post_init__(self) -> None:
        ProjectIdentity(self.project_id)
        MemoryAccessPrincipal(self.agent_id)
        object.__setattr__(self, "project_id", self.project_id.strip())
        object.__setattr__(self, "agent_id", self.agent_id.strip())
        object.__setattr__(self, "permissions", frozenset(ProjectMemoryPermission(p) for p in self.permissions))


@dataclass(frozen=True, slots=True)
class ProjectMemoryAuthorizationResult:
    operation: str
    requester_agent_id: str | None
    project_id: str | None
    grant_type: str | None
    allowed: bool
    reason: ProjectMemoryReason
    affected_count: int = 0
    def observation(self) -> dict[str, object]:
        return {"operation": self.operation, "requester": self.requester_agent_id,
                "owner_kind": "PROJECT", "owner_match": self.allowed,
                "visibility": "PROJECT", "scope_match": self.allowed,
                "grant_type": self.grant_type, "decision": "ALLOW" if self.allowed else "DENY",
                "reason": self.reason.value, "affected_count": self.affected_count}


@dataclass(frozen=True, slots=True)
class ProjectSemanticRecord:
    memory_id: str
    project_id: str
    logical_key: str
    canonical_text: str
    payload: dict[str, object]
    status: str
    origin_agent_id: str
    origin_run_id: str
    created_by_agent_id: str
    updated_by_agent_id: str
    source_memory_id: str | None = None
    source_owner_agent_id: str | None = None
    promoted_by_agent_id: str | None = None
    promotion_run_id: str | None = None
    promotion_time: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True, slots=True)
class ProjectMemoryMutation:
    outcome: str
    record: ProjectSemanticRecord | None
    authorization: ProjectMemoryAuthorizationResult


class ProjectSemanticMemoryStore:
    """SQLite persistence primitive；不接受 grant，也不做 authorization。"""
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    @staticmethod
    def _row(row: sqlite3.Row) -> ProjectSemanticRecord:
        values = {k: row[k] for k in ProjectSemanticRecord.__dataclass_fields__}
        values["payload"] = json.loads(values["payload"])
        return ProjectSemanticRecord(**values)
    def active(self, project_id: str, logical_key: str | None = None) -> tuple[ProjectSemanticRecord, ...]:
        with self._connect() as conn:
            if logical_key is None:
                rows = conn.execute("SELECT * FROM project_semantic_memory WHERE project_id=? AND visibility='PROJECT' AND status='ACTIVE' ORDER BY created_at DESC, memory_id ASC", (project_id,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM project_semantic_memory WHERE project_id=? AND logical_key=? AND visibility='PROJECT' AND status='ACTIVE' ORDER BY created_at ASC, memory_id ASC", (project_id, logical_key)).fetchall()
        return tuple(self._row(r) for r in rows)
    def mutate(self, record: ProjectSemanticRecord, *, supersede: bool) -> ProjectMemoryMutation:
        now = datetime.now(UTC).isoformat()
        record = replace(record, created_at=now, updated_at=now)
        conn = self._connect(); conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT * FROM project_semantic_memory WHERE project_id=? AND logical_key=? AND visibility='PROJECT' AND status='ACTIVE' ORDER BY created_at ASC, memory_id ASC", (record.project_id, record.logical_key)).fetchall()
            if rows:
                current = self._row(rows[-1])
                if current.payload == record.payload and current.canonical_text == record.canonical_text:
                    conn.execute("COMMIT"); return ProjectMemoryMutation("NO_CHANGE", current, _allow("WRITE", record.created_by_agent_id, record.project_id, ProjectMemoryPermission.WRITE))
                if not supersede:
                    conn.execute("COMMIT"); return ProjectMemoryMutation("CONFLICT", current, _deny("WRITE", record.created_by_agent_id, record.project_id, ProjectMemoryPermission.WRITE, ProjectMemoryReason.PROJECT_SEMANTIC_CONFLICT))
                conn.execute("UPDATE project_semantic_memory SET status='SUPERSEDED', superseded_by_memory_id=?, updated_at=?, updated_by_agent_id=? WHERE project_id=? AND logical_key=? AND status='ACTIVE'", (record.memory_id, now, record.updated_by_agent_id, record.project_id, record.logical_key))
            conn.execute("INSERT INTO project_semantic_memory (memory_id,project_id,owner_kind,owner_id,visibility,scope_id,logical_key,canonical_text,payload,status,origin_agent_id,origin_run_id,created_by_agent_id,updated_by_agent_id,source_memory_id,source_owner_agent_id,promoted_by_agent_id,promotion_run_id,promotion_time,superseded_by_memory_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (record.memory_id,record.project_id,'PROJECT',record.project_id,'PROJECT',record.project_id,record.logical_key,record.canonical_text,json.dumps(record.payload,ensure_ascii=False,sort_keys=True,separators=(',',':')),'ACTIVE',record.origin_agent_id,record.origin_run_id,record.created_by_agent_id,record.updated_by_agent_id,record.source_memory_id,record.source_owner_agent_id,record.promoted_by_agent_id,record.promotion_run_id,record.promotion_time,None,now,now))
            conn.execute("COMMIT"); return ProjectMemoryMutation("SUPERSEDED" if rows else "CREATED", record, _allow("WRITE", record.created_by_agent_id, record.project_id, ProjectMemoryPermission.WRITE))
        except Exception:
            conn.execute("ROLLBACK"); raise
        finally: conn.close()
    def forget(self, project_id: str, logical_key: str, updater: str) -> int:
        with self._connect() as conn:
            cur = conn.execute("UPDATE project_semantic_memory SET status='FORGOTTEN', updated_at=?, updated_by_agent_id=? WHERE project_id=? AND logical_key=? AND visibility='PROJECT' AND status='ACTIVE'", (datetime.now(UTC).isoformat(), updater, project_id, logical_key))
            conn.commit(); return cur.rowcount


def _allow(op: str, agent: str, project: str, permission: ProjectMemoryPermission) -> ProjectMemoryAuthorizationResult:
    return ProjectMemoryAuthorizationResult(op, agent, project, permission.value, True, ProjectMemoryReason.ALLOW)
def _deny(op: str, agent: str | None, project: str | None, permission: ProjectMemoryPermission, reason: ProjectMemoryReason) -> ProjectMemoryAuthorizationResult:
    return ProjectMemoryAuthorizationResult(op, agent, project, permission.value, False, reason)


class ProjectSemanticMemoryService:
    """生产 Project Memory 授权与 lifecycle authority。"""
    def __init__(self, store: ProjectSemanticMemoryStore, private_store: AdvancedMemoryStore, *, private_authorizer: MemoryAccessAuthorizer | None = None) -> None:
        self._store, self._private_store = store, private_store
        self._private_authorizer = private_authorizer or MemoryAccessAuthorizer()
    @staticmethod
    def _auth(operation: str, requester: MemoryAccessPrincipal | None, project: ProjectIdentity | None, grant: ProjectMemoryGrant | None, permission: ProjectMemoryPermission) -> ProjectMemoryAuthorizationResult:
        if project is None: return _deny(operation, requester.agent_id if requester else None, None, permission, ProjectMemoryReason.PROJECT_IDENTITY_MISSING)
        if not isinstance(requester, MemoryAccessPrincipal) or grant is None or grant.agent_id != requester.agent_id or grant.project_id != project.project_id or permission not in grant.permissions:
            reasons = {ProjectMemoryPermission.READ: ProjectMemoryReason.PROJECT_READ_DENIED, ProjectMemoryPermission.WRITE: ProjectMemoryReason.PROJECT_WRITE_DENIED, ProjectMemoryPermission.FORGET: ProjectMemoryReason.PROJECT_FORGET_DENIED, ProjectMemoryPermission.PROMOTE: ProjectMemoryReason.PROJECT_PROMOTION_DENIED}
            return _deny(operation, requester.agent_id if requester else None, project.project_id, permission, reasons[permission])
        return _allow(operation, requester.agent_id, project.project_id, permission)
    def read(self, *, requester: MemoryAccessPrincipal | None, project: ProjectIdentity | None, grant: ProjectMemoryGrant | None) -> tuple[tuple[ProjectSemanticRecord, ...], ProjectMemoryAuthorizationResult]:
        auth = self._auth("READ", requester, project, grant, ProjectMemoryPermission.READ)
        return (self._store.active(project.project_id) if auth.allowed and project else (), auth)
    def write(self, *, requester: MemoryAccessPrincipal | None, project: ProjectIdentity | None, grant: ProjectMemoryGrant | None, logical_key: str, canonical_text: str, payload: dict[str, object], run_id: str, supersede: bool = False) -> ProjectMemoryMutation:
        auth = self._auth("WRITE", requester, project, grant, ProjectMemoryPermission.WRITE)
        if not auth.allowed or project is None or not logical_key or not canonical_text or not isinstance(payload, dict): return ProjectMemoryMutation("DENIED", None, auth)
        record = ProjectSemanticRecord(str(uuid4()), project.project_id, logical_key, canonical_text, payload, 'ACTIVE', requester.agent_id, run_id, requester.agent_id, requester.agent_id)
        return self._store.mutate(record, supersede=supersede)
    def forget(self, *, requester: MemoryAccessPrincipal | None, project: ProjectIdentity | None, grant: ProjectMemoryGrant | None, logical_key: str) -> ProjectMemoryAuthorizationResult:
        auth = self._auth("FORGET", requester, project, grant, ProjectMemoryPermission.FORGET)
        if not auth.allowed or project is None or not logical_key: return auth
        count = self._store.forget(project.project_id, logical_key, requester.agent_id)
        return replace(auth, affected_count=count)
    def promote(self, *, requester: MemoryAccessPrincipal | None, project: ProjectIdentity | None, grant: ProjectMemoryGrant | None, source_memory_id: str, source_owner_agent_id: str, source_scope: str, run_id: str, supersede: bool = False) -> ProjectMemoryMutation:
        auth = self._auth("PROMOTE", requester, project, grant, ProjectMemoryPermission.PROMOTE)
        write = self._auth("WRITE", requester, project, grant, ProjectMemoryPermission.WRITE)
        if not auth.allowed or not write.allowed or project is None: return ProjectMemoryMutation("DENIED", None, auth if not auth.allowed else write)
        private = self._private_authorizer.authorize_private_read(requester, source_owner_agent_id, source_scope, requested_memory_scope=source_scope)
        if not private.allowed: return ProjectMemoryMutation("DENIED", None, _deny("PROMOTE", requester.agent_id if requester else None, project.project_id, ProjectMemoryPermission.PROMOTE, ProjectMemoryReason.PRIVATE_SOURCE_DENIED))
        try: source = self._private_store.get_by_memory_id(source_memory_id)
        except Exception: return ProjectMemoryMutation("DENIED", None, _deny("PROMOTE", requester.agent_id if requester else None, project.project_id, ProjectMemoryPermission.PROMOTE, ProjectMemoryReason.PRIVATE_SOURCE_DENIED))
        if source.agent_id != source_owner_agent_id or source.memory_scope != source_scope or source.memory_type is not MemoryType.SEMANTIC or source.status is not MemoryStatus.ACTIVE or source.logical_key is None:
            return ProjectMemoryMutation("DENIED", None, _deny("PROMOTE", requester.agent_id, project.project_id, ProjectMemoryPermission.PROMOTE, ProjectMemoryReason.PRIVATE_SOURCE_DENIED))
        record = ProjectSemanticRecord(str(uuid4()), project.project_id, source.logical_key, source.canonical_text, source.payload, 'ACTIVE', source.origin.origin_agent_id, source.origin.origin_run_id, requester.agent_id, requester.agent_id, source.memory_id, source.agent_id, requester.agent_id, run_id, datetime.now(UTC).isoformat())
        return self._store.mutate(record, supersede=supersede)
