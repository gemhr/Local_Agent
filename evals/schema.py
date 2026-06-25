#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""评估样本与运行结果结构定义。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class EvalSample:
    """单条评估样本。"""

    sample_id: str
    category: str
    user_query: str
    expected_citations: list[str] = field(default_factory=list)
    expected_tools: list[str] = field(default_factory=list)
    expected_agent: str | None = None
    reference_answer: str | None = None
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: dict) -> "EvalSample":
        return cls(
            sample_id=str(payload["sample_id"]),
            category=str(payload.get("category", "general")),
            user_query=str(payload.get("user_query", "")),
            expected_citations=[str(item) for item in payload.get("expected_citations", [])],
            expected_tools=[str(item) for item in payload.get("expected_tools", [])],
            expected_agent=payload.get("expected_agent"),
            reference_answer=payload.get("reference_answer"),
            tags=[str(item) for item in payload.get("tags", [])],
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EvalRunRecord:
    """单条样本在一次运行中的观测结果。"""

    sample_id: str
    variant: str
    latency_ms: float = 0.0
    answer: str = ""
    predicted_agent: str | None = None
    used_tools: list[str] = field(default_factory=list)
    retrieved_docs: list[str] = field(default_factory=list)
    citation_hits: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict) -> "EvalRunRecord":
        return cls(
            sample_id=str(payload["sample_id"]),
            variant=str(payload.get("variant", "unknown")),
            latency_ms=float(payload.get("latency_ms", 0.0)),
            answer=str(payload.get("answer", "")),
            predicted_agent=payload.get("predicted_agent"),
            used_tools=[str(item) for item in payload.get("used_tools", [])],
            retrieved_docs=[str(item) for item in payload.get("retrieved_docs", [])],
            citation_hits=[str(item) for item in payload.get("citation_hits", [])],
            metadata=dict(payload.get("metadata", {})),
        )

    def to_dict(self) -> dict:
        return asdict(self)
