#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Phase9-WP3：真实独立 MCP stdio server（REAL_MCP_E2E demo server）。

这是一个真实、独立、标准 MCP 2025-06-18 协议实现：通过 stdin/stdout 的
newline-delimited JSON-RPC 2.0 提供生命周期（initialize / notifications/
initialized / tools/list / tools/call）与两个受控 demo tool。它不 import
LocalAgent 的任何模块（真实互操作），使用 MCP 官方推荐的 Python SDK 优先
策略不可行时保持零第三方依赖的标准协议实现。

提供两个工具（语义见 WP3 任务 §10/§11）：

- ``get_demo_status``
    read-only；返回确定性状态 JSON（服务器计数器、workspace 路径存在性）。
    满足无 side effect、确定性输出、易自然语言触发、易人工验证。

- ``append_demo_record``
    side-effect；向 dedicated demo workspace 的 ``demo_state.jsonl`` 追加
    一条记录（timestamp、record_type、caller_note）。文件不存在时创建。
    每次真实执行都会在 ``demo_state.jsonl`` 中产生一行，因此外部观察者
    （harness / 审查者）可以通过统计文件行数独立证明 exactly-once /
    zero-execution；服务器内部还维护 monotonic call counter。

安全边界：

- workspace 路径仅接受 ``--workspace`` 参数显式给出的 dedicated demo 目录；
  不读取该目录之外的文件，不写入其它任何位置。
- ``append_demo_record`` 的参数是 ``record_type`` 与 ``caller_note`` 两个
  string 字段，均为业务载荷；server 不会记录原始环境变量或完整路径之外
  的宿主信息。
- 全部 stderr 输出仅为诊断计数摘要（tool 执行计数），不包含请求参数或
  用户正文。

用法：

    python mcp_demo_server.py --workspace <dedicated-demo-directory>

协议版本固定为 ``2025-06-18``（与 LocalAgent client 冻结版本一致）。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from pathlib import Path

# Windows 子进程不继承完整 parent environment；stdio 管道默认编码随宿主
# 漂移（cp936/ANSI）。MCP stdio payload 是 UTF-8 JSON，这里把 stdin/stdout
# 显式固定为无 BOM 的 UTF-8，保证跨宿主编码确定性。
if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", newline="\n"
    )
if sys.stdin.encoding and sys.stdin.encoding.lower().replace("-", "") != "utf8":
    sys.stdin = io.TextIOWrapper(
        sys.stdin.buffer, encoding="utf-8", newline="\n"
    )
if sys.stderr.encoding and sys.stderr.encoding.lower().replace("-", "") != "utf8":
    sys.stderr = io.TextIOWrapper(
        sys.stderr.buffer, encoding="utf-8", newline="\n"
    )

PROTOCOL_VERSION = "2025-06-18"

# 状态文件名（demo workspace 内的 dedicated side-effect 记录）。
STATE_FILE_NAME = "demo_state.jsonl"
STATE_MARKER_FILE_NAME = "demo_marker.txt"

# 并发只有 LocalAgent 单 session 串行调用，无需锁；文件写为 append+flush。
WRITE_TO_STDOUT = sys.stdout


class DemoServerError(Exception):
    """tools/call 业务错误：以 isError=true result 返回（safe summary）。"""


def _send(payload: dict) -> None:
    WRITE_TO_STDOUT.write(json.dumps(payload, ensure_ascii=False) + "\n")
    WRITE_TO_STDOUT.flush()


def _read_message() -> dict | None:
    line = sys.stdin.readline()
    if not line:
        return None
    # 容忍宿主工具写入的 UTF-8 BOM 与首行前导空白。
    line = line.lstrip("\ufeff").strip()
    if not line:
        return None
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        return {"__malformed__": True}
    return message if isinstance(message, dict) else {"__malformed__": True}


def _reply_ok(request_id: object, result: dict) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "result": result})


def _reply_error(request_id: object, code: int, message: str) -> None:
    _send(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }
    )


def _tool_get_demo_status() -> dict:
    return {
        "name": "get_demo_status",
        "description": (
            "查看 demo 服务状态：返回 demo 服务器名称、已执行的 demo 写入"
            "次数与 workspace 是否就绪。用于查询当前 demo 环境状态。"
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "annotations": {"readOnlyHint": True},
    }


def _tool_append_demo_record() -> dict:
    return {
        "name": "append_demo_record",
        "description": (
            "向 demo 记录簿追加一条 demo 记录。需要提供 record_type 与"
            " caller_note 两个字段：record_type 是记录类别（例如"
            " phase9-demo），caller_note 是本次记录的备注说明。每次追加"
            "都会真实写入 demo 记录簿。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "record_type": {"type": "string"},
                "caller_note": {"type": "string"},
            },
            "required": ["record_type", "caller_note"],
        },
        "annotations": {"readOnlyHint": False},
    }


TOOL_FACTORIES = {
    "get_demo_status": _tool_get_demo_status,
    "append_demo_record": _tool_append_demo_record,
}


class DemoWorkspace:
    """dedicated demo workspace 的受控 side-effect 状态。"""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace
        self._state_path = workspace / STATE_FILE_NAME
        self._append_count = 0

    @property
    def state_path(self) -> Path:
        return self._state_path

    @property
    def append_count(self) -> int:
        return self._append_count

    def status_summary(self) -> str:
        """read-only 状态摘要：确定性文本，便于模型回答与人工验证。"""
        record_count = 0
        if self._state_path.exists():
            with self._state_path.open("r", encoding="utf-8") as handle:
                record_count = sum(
                    1 for line in handle if line.strip()
                )
        return (
            "demo server name: localagent-demo-mcp; "
            f"append_demo_record executions: {self._append_count}; "
            f"demo records on disk: {record_count}; "
            f"workspace ready: {str(self._workspace.exists()).lower()}"
        )

    def append_record(self, record_type: str, caller_note: str) -> str:
        """唯一 side-effect 入口：追加一行 demo 记录并立即落盘。"""
        for field_name, field_value in (
            ("record_type", record_type),
            ("caller_note", caller_note),
        ):
            if not isinstance(field_value, str) or not field_value.strip():
                raise DemoServerError(
                    f"{field_name} must be a non-empty string"
                )
            if len(field_value) > 512:
                raise DemoServerError(
                    f"{field_name} exceeds the demo length limit"
                )
        entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
            + "Z",
            "record_type": record_type.strip(),
            "caller_note": caller_note.strip(),
        }
        self._workspace.mkdir(parents=True, exist_ok=True)
        with self._state_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._append_count += 1
        self._write_marker()
        return (
            "demo record appended: "
            f"record_type={entry['record_type']}; "
            f"caller_note={entry['caller_note']}; "
            f"total_records={self._append_count}"
        )

    def _write_marker(self) -> None:
        """独立于 jsonl 的第二证据面：追加计数 marker（诊断用途）。"""
        marker = self._workspace / STATE_MARKER_FILE_NAME
        with marker.open("a", encoding="utf-8") as handle:
            handle.write(f"append_count={self._append_count}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        required=True,
        help="dedicated demo workspace directory for demo side effects",
    )
    parser.add_argument(
        "--protocol-version-override",
        default=None,
        help="diagnostic only: reply a non-2025-06-18 protocol version",
    )
    args = parser.parse_args()

    workspace = DemoWorkspace(Path(args.workspace).resolve())
    sys.stderr.write(
        "[localagent-demo-mcp] starting; workspace dedicated to demo\n"
    )

    while True:
        message = _read_message()
        if message is None:
            break
        if message.get("__malformed__"):
            continue
        method = message.get("method")
        request_id = message.get("id")

        if method == "initialize":
            protocol_version = (
                args.protocol_version_override or PROTOCOL_VERSION
            )
            _reply_ok(
                request_id,
                {
                    "protocolVersion": protocol_version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": "localagent-demo-mcp",
                        "version": "1.0.0",
                    },
                },
            )
            continue

        # notifications（无 id）：initialized / cancelled 一律只确认不回复。
        if request_id is None:
            continue

        if method == "tools/list":
            _reply_ok(
                request_id,
                {
                    "tools": [
                        _tool_get_demo_status(),
                        _tool_append_demo_record(),
                    ]
                },
            )
            continue

        if method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            arguments = params.get("arguments") or {}
            try:
                if name == "get_demo_status":
                    _reply_ok(
                        request_id,
                        {
                            "content": [
                                {
                                    "type": "text",
                                    "text": workspace.status_summary(),
                                }
                            ],
                            "isError": False,
                        },
                    )
                elif name == "append_demo_record":
                    text = workspace.append_record(
                        arguments.get("record_type"),
                        arguments.get("caller_note"),
                    )
                    _reply_ok(
                        request_id,
                        {
                            "content": [{"type": "text", "text": text}],
                            "isError": False,
                        },
                    )
                else:
                    _reply_ok(
                        request_id,
                        {
                            "content": [
                                {
                                    "type": "text",
                                    "text": "unknown demo tool requested",
                                }
                            ],
                            "isError": True,
                        },
                    )
            except DemoServerError as error:
                _reply_ok(
                    request_id,
                    {
                        "content": [{"type": "text", "text": str(error)}],
                        "isError": True,
                    },
                )
            sys.stderr.write(
                "[localagent-demo-mcp] tool call handled; "
                f"append_count={workspace.append_count}\n"
            )
            continue

        _reply_error(request_id, -32601, "method not found")

    sys.stderr.write("[localagent-demo-mcp] stdin closed; exiting\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
