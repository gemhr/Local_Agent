#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""可供智能体调用的本地工具集合。"""

import os

import pandas as pd


def _clean_path(value: str) -> str:
    """清理模型生成的路径参数。

    Args:
        value: 原始路径字符串。

    Returns:
        str: 去除首尾空格和引号后的路径。
    """
    return value.strip().strip("'\"")


def list_files_in_dir(directory: str) -> str:
    """列出指定目录下的文件。

    Args:
        directory: 目录路径。

    Returns:
        str: 文件列表或错误信息。
    """
    try:
        directory = _clean_path(directory)
        if not os.path.exists(directory):
            return f"Path does not exist: {directory}"
        return f"Files in {directory}:\n" + "\n".join(os.listdir(directory))
    except Exception as exc:
        return f"List files failed: {exc}"


def analyze_excel_data(file_path: str) -> str:
    """读取 CSV 或 Excel 并返回摘要信息。

    Args:
        file_path: 表格文件路径。

    Returns:
        str: 分析摘要或错误信息。
    """
    try:
        file_path = _clean_path(file_path)
        if not os.path.exists(file_path):
            return f"File not found: {file_path}"

        if file_path.endswith(".csv"):
            frame = pd.read_csv(file_path)
        else:
            frame = pd.read_excel(file_path)

        info = {
            "rows": len(frame),
            "columns": list(frame.columns),
            "null_counts": frame.isnull().sum().to_dict(),
            "summary": frame.describe(include="all").fillna("").to_dict(),
        }
        return f"Analysis for {os.path.basename(file_path)}:\n{info}"
    except Exception as exc:
        return f"Excel analysis failed: {exc}"


def get_system_status(args: str = "") -> str:
    """返回当前机器的基础运行状态。

    Args:
        args: 占位参数，保留给工具调用接口。

    Returns:
        str: 系统、CPU 和内存信息。
    """
    import platform
    import psutil

    cpu_usage = psutil.cpu_percent(interval=1)
    memory = psutil.virtual_memory()
    return (
        f"system: {platform.system()} {platform.release()}\n"
        f"cpu_usage: {cpu_usage}%\n"
        f"available_memory_mb: {memory.available // (1024 ** 2)}"
    )
