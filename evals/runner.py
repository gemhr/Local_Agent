#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""评估批跑器。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from urllib import request

from evals.schema import EvalRunRecord, EvalSample


class AgentClient:
    """抽象的 Agent 调用客户端。"""

    def run(self, sample: EvalSample, *, variant: str) -> EvalRunRecord:
        raise NotImplementedError


@dataclass
class HttpAgentClient(AgentClient):
    """通过本地 FastAPI 接口调用 Agent。"""

    api_base_url: str
    agent_id: str = "core_router"
    timeout_seconds: float = 120.0

    def run(self, sample: EvalSample, *, variant: str) -> EvalRunRecord:
        start = time.perf_counter()
        req_body = json.dumps(
            {"agent_id": self.agent_id, "query": sample.user_query, "file_path": ""},
            ensure_ascii=False,
        ).encode("utf-8")
        req = request.Request(
            f"{self.api_base_url.rstrip('/')}/api/chat",
            data=req_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=self.timeout_seconds) as response:
            raw_bytes = response.read()
        raw_text = raw_bytes.decode("utf-8", errors="ignore")

        answer, metadata = self._parse_response(raw_text)
        latency_ms = (time.perf_counter() - start) * 1000
        citation_hits = [doc for doc in metadata.get("retrieved_docs", []) if doc in sample.expected_citations]
        return EvalRunRecord(
            sample_id=sample.sample_id,
            variant=variant,
            latency_ms=latency_ms,
            answer=answer,
            predicted_agent=metadata.get("predicted_agent"),
            used_tools=list(metadata.get("used_tools", [])),
            retrieved_docs=list(metadata.get("retrieved_docs", [])),
            citation_hits=citation_hits,
            metadata=metadata,
        )

    @staticmethod
    def _parse_response(raw_text: str) -> tuple[str, dict]:
        """从聊天文本解析输出和结构化元数据。"""
        meta_prefix = "[[EVAL_META]]"
        lines = raw_text.splitlines()
        metadata: dict = {}
        answer_lines: list[str] = []
        for line in lines:
            if line.startswith(meta_prefix):
                try:
                    metadata = json.loads(line[len(meta_prefix) :].strip())
                except json.JSONDecodeError:
                    metadata = {}
                continue
            if line.startswith("[[ORCH]]"):
                continue
            answer_lines.append(line)
        return "\n".join(answer_lines).strip(), metadata


@dataclass
class ReplayAgentClient(AgentClient):
    """从预先生成的 JSONL 回放结果，用于离线对比。"""

    replay_file: str

    def __post_init__(self) -> None:
        replay_path = Path(self.replay_file)
        self._records = {}
        for line in replay_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            self._records[row["sample_id"]] = row

    def run(self, sample: EvalSample, *, variant: str) -> EvalRunRecord:
        payload = self._records.get(sample.sample_id)
        if payload is None:
            return EvalRunRecord(sample_id=sample.sample_id, variant=variant, answer="", metadata={"missing": True})
        payload = dict(payload)
        payload["variant"] = variant
        return EvalRunRecord.from_dict(payload)


class EvalRunner:
    """批量运行评估。"""

    def __init__(self, client: AgentClient) -> None:
        self.client = client

    def run_batch(self, samples: list[EvalSample], *, variant: str) -> list[EvalRunRecord]:
        outputs: list[EvalRunRecord] = []
        for sample in samples:
            outputs.append(self.client.run(sample, variant=variant))
        return outputs
