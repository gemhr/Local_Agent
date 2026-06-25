"""Offline evaluation toolkit for Local Agent."""

from .dataset import load_dataset, save_dataset
from .metrics import compute_metrics
from .report import build_markdown_report
from .runner import EvalRunner
from .schema import EvalSample, EvalRunRecord

__all__ = [
    "load_dataset",
    "save_dataset",
    "compute_metrics",
    "build_markdown_report",
    "EvalRunner",
    "EvalSample",
    "EvalRunRecord",
]
