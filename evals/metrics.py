#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""评估指标计算。"""

from __future__ import annotations

from collections import defaultdict

from evals.schema import EvalRunRecord, EvalSample


def _safe_ratio(hit: int, total: int) -> float:
    return 0.0 if total == 0 else hit / total


def compute_metrics(samples: list[EvalSample], records: list[EvalRunRecord], *, recall_k: int = 3) -> dict:
    """计算核心离线评估指标。"""
    sample_map = {item.sample_id: item for item in samples}

    citation_hit = 0
    citation_total = 0
    router_hit = 0
    router_total = 0
    tool_hit = 0
    tool_total = 0
    final_pass = 0
    total = len(records)
    latency_total = 0.0
    recall_hit = 0
    recall_total = 0
    bad_cases = []

    by_category = defaultdict(lambda: {"pass": 0, "total": 0})

    for record in records:
        sample = sample_map.get(record.sample_id)
        if sample is None:
            continue

        latency_total += record.latency_ms
        category = sample.category
        by_category[category]["total"] += 1

        expected_citations = sample.expected_citations
        retrieved_topk = record.retrieved_docs[:recall_k]
        if expected_citations:
            recall_total += 1
            if any(item in retrieved_topk for item in expected_citations):
                recall_hit += 1

            citation_total += 1
            if any(item in record.citation_hits for item in expected_citations):
                citation_hit += 1

        if sample.expected_agent:
            router_total += 1
            if record.predicted_agent == sample.expected_agent:
                router_hit += 1

        if sample.expected_tools:
            tool_total += 1
            expected_set = set(sample.expected_tools)
            actual_set = set(record.used_tools)
            if expected_set.issubset(actual_set):
                tool_hit += 1

        pass_flag = _answer_pass(sample, record)
        if pass_flag:
            final_pass += 1
            by_category[category]["pass"] += 1
        else:
            bad_cases.append(
                {
                    "sample_id": sample.sample_id,
                    "category": sample.category,
                    "query": sample.user_query,
                    "expected_agent": sample.expected_agent,
                    "predicted_agent": record.predicted_agent,
                    "expected_tools": sample.expected_tools,
                    "used_tools": record.used_tools,
                    "expected_citations": sample.expected_citations,
                    "retrieved_docs": record.retrieved_docs,
                    "answer_preview": record.answer[:200],
                }
            )

    return {
        "num_samples": total,
        "recall_at_k": _safe_ratio(recall_hit, recall_total),
        "citation_hit_rate": _safe_ratio(citation_hit, citation_total),
        "router_accuracy": _safe_ratio(router_hit, router_total),
        "tool_call_accuracy": _safe_ratio(tool_hit, tool_total),
        "final_answer_pass_rate": _safe_ratio(final_pass, total),
        "avg_latency_ms": 0.0 if total == 0 else latency_total / total,
        "by_category": {
            key: _safe_ratio(value["pass"], value["total"])
            for key, value in sorted(by_category.items())
        },
        "bad_cases": bad_cases,
    }


def _answer_pass(sample: EvalSample, record: EvalRunRecord) -> bool:
    """可解释的回答通过规则（MVP 版本）。"""
    answer = record.answer.strip().lower()
    if not answer:
        return False

    if sample.reference_answer:
        hints = [token for token in sample.reference_answer.lower().split() if len(token) >= 4]
        if hints:
            overlap = sum(1 for token in hints if token in answer)
            if overlap == 0:
                return False

    if sample.expected_tools and not set(sample.expected_tools).issubset(set(record.used_tools)):
        return False

    if sample.expected_agent and record.predicted_agent and sample.expected_agent != record.predicted_agent:
        return False

    return True
