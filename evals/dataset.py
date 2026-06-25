#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""评估数据集读写。"""

from __future__ import annotations

import json
from pathlib import Path

from evals.schema import EvalSample


def load_dataset(path: str | Path) -> list[EvalSample]:
    """从 JSONL 文件加载样本。"""
    samples: list[EvalSample] = []
    dataset_path = Path(path)
    for line_no, line in enumerate(dataset_path.read_text(encoding="utf-8").splitlines(), start=1):
        content = line.strip()
        if not content:
            continue
        payload = json.loads(content)
        samples.append(EvalSample.from_dict(payload))
    return samples


def save_dataset(path: str | Path, samples: list[EvalSample]) -> None:
    """保存样本为 JSONL 文件。"""
    dataset_path = Path(path)
    dataset_path.parent.mkdir(parents=True, exist_ok=True)
    rows = [json.dumps(sample.to_dict(), ensure_ascii=False) for sample in samples]
    dataset_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
