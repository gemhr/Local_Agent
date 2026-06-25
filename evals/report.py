#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""评估报告输出。"""

from __future__ import annotations

from datetime import datetime, timezone


def _fmt_pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def build_markdown_report(
    baseline_metrics: dict,
    candidate_metrics: dict,
    *,
    baseline_name: str,
    candidate_name: str,
    dataset_path: str,
) -> str:
    """构建对比评估 markdown 报告。"""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    rows = [
        ("Recall@K", baseline_metrics["recall_at_k"], candidate_metrics["recall_at_k"]),
        ("引用命中率", baseline_metrics["citation_hit_rate"], candidate_metrics["citation_hit_rate"]),
        ("Router 正确率", baseline_metrics["router_accuracy"], candidate_metrics["router_accuracy"]),
        ("Tool Call 正确率", baseline_metrics["tool_call_accuracy"], candidate_metrics["tool_call_accuracy"]),
        ("最终回答通过率", baseline_metrics["final_answer_pass_rate"], candidate_metrics["final_answer_pass_rate"]),
    ]

    lines = [
        "# Local Agent 离线评估报告",
        "",
        f"- 生成时间：{now}",
        f"- 数据集：`{dataset_path}`",
        f"- 样本数：{candidate_metrics['num_samples']}",
        f"- 对比版本：`{baseline_name}` vs `{candidate_name}`",
        "",
        "## 核心指标对比",
        "",
        "| 指标 | 基线 | 候选 | 差值 |",
        "|---|---:|---:|---:|",
    ]

    for name, b, c in rows:
        lines.append(f"| {name} | {_fmt_pct(b)} | {_fmt_pct(c)} | {_fmt_pct(c - b)} |")

    lines.extend(
        [
            f"| 平均响应时间(ms) | {baseline_metrics['avg_latency_ms']:.2f} | {candidate_metrics['avg_latency_ms']:.2f} | {candidate_metrics['avg_latency_ms'] - baseline_metrics['avg_latency_ms']:.2f} |",
            "",
            "## 分类通过率（候选版本）",
            "",
            "| 类别 | 通过率 |",
            "|---|---:|",
        ]
    )

    for category, pass_rate in candidate_metrics.get("by_category", {}).items():
        lines.append(f"| {category} | {_fmt_pct(pass_rate)} |")

    lines.extend(["", "## Bad Cases（候选版本 Top 10）", ""])
    bad_cases = candidate_metrics.get("bad_cases", [])[:10]
    if not bad_cases:
        lines.append("- 无 bad case。")
    else:
        for case in bad_cases:
            lines.extend(
                [
                    f"### {case['sample_id']} ({case['category']})",
                    f"- Query: {case['query']}",
                    f"- 期望路由: {case['expected_agent']} / 实际路由: {case['predicted_agent']}",
                    f"- 期望工具: {case['expected_tools']} / 实际工具: {case['used_tools']}",
                    f"- 期望引用: {case['expected_citations']}",
                    f"- 实际检索: {case['retrieved_docs'][:5]}",
                    f"- 回答预览: {case['answer_preview']}",
                    "",
                ]
            )

    return "\n".join(lines).rstrip() + "\n"
