#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tool Catalog、确定性 Discovery 与 durable Run Tool Snapshot 合同。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
import hashlib
import json
import re
import time
from types import MappingProxyType
from typing import Iterable, Mapping, Protocol
from uuid import uuid4

from core.runtime.tool_governance import (
    ToolGovernanceContext,
    ToolGovernanceOutcome,
    ToolGovernanceService,
)
from core.runtime.tool_registry import ToolDescriptor, ToolRegistration, ToolRegistry


TOOL_SELECTION_ALGORITHM_VERSION = "metadata-keyword-v1"
TOOL_SNAPSHOT_SCHEMA_VERSION = 1


class MetricsRecorder(Protocol):
    def increment_counter(self, name: str, value: float = 1, *, labels=None) -> None: ...
    def observe_histogram(self, name: str, value: float, *, labels=None) -> None: ...


def _digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _provider_facts(registration: ToolRegistration) -> tuple[str, str, str]:
    """返回不含 session generation 的 durable provider identity。"""
    adapter = registration.adapter
    server_id = getattr(adapter, "server_id", None)
    remote_name = getattr(adapter, "remote_name", None)
    if isinstance(server_id, str) and server_id and isinstance(remote_name, str) and remote_name:
        return "mcp", server_id, remote_name
    provider = f"{adapter.__class__.__module__}.{adapter.__class__.__qualname__}"
    return "local", provider, registration.descriptor.name


def _schema_digest(registration: ToolRegistration) -> str:
    provider_kind, provider_identity, remote_tool_id = _provider_facts(registration)
    return _digest({
        "canonical_tool_id": registration.descriptor.name,
        "provider_kind": provider_kind,
        "provider_identity": provider_identity,
        "remote_tool_id": remote_tool_id,
        "input_schema": registration.adapter.llm_input_schema(),
    })


def _descriptor_digest(registration: ToolRegistration) -> str:
    descriptor = registration.descriptor
    return _digest({
        "name": descriptor.name,
        "description": descriptor.description,
        "llm_instructions": descriptor.llm_instructions,
    })


@dataclass(frozen=True, slots=True)
class ToolResolutionItem:
    tool_name: str
    provider_kind: str
    provider_identity: str
    remote_tool_id: str
    schema_digest: str
    descriptor_digest: str

    @classmethod
    def from_registration(cls, registration: ToolRegistration) -> "ToolResolutionItem":
        provider_kind, provider_identity, remote_tool_id = _provider_facts(registration)
        return cls(
            tool_name=registration.descriptor.name,
            provider_kind=provider_kind,
            provider_identity=provider_identity,
            remote_tool_id=remote_tool_id,
            schema_digest=_schema_digest(registration),
            descriptor_digest=_descriptor_digest(registration),
        )

    def identity_dict(self) -> dict[str, str]:
        return {
            "tool_name": self.tool_name,
            "provider_kind": self.provider_kind,
            "provider_identity": self.provider_identity,
            "remote_tool_id": self.remote_tool_id,
            "schema_digest": self.schema_digest,
            "descriptor_digest": self.descriptor_digest,
        }


def _catalog_digest(registrations: Iterable[ToolRegistration]) -> str:
    items = sorted(
        (ToolResolutionItem.from_registration(item).identity_dict() for item in registrations),
        key=lambda item: item["tool_name"],
    )
    return _digest(items)


class ToolCatalog:
    """ToolRegistry 的只读 Catalog Adapter，不复制 Tool Truth。"""

    def __init__(self, registry: ToolRegistry) -> None:
        if not isinstance(registry, ToolRegistry) or not registry.frozen:
            raise TypeError("ToolCatalog 需要已冻结 ToolRegistry")
        self._registry = registry

    def list_available_tools(self) -> tuple[ToolRegistration, ...]:
        return self._registry.registrations()

    def get_tool_descriptor(self, name: str) -> ToolDescriptor | None:
        registration = self._registry.resolve(name)
        return registration.descriptor if registration is not None else None

    @property
    def catalog_digest(self) -> str:
        return _catalog_digest(self._registry.registrations())


def _snapshot_digest_payload(*, run_id: str, registry_digest: str,
                             selection_algorithm_version: str,
                             snapshot_schema_version: int,
                             selection_query_digest: str,
                             tools: Iterable[ToolResolutionItem]) -> dict[str, object]:
    return {
        "run_id": run_id,
        "registry_digest": registry_digest,
        "selection_algorithm_version": selection_algorithm_version,
        "snapshot_schema_version": snapshot_schema_version,
        "selection_query_digest": selection_query_digest,
        "tools": [item.identity_dict() for item in tools],
    }


@dataclass(frozen=True, slots=True)
class ToolResolutionSnapshot:
    snapshot_id: str
    run_id: str
    created_at: datetime
    tools: tuple[ToolResolutionItem, ...]
    registry_digest: str
    selection_algorithm_version: str
    snapshot_schema_version: int
    selection_query_digest: str
    snapshot_digest: str
    _registrations: Mapping[str, ToolRegistration] = field(
        repr=False, compare=False, default_factory=dict
    )

    def __post_init__(self) -> None:
        if not self.snapshot_id or not self.run_id:
            raise ValueError("snapshot_id 和 run_id 必须非空")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at 必须带时区")
        if self.snapshot_schema_version != TOOL_SNAPSHOT_SCHEMA_VERSION:
            raise ValueError("不支持的 ToolResolutionSnapshot schema version")
        names = tuple(item.tool_name for item in self.tools)
        if len(names) != len(set(names)):
            raise ValueError("ToolResolutionSnapshot 不允许重复 Tool identity")
        expected = _digest(_snapshot_digest_payload(
            run_id=self.run_id,
            registry_digest=self.registry_digest,
            selection_algorithm_version=self.selection_algorithm_version,
            snapshot_schema_version=self.snapshot_schema_version,
            selection_query_digest=self.selection_query_digest,
            tools=self.tools,
        ))
        if expected != self.snapshot_digest:
            raise ValueError("ToolResolutionSnapshot digest 不匹配")
        object.__setattr__(self, "_registrations", MappingProxyType(dict(self._registrations)))

    def resolve(self, name: str) -> ToolRegistration | None:
        return self._registrations.get(name)

    def registrations(self) -> tuple[ToolRegistration, ...]:
        return tuple(self._registrations[item.tool_name] for item in self.tools)

    def to_dict(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "run_id": self.run_id,
            "created_at": self.created_at.isoformat(),
            "registry_digest": self.registry_digest,
            "selection_algorithm_version": self.selection_algorithm_version,
            "snapshot_schema_version": self.snapshot_schema_version,
            "selection_query_digest": self.selection_query_digest,
            "snapshot_digest": self.snapshot_digest,
            "tools": [item.identity_dict() for item in self.tools],
        }


class ToolSnapshotCompatibilityCode(str, Enum):
    TOOL_MISSING = "TOOL_SNAPSHOT_TOOL_MISSING"
    SCHEMA_DRIFT = "TOOL_SNAPSHOT_SCHEMA_DRIFT"
    PROVIDER_DRIFT = "TOOL_SNAPSHOT_PROVIDER_DRIFT"
    DESCRIPTOR_DRIFT = "TOOL_SNAPSHOT_DESCRIPTOR_DRIFT"


class ToolSnapshotCompatibilityError(RuntimeError):
    def __init__(self, error_code: ToolSnapshotCompatibilityCode) -> None:
        self.error_code = error_code
        super().__init__(error_code.value)


def hydrate_tool_snapshot(snapshot: ToolResolutionSnapshot,
                          registry: ToolRegistry) -> ToolResolutionSnapshot:
    """按 durable identity 解析当前 executable registration；任一漂移即关闭恢复。"""
    registrations: dict[str, ToolRegistration] = {}
    for frozen_item in snapshot.tools:
        registration = registry.resolve(frozen_item.tool_name)
        if registration is None:
            raise ToolSnapshotCompatibilityError(ToolSnapshotCompatibilityCode.TOOL_MISSING)
        current = ToolResolutionItem.from_registration(registration)
        if (current.provider_kind != frozen_item.provider_kind
                or current.provider_identity != frozen_item.provider_identity
                or current.remote_tool_id != frozen_item.remote_tool_id):
            raise ToolSnapshotCompatibilityError(ToolSnapshotCompatibilityCode.PROVIDER_DRIFT)
        if current.schema_digest != frozen_item.schema_digest:
            raise ToolSnapshotCompatibilityError(ToolSnapshotCompatibilityCode.SCHEMA_DRIFT)
        if current.descriptor_digest != frozen_item.descriptor_digest:
            raise ToolSnapshotCompatibilityError(ToolSnapshotCompatibilityCode.DESCRIPTOR_DRIFT)
        registrations[frozen_item.tool_name] = registration
    return ToolResolutionSnapshot(
        snapshot_id=snapshot.snapshot_id, run_id=snapshot.run_id,
        created_at=snapshot.created_at, tools=snapshot.tools,
        registry_digest=snapshot.registry_digest,
        selection_algorithm_version=snapshot.selection_algorithm_version,
        snapshot_schema_version=snapshot.snapshot_schema_version,
        selection_query_digest=snapshot.selection_query_digest,
        snapshot_digest=snapshot.snapshot_digest, _registrations=registrations,
    )


def create_tool_snapshot(run_id: str, tools: Iterable[ToolRegistration], *,
                         registry_digest: str | None = None,
                         selection_query: str = "", snapshot_id: str | None = None,
                         created_at: datetime | None = None,
                         metrics_recorder: MetricsRecorder | None = None) -> ToolResolutionSnapshot:
    registrations = tuple(tools)
    items = tuple(ToolResolutionItem.from_registration(item) for item in registrations)
    query_digest = _digest({"normalized_query": " ".join(selection_query.lower().split())})
    active_registry_digest = registry_digest or _catalog_digest(registrations)
    digest = _digest(_snapshot_digest_payload(
        run_id=run_id, registry_digest=active_registry_digest,
        selection_algorithm_version=TOOL_SELECTION_ALGORITHM_VERSION,
        snapshot_schema_version=TOOL_SNAPSHOT_SCHEMA_VERSION,
        selection_query_digest=query_digest, tools=items,
    ))
    snapshot = ToolResolutionSnapshot(
        snapshot_id=snapshot_id or uuid4().hex, run_id=run_id,
        created_at=created_at or datetime.now(UTC), tools=items,
        registry_digest=active_registry_digest,
        selection_algorithm_version=TOOL_SELECTION_ALGORITHM_VERSION,
        snapshot_schema_version=TOOL_SNAPSHOT_SCHEMA_VERSION,
        selection_query_digest=query_digest, snapshot_digest=digest,
        _registrations={item.descriptor.name: item for item in registrations},
    )
    if metrics_recorder is not None:
        metrics_recorder.increment_counter("runtime_tool_snapshot_created_total")
    return snapshot


_TOKEN_PATTERN = re.compile(r"[\w]+", re.UNICODE)


class ToolDiscovery:
    """基于 metadata 的确定性候选选择器；不执行 Tool，也不拥有授权。"""

    def __init__(self, catalog: ToolCatalog, *, top_k: int = 10,
                 governance: ToolGovernanceService | None = None,
                 metrics_recorder: MetricsRecorder | None = None) -> None:
        if not 1 <= top_k <= 100:
            raise ValueError("top_k 必须位于 1..100")
        self.catalog = catalog
        self.top_k = top_k
        self.governance = governance
        self.metrics_recorder = metrics_recorder

    def discover_tools(self, query: str, principal: str | ToolGovernanceContext,
                       context: object | None = None) -> tuple[ToolRegistration, ...]:
        started = time.perf_counter()
        query_tokens = tuple(token.lower() for token in _TOKEN_PATTERN.findall(query or ""))
        governance_context = principal if isinstance(principal, ToolGovernanceContext) else None
        if governance_context is None and isinstance(principal, str):
            governance_context = ToolGovernanceContext(
                principal_agent_id=principal,
                run_id=getattr(context, "run_id", "discovery"), step_id="tool-discovery")
        scored: list[tuple[int, str, ToolRegistration]] = []
        for registration in self.catalog.list_available_tools():
            if self.governance is not None:
                decision = self.governance.authorize_tool(governance_context, registration)
                if decision.outcome is not ToolGovernanceOutcome.ALLOW:
                    continue
            descriptor = registration.descriptor
            fields = (descriptor.name.lower(), descriptor.description.lower(),
                      descriptor.llm_instructions.lower())
            score = 0
            for token in query_tokens:
                if token == fields[0]: score += 100
                elif token in fields[0]: score += 50
                if token in fields[1]: score += 10
                if token in fields[2]: score += 5
            if score > 0:
                scored.append((score, descriptor.name, registration))
        result = tuple(item[2] for item in sorted(
            scored, key=lambda item: (-item[0], item[1]))[:self.top_k])
        if self.metrics_recorder is not None:
            self.metrics_recorder.observe_histogram(
                "runtime_tool_discovery_latency_seconds", time.perf_counter() - started)
            self.metrics_recorder.increment_counter(
                "runtime_tool_discovery_candidate_count_total", len(result))
        return result

    def create_tool_snapshot(self, run_id: str, tools: Iterable[ToolRegistration], *,
                             selection_query: str = "") -> ToolResolutionSnapshot:
        return create_tool_snapshot(
            run_id, tools, registry_digest=self.catalog.catalog_digest,
            selection_query=selection_query, metrics_recorder=self.metrics_recorder)


__all__ = [
    "TOOL_SELECTION_ALGORITHM_VERSION", "TOOL_SNAPSHOT_SCHEMA_VERSION",
    "ToolCatalog", "ToolDiscovery", "ToolResolutionItem", "ToolResolutionSnapshot",
    "ToolSnapshotCompatibilityCode", "ToolSnapshotCompatibilityError",
    "create_tool_snapshot", "hydrate_tool_snapshot",
]
