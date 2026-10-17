"""受信 Memory 身份与有界来源合同；不从模型或 Agent 身份推导用户。"""

from dataclasses import dataclass
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

from core.runtime.execution_aggregate import canonical_payload_digest


class MemoryContextError(RuntimeError):
    def __init__(self, code):
        self.error_code = code
        super().__init__(code)


@dataclass(frozen=True)
class MemoryAccessContext:
    tenant_id: str
    user_id: str
    agent_id: str
    session_id: str
    principal_kind: str = "HUMAN"
    session_expires_at: datetime | None = None

    def __post_init__(self):
        if (any(not isinstance(v, str) or not v.strip() or len(v) > 255
                for v in (self.tenant_id, self.user_id, self.agent_id, self.session_id))
                or self.principal_kind not in {"HUMAN", "SERVICE"}):
            raise MemoryContextError("MEMORY_ACCESS_DENIED")

    @classmethod
    def from_principal(cls, principal, agent_id, session_id=None):
        from core.auth import Principal
        if not isinstance(principal, Principal) or not principal.tenant_id:
            raise MemoryContextError("MEMORY_PRINCIPAL_REQUIRED")
        return cls(principal.tenant_id, str(principal.user_id), agent_id, session_id or "default", principal.principal_kind)

    @property
    def conversation_id(self):
        return self.scope_id("SESSION")

    def to_dict(self):
        from dataclasses import asdict
        result = asdict(self)
        result["session_expires_at"] = self.session_expires_at.isoformat() if self.session_expires_at else None
        return result

    @classmethod
    def from_dict(cls, value):
        copied = dict(value)
        if copied.get("session_expires_at"):
            copied["session_expires_at"] = datetime.fromisoformat(copied["session_expires_at"])
        return cls(**copied)

    def scope_id(self, kind, project_id=None):
        if kind not in {"SESSION", "USER", "PROJECT"} or (kind == "PROJECT" and not project_id):
            raise MemoryContextError("MEMORY_SCOPE_INVALID")
        key = [self.tenant_id, kind, self.agent_id, project_id if kind == "PROJECT" else self.user_id,
            self.session_id if kind == "SESSION" else None]
        return str(uuid5(NAMESPACE_URL, "memory-scope:" + canonical_payload_digest(key)))


def bounded_dependencies(values):
    """拒绝权限依赖截断；稳定去重，不抹去版本差异。"""
    if not isinstance(values, (list, tuple)) or len(values) > 128:
        raise MemoryContextError("MEMORY_DEPENDENCIES_UNVERIFIABLE")
    for value in values:
        if (not isinstance(value, dict) or value.get("kind") not in {"RECORD", "EXCHANGE", "USER_MESSAGE", "SCOPE"}
                or not isinstance(value.get("scope_id"), str) or type(value.get("epoch")) is not int
                or value["epoch"] < 1):
            raise MemoryContextError("MEMORY_DEPENDENCIES_UNVERIFIABLE")
        if value["kind"] != "SCOPE" and (not isinstance(value.get("id"), str)
                or type(value.get("version")) is not int or value["version"] < 1
                or not isinstance(value.get("digest"), str) or len(value["digest"]) != 64):
            raise MemoryContextError("MEMORY_DEPENDENCIES_UNVERIFIABLE")
    result = {canonical_payload_digest(v): dict(v) for v in values}
    return [result[key] for key in sorted(result)]
