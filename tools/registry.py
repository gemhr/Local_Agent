#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""内置工具注册模块（production population helper，不拥有 Registry）。"""

from core.runtime.tool_adapters import (
    ComplexWorkflowToolAdapter,
    LegacyStringToolAdapter,
)
from core.runtime.tool_registry import ToolDescriptor, ToolRegistration
from tools.local_tools import (
    analyze_excel_data,
    get_system_status,
    list_files_in_dir,
    parse_filesystem_argument,
)


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
                llm_instructions="适用于用户要查看某个本地目录中的文件；参数只填目录路径。",
            ),
            adapter=LegacyStringToolAdapter(
                tool_name="list_files",
                function=list_files_in_dir,
                error_prefixes=("Path does not exist:", "List files failed:"),
                argument_parser=parse_filesystem_argument,
            ),
        )
    )
    tool_registry.register(
        ToolRegistration(
            descriptor=ToolDescriptor(
                name="analyze_excel",
                description="Analyze a local Excel or CSV file and summarize the data.",
                llm_instructions="适用于用户要分析本地 Excel 或 CSV；参数只填文件路径。",
            ),
            adapter=LegacyStringToolAdapter(
                tool_name="analyze_excel",
                function=analyze_excel_data,
                error_prefixes=("File not found:", "Excel analysis failed:"),
                argument_parser=parse_filesystem_argument,
            ),
        )
    )
    tool_registry.register(
        ToolRegistration(
            descriptor=ToolDescriptor(
                name="get_system_status",
                description="Return basic local system status. No arguments required.",
                llm_instructions="适用于用户询问本机基本系统状态；使用空参数。",
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
                    "Run a deterministic local batch-workflow simulation."
                ),
                llm_instructions=(
                    "适用于用户要求对某资源中的一个或多个项目进行预演或模拟变更；"
                    "不适用于解释概念或普通聊天。参数使用 JSON，只提取业务字段："
                    "resource_key，以及 items 数组（每项含 item_id、action、quantity）。"
                    "用户表示“只预演”时 execution_mode 用 DRY_RUN；表示真实模拟副作用时用 "
                    "NON_IDEMPOTENT_SIMULATION；要求可安全重复提交时用 IDEMPOTENT_COMMIT。"
                    "operation_id 和幂等键由系统补齐；不要填写内部测试、故障注入或运行时字段。"
                    "示例：{\"resource_key\":\"demo-resource\",\"execution_mode\":"
                    "\"NON_IDEMPOTENT_SIMULATION\",\"items\":[{\"item_id\":\"item-1\","
                    "\"action\":\"ADD\",\"quantity\":1}]}。"
                ),
            ),
            adapter=ComplexWorkflowToolAdapter(),
        )
    )
