#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""内置工具注册模块（production population helper，不拥有 Registry）。"""

from core.runtime.tool_adapters import (
    ComplexWorkflowToolAdapter,
    LegacyStringToolAdapter,
)
from core.runtime.tool_registry import ToolDescriptor, ToolRegistration
from tools.local_tools import analyze_excel_data, get_system_status, list_files_in_dir


def register_all_tools(tool_registry) -> None:
    """将全部内置 Tool 注册到 ToolRegistry。

    Args:
        tool_registry: startup builder 状态的 ToolRegistry；注册完成后由调用方 freeze。
    """
    tool_registry.register(
        ToolRegistration(
            descriptor=ToolDescriptor(
                name="list_files",
                description="List files in a local directory. Argument: directory path.",
            ),
            adapter=LegacyStringToolAdapter(
                tool_name="list_files",
                function=list_files_in_dir,
                error_prefixes=("Path does not exist:", "List files failed:"),
            ),
        )
    )
    tool_registry.register(
        ToolRegistration(
            descriptor=ToolDescriptor(
                name="analyze_excel",
                description="Analyze a local Excel or CSV file and summarize the data.",
            ),
            adapter=LegacyStringToolAdapter(
                tool_name="analyze_excel",
                function=analyze_excel_data,
                error_prefixes=("File not found:", "Excel analysis failed:"),
            ),
        )
    )
    tool_registry.register(
        ToolRegistration(
            descriptor=ToolDescriptor(
                name="get_system_status",
                description="Return basic local system status. No arguments required.",
            ),
            adapter=LegacyStringToolAdapter(
                tool_name="get_system_status",
                function=get_system_status,
                default_timeout_seconds=3.0,
                max_output_bytes=4096,
                max_concurrency=2,
            ),
        )
    )
    tool_registry.register(
        ToolRegistration(
            descriptor=ToolDescriptor(
                name="complex_workflow_simulator",
                description=(
                    "Run a deterministic local batch-workflow simulation. "
                    "Argument: one JSON object containing operation_id, resource_key, "
                    "idempotency_key, execution_mode, items, failure_injection, "
                    "processing_options, and metadata. "
                    "NON_IDEMPOTENT_SIMULATION must only be selected explicitly."
                ),
            ),
            adapter=ComplexWorkflowToolAdapter(),
        )
    )
