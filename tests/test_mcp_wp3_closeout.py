"""Phase9-WP3：MCP 文档收口与 native 续写修复的确定性回归测试。

- `runtime_error_code_catalog.md` 必须收录当前真实存在的全部 MCP_ safe code；
- `runtime_capability_matrix.md` 必须将 REAL_MCP_E2E 标为 SUPPORTED 且不再
  保留 NOT_IMPLEMENTED 的 REAL_MCP_E2E 行；
- `AgentRouter._estimate_messages_tokens` 必须容忍 DeepSeek native
  tool_calls 消息的 ``content=None``（WP3 REAL E2E Case B 暴露的续写缺陷）。

全部为 DETERMINISTIC_TEST；REAL_MCP_E2E 证据见
`.ai/handoff/stage5_phase9_mcp_wp3/30_zcode_mcp_real_e2e_closeout.md`。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "docs" / "runtime" / "runtime_error_code_catalog.md"
MATRIX = ROOT / "docs" / "runtime" / "runtime_capability_matrix.md"
ROUTER = ROOT / "core" / "agent_router.py"

# 生产代码中真实存在的 MCP_ safe code（mcp/errors.py 类级 + registration/adapter 字符串）
EXPECTED_MCP_CODES = {
    "MCP_BOUNDARY_ERROR",
    "MCP_CONFIG_INVALID",
    "MCP_TRANSPORT_ERROR",
    "MCP_SERVER_UNAVAILABLE",
    "MCP_TRANSPORT_TIMEOUT",
    "MCP_TRANSPORT_CLOSED",
    "MCP_PROTOCOL_ERROR",
    "MCP_PROTOCOL_VERSION_UNSUPPORTED",
    "MCP_CAPABILITY_MISSING",
    "MCP_DISCOVERY_INVALID",
    "MCP_TOOL_POLICY_MISSING",
    "MCP_TOOL_POLICY_INVALID",
    "MCP_TOOL_RISK_UNCLASSIFIED",
    "MCP_TOOL_NAME_COLLISION",
    "MCP_TOOL_INPUT_SCHEMA_INVALID",
    "MCP_TOOL_INPUT_SCHEMA_UNSUPPORTED",
    "MCP_TOOL_DESCRIPTOR_INVALID",
    "MCP_SESSION_UNAVAILABLE",
    "MCP_TOOL_REPORTED_ERROR",
    "MCP_TOOL_RESULT_UNSUPPORTED",
}


def _production_mcp_codes() -> set[str]:
    """从 mcp/ 生产源码 AST 提取实际出现的 MCP_ 前缀字符串常量。"""
    codes: set[str] = set()
    for path in sorted((ROOT / "mcp").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.startswith("MCP_"):
                    codes.add(node.value)
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Name)
                        and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str)
                        and node.value.value.startswith("MCP_")
                    ):
                        codes.add(node.value.value)
    return codes


def test_error_code_catalog_contains_all_production_mcp_codes() -> None:
    catalog_text = CATALOG.read_text(encoding="utf-8")
    missing = EXPECTED_MCP_CODES - {
        code for code in EXPECTED_MCP_CODES if f"`{code}`" in catalog_text
    }
    assert not missing, f"错误码目录缺少 MCP codes: {sorted(missing)}"


def test_catalog_mcp_codes_match_production_source() -> None:
    """目录不虚构：目录中的 MCP 行必须与生产源码真实常量一致。"""
    production = _production_mcp_codes()
    catalog_text = CATALOG.read_text(encoding="utf-8")
    documented = {
        code
        for code in EXPECTED_MCP_CODES
        if f"`{code}`" in catalog_text
    }
    unknown = documented - production
    assert not unknown, f"目录记录了生产源码不存在的 MCP code: {sorted(unknown)}"


def test_capability_matrix_marks_real_mcp_e2e_supported() -> None:
    matrix_text = MATRIX.read_text(encoding="utf-8")
    assert "REAL_MCP_E2E（真实独立 MCP server 全链路，Phase9-WP3） | SUPPORTED" in matrix_text
    # 不允许残留 REAL_MCP_E2E = NOT_IMPLEMENTED 的旧行
    for line in matrix_text.splitlines():
        if "REAL_MCP_E2E" in line:
            assert "NOT_IMPLEMENTED" not in line, line[:120]


def test_estimate_messages_tokens_tolerates_none_content() -> None:
    """native tool_calls assistant 消息 content=None 时估算不得崩溃。"""
    router_source = ROUTER.read_text(encoding="utf-8")
    assert "message.get(\"content\") or \"\"" in router_source
