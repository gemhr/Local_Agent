#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Local Agent 离线评估批跑入口。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evals.dataset import load_dataset
from evals.metrics import compute_metrics
from evals.report import build_markdown_report
from evals.runner import EvalRunner, HttpAgentClient, ReplayAgentClient


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run offline evaluation for Local Agent")
    parser.add_argument("--dataset", default="data/eval/samples.jsonl", help="评估数据集 JSONL 路径")

    parser.add_argument("--baseline-name", default="baseline")
    parser.add_argument("--candidate-name", default="candidate")

    parser.add_argument("--baseline-api", default=None, help="基线版本 API URL")
    parser.add_argument("--candidate-api", default=None, help="候选版本 API URL")

    parser.add_argument("--baseline-replay", default=None, help="基线回放 JSONL 文件")
    parser.add_argument("--candidate-replay", default=None, help="候选回放 JSONL 文件")

    parser.add_argument("--agent-id", default="core_router", help="批跑时默认 agent_id")
    parser.add_argument("--recall-k", type=int, default=3)
    parser.add_argument("--out-dir", default="data/eval/runs")
    return parser.parse_args()


def build_runner(api_url: str | None, replay_file: str | None, agent_id: str) -> EvalRunner:
    if replay_file:
        return EvalRunner(ReplayAgentClient(replay_file=replay_file))
    if api_url:
        return EvalRunner(HttpAgentClient(api_base_url=api_url, agent_id=agent_id))
    raise ValueError("需要指定 API 地址或 replay 文件")


def dump_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    args = parse_args()
    samples = load_dataset(args.dataset)

    baseline_runner = build_runner(args.baseline_api, args.baseline_replay, args.agent_id)
    candidate_runner = build_runner(args.candidate_api, args.candidate_replay, args.agent_id)

    baseline_records = baseline_runner.run_batch(samples, variant=args.baseline_name)
    candidate_records = candidate_runner.run_batch(samples, variant=args.candidate_name)

    baseline_metrics = compute_metrics(samples, baseline_records, recall_k=args.recall_k)
    candidate_metrics = compute_metrics(samples, candidate_records, recall_k=args.recall_k)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    baseline_file = out_dir / f"{args.baseline_name}_records.jsonl"
    candidate_file = out_dir / f"{args.candidate_name}_records.jsonl"
    report_file = out_dir / f"report_{args.baseline_name}_vs_{args.candidate_name}.md"

    dump_jsonl(baseline_file, [row.to_dict() for row in baseline_records])
    dump_jsonl(candidate_file, [row.to_dict() for row in candidate_records])

    report = build_markdown_report(
        baseline_metrics,
        candidate_metrics,
        baseline_name=args.baseline_name,
        candidate_name=args.candidate_name,
        dataset_path=args.dataset,
    )
    report_file.write_text(report, encoding="utf-8")

    print(f"[eval] baseline records: {baseline_file}")
    print(f"[eval] candidate records: {candidate_file}")
    print(f"[eval] report: {report_file}")


if __name__ == "__main__":
    main()
