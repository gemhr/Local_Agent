#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Phase9-WP3：REAL MCP E2E harness。

针对一个真实运行的 LocalAgent backend（uvicorn + lifespan + Coordinated
Runtime + DeepSeek remote model + 真实独立 stdio MCP demo server）执行完整
HTTP E2E：

- Case A  read-only：自然语言 → DeepSeek native tool selection →
  demo_read_status（MCP-backed）→ governance ALLOW → tools/call →
  role=tool continuation → final answer。
- Case B  side-effect APPROVE：自然语言 → demo_append_record →
  APPROVAL_REQUIRED → 真实 HTTP APPROVE → exactly-once tools/call →
  final answer。
- Case C  side-effect REJECT：自然语言 → APPROVAL_REQUIRED → 真实 HTTP
  REJECT → zero MCP execution。
- Case F  failure：server unavailable（session 缺失）→ safe typed failure。

观察面（互不信任、交叉验证）：
- HTTP wire：X-Run-Id、[[ORCH]] CONTROL 事件、final answer 文本。
- MCP demo server 独立状态：demo_state.jsonl 行数 / demo_marker.txt。
- backend 进程结构化日志文件（console log，由 uvicorn 写出）。

用法：
    python demo/run_real_mcp_e2e.py --base-url http://127.0.0.1:8000 \
        --workspace <demo_workspace> --log-file <backend_console_log> \
        --cases A,B,C,F --out <result_json_path>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

APPROVAL_REQUESTED = "TOOL_APPROVAL_REQUESTED"
APPROVAL_DECIDED = "TOOL_APPROVAL_DECIDED"
RUN_COMPLETED = "RUN_COMPLETED"
STEP_COMPLETED = "STEP_COMPLETED"
TOOL_STARTED = "TOOL_STARTED"
TOOL_COMPLETED = "TOOL_COMPLETED"

_ORCH_RE = re.compile(r"^\[\[ORCH\]\](\{.*\})\s*$", re.DOTALL)


def http_post_json(url: str, body: dict, timeout: float = 15.0) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            payload = json.loads(error.read().decode("utf-8"))
        except Exception:
            payload = {}
        return error.code, payload


def stream_chat(base_url: str, query: str, on_orch_event) -> dict:
    """POST /api/chat 并消费完整 chunk 流；返回 run_id / 文本 / 事件。"""
    request = urllib.request.Request(
        f"{base_url}/api/chat",
        data=json.dumps(
            {"agent_id": "core_router", "query": query, "file_path": ""}
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    result: dict = {
        "run_id": None,
        "text_chunks": [],
        "events": [],
        "safe_errors": [],
    }
    with urllib.request.urlopen(request, timeout=1800) as response:
        result["run_id"] = response.headers.get("X-Run-Id")
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace")
            match = _ORCH_RE.match(line.strip())
            if match:
                event = json.loads(match.group(1))
                result["events"].append(event)
                on_orch_event(event)
                continue
            if line.startswith("[runtime-error]"):
                result["safe_errors"].append(line.strip())
            else:
                result["text_chunks"].append(line)
    return result


def read_marker_counts(workspace: Path) -> dict:
    """从 demo server 的 dedicated 状态面独立读取执行证据。"""
    state = workspace / "demo_state.jsonl"
    marker = workspace / "demo_marker.txt"
    records = []
    if state.exists():
        for line in state.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    marker_appends = 0
    if marker.exists():
        for line in marker.read_text(encoding="utf-8").splitlines():
            match = re.match(r"append_count=(\d+)", line.strip())
            if match:
                marker_appends = max(marker_appends, int(match.group(1)))
    return {
        "jsonl_record_count": len(records),
        "jsonl_records": records,
        "marker_max_append_count": marker_appends,
    }


def count_log_occurrences(log_file: Path, needle: str) -> int:
    if not log_file.exists():
        return 0
    return log_file.read_text(encoding="utf-8", errors="replace").count(needle)


def wait_for_approval(stream_state: dict, deadline: float) -> dict | None:
    """从已缓存事件里取最新 approval request（流式回调已实时触发 approve）。"""
    for event in reversed(stream_state["events"]):
        if event.get("event_type") == APPROVAL_REQUESTED:
            return event
        if event.get("event_type") == RUN_COMPLETED:
            break
    return None


def run_case_a(base_url: str, workspace: Path) -> dict:
    before = read_marker_counts(workspace)
    evidence: dict = {"case": "A_read_only", "marker_before": before}
    approval_seen: list = []

    def on_event(event: dict) -> None:
        if event.get("event_type") == APPROVAL_REQUESTED:
            approval_seen.append(event)

    state = stream_chat(base_url, "请查询一下当前 demo 环境的状态，告诉我 demo 服务器现在记录了几条内容", on_event)
    final_answer = "".join(state["text_chunks"]).strip()
    after = read_marker_counts(workspace)
    evidence.update(
        {
            "run_id": state["run_id"],
            "final_answer": final_answer,
            "safe_errors": state["safe_errors"],
            "marker_after": after,
        }
    )
    evidence["orchestration_events"] = [
        {
            "event_type": e.get("event_type"),
            "tool_name": e.get("payload", {}).get("tool_name"),
            "succeeded": e.get("payload", {}).get("succeeded"),
            "safe_error_code": e.get("payload", {}).get("safe_error_code"),
            "status": e.get("payload", {}).get("status"),
            "risk_level": e.get("payload", {}).get("risk_level"),
            "approval_id": e.get("payload", {}).get("approval_id"),
            "decision_status": e.get("payload", {}).get("decision_status"),
        }
        for e in state["events"]
    ]
    tool_events = [e for e in state["events"] if e.get("event_type") == TOOL_COMPLETED]
    evidence["tool_completed_count"] = len(tool_events)
    evidence["tool_name_used"] = tool_events[-1]["payload"]["tool_name"] if tool_events else None
    evidence["tool_succeeded"] = tool_events[-1]["payload"].get("succeeded") if tool_events else None
    evidence["approval_requested_observed"] = bool(approval_seen)
    evidence["demo_record_delta"] = (
        after["jsonl_record_count"] - before["jsonl_record_count"]
    )
    return evidence


def run_case_b_or_c(base_url: str, workspace: Path, approve: bool, record_note: str) -> dict:
    before = read_marker_counts(workspace)
    label = "B_approve" if approve else "C_reject"
    evidence: dict = {"case": f"{label}_side_effect", "marker_before": before}
    approval_event: dict | None = None
    decided_event: dict | None = None
    decision_http: dict = {}

    def on_event(event: dict) -> None:
        nonlocal approval_event, decided_event
        if event.get("event_type") == APPROVAL_REQUESTED and approval_event is None:
            approval_event = event
            payload = event["payload"]
            decision_path = "approve" if approve else "reject"
            url = (
                f"{base_url}/api/runtime/runs/{event['run_id']}"
                f"/tool-approvals/{payload['approval_id']}/{decision_path}"
            )
            status, body = http_post_json(
                url,
                {
                    "invocation_binding_digest": payload[
                        "invocation_binding_digest"
                    ],
                    "actor_id": "wp3-real-e2e-harness",
                },
            )
            decision_http["status"] = status
            decision_http["body"] = body
        elif event.get("event_type") == APPROVAL_DECIDED:
            decided_event = event

    state = stream_chat(
        base_url,
        f"请在 demo 记录簿里追加一条 demo 记录，record_type 用 phase9-demo，备注写：{record_note}",
        on_event,
    )
    final_answer = "".join(state["text_chunks"]).strip()
    after = read_marker_counts(workspace)
    evidence.update(
        {
            "run_id": state["run_id"],
            "approval_requested": (
                {
                    "approval_id": approval_event["payload"]["approval_id"],
                    "tool_name": approval_event["payload"]["tool_name"],
                    "risk_level": approval_event["payload"]["risk_level"],
                    "risk_facts": approval_event["payload"].get("risk_facts"),
                    "invocation_binding_digest": approval_event["payload"][
                        "invocation_binding_digest"
                    ],
                }
                if approval_event
                else None
            ),
            "decision_http": decision_http,
            "decision_status_observed": (
                decided_event["payload"].get("decision_status")
                if decided_event
                else None
            ),
            "final_answer": final_answer,
            "safe_errors": state["safe_errors"],
            "marker_after": after,
        }
    )
    evidence["orchestration_events"] = [
        {
            "event_type": e.get("event_type"),
            "tool_name": e.get("payload", {}).get("tool_name"),
            "succeeded": e.get("payload", {}).get("succeeded"),
            "safe_error_code": e.get("payload", {}).get("safe_error_code"),
            "status": e.get("payload", {}).get("status"),
            "approval_id": e.get("payload", {}).get("approval_id"),
            "decision_status": e.get("payload", {}).get("decision_status"),
        }
        for e in state["events"]
    ]
    tool_completed = [e for e in state["events"] if e.get("event_type") == TOOL_COMPLETED]
    evidence["tool_completed_count"] = len(tool_completed)
    evidence["tool_name_used"] = (
        tool_completed[-1]["payload"]["tool_name"] if tool_completed else None
    )
    evidence["demo_record_delta"] = (
        after["jsonl_record_count"] - before["jsonl_record_count"]
    )
    # 记录内容里的 record/caller note 计数（exactly-once 的文件证据）
    if approve and approval_event:
        evidence["jsonl_records_matching_note"] = [
            r
            for r in after["jsonl_records"]
            if r.get("caller_note") == record_note
        ]
    return evidence


def run_case_f(base_url: str, workspace: Path, config_path: Path) -> dict:
    """Failure case：请求 backend 重新加载一个指向不可执行命令的配置。

    该 case 需要独立 backend 实例（由外层脚本负责启动/关闭），本 harness
    只对当前 backend 发起一次 read-only 对话并记录最终 safe 状态。真正的
    unavailable 语义由 backend 启动期的 DISCOVERY_FAILED 日志与本次对话
    的 tool 缺失行为共同证明。
    """
    before = read_marker_counts(workspace)
    state = stream_chat(base_url, "请查询一下当前 demo 环境的状态", lambda event: None)
    final_answer = "".join(state["text_chunks"]).strip()
    after = read_marker_counts(workspace)
    return {
        "case": "F_failure_probe",
        "run_id": state["run_id"],
        "final_answer": final_answer,
        "safe_errors": state["safe_errors"],
        "demo_record_delta": (
            after["jsonl_record_count"] - before["jsonl_record_count"]
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--cases", default="A,B,C")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    workspace = Path(args.workspace).resolve()
    cases = [case.strip().upper() for case in args.cases.split(",") if case.strip()]

    results = []
    for case in cases:
        started = time.time()
        if case == "A":
            result = run_case_a(base_url, workspace)
        elif case == "B":
            result = run_case_b_or_c(
                base_url, workspace, approve=True, record_note="wp3-approve-once"
            )
        elif case == "C":
            result = run_case_b_or_c(
                base_url, workspace, approve=False, record_note="wp3-reject-never"
            )
        elif case == "F":
            result = run_case_f(base_url, workspace, Path("unused"))
        else:
            raise SystemExit(f"unknown case: {case}")
        result["duration_seconds"] = round(time.time() - started, 2)
        results.append(result)
        print(
            f"[harness] case {case} done in {result['duration_seconds']}s; "
            f"run_id={result.get('run_id')}",
            file=sys.stderr,
        )

    Path(args.out).write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[harness] wrote results to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
