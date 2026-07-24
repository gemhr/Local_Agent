#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""内置工具注册模块。"""

from tools.complex_workflow_simulator import complex_workflow_simulator
from tools.local_tools import analyze_excel_data, get_system_status, list_files_in_dir
from core.runtime.tool_adapters import (
    ComplexWorkflowToolAdapter,
    LegacyStringToolAdapter,
)


def register_all_tools(router) -> None:
    """将全部内置工具注册到路由器。

    Args:
        router: 提供 ``register_tool`` 方法的路由器实例。
    """
    router.register_tool(
        name="list_files",
        func=list_files_in_dir,
        description="List files in a local directory. Argument: directory path.",
    )
    router.register_tool(
        name="analyze_excel",
        func=analyze_excel_data,
        description="Analyze a local Excel or CSV file and summarize the data.",
    )
    router.register_tool(
        name="get_system_status",
        func=get_system_status,
        description="Return basic local system status. No arguments required.",
    )
    if hasattr(router, "attach_tool_adapter"):
        router.attach_tool_adapter(
            "get_system_status",
            LegacyStringToolAdapter(
            tool_name="get_system_status",
            function=get_system_status,
            default_timeout_seconds=3.0,
            max_output_bytes=4096,
            max_concurrency=2,
            ),
        )
    router.register_tool(
        name="complex_workflow_simulator",
        func=complex_workflow_simulator,
        description=(
            "Run a deterministic local batch-workflow simulation. "
            "Argument: one JSON object containing operation_id, resource_key, "
            "idempotency_key, execution_mode, items, failure_injection, "
            "processing_options, and metadata. "
            "NON_IDEMPOTENT_SIMULATION must only be selected explicitly."
        ),
    )
    if hasattr(router, "attach_tool_adapter"):
        router.attach_tool_adapter(
            "complex_workflow_simulator", ComplexWorkflowToolAdapter()
        )
