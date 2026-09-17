"""Stage8 WP9 的确定性执行结果观察与日志解析。"""

from __future__ import annotations

from dataclasses import dataclass

from core.stage8.domain import ExecutionResult, ExternalExecutionStatus


MAX_OBSERVATION_BYTES = 16 * 1024
MAX_ERROR_CODE_BYTES = 128
MAX_ERROR_MESSAGE_BYTES = 2 * 1024
MAX_FAILED_STEP_BYTES = 512
MAX_RESULT_LOCATION_CHARS = 1024

_RESULT_FIELD_LIMITS = {
    "STATUS": 16,
    "ERROR_CODE": MAX_ERROR_CODE_BYTES,
    "ERROR_MESSAGE": MAX_ERROR_MESSAGE_BYTES,
    "FAILED_STEP": MAX_FAILED_STEP_BYTES,
}


def _bounded_utf8_prefix(value: str, max_bytes: int) -> str:
    """返回严格不超过 byte 上限、且不包含残缺 UTF-8 字符的前缀。"""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def bounded_log_excerpt(value: str, max_bytes: int = MAX_OBSERVATION_BYTES) -> str:
    """为持久化生成 UTF-8 安全的 bounded head/tail excerpt。"""
    if max_bytes <= 0:
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    marker = b"\n...[truncated]...\n"
    if max_bytes <= len(marker):
        return encoded[:max_bytes].decode("utf-8", errors="ignore")
    remaining = max_bytes - len(marker)
    head_bytes = remaining // 2
    tail_bytes = remaining - head_bytes
    head = encoded[:head_bytes].decode("utf-8", errors="ignore")
    tail = encoded[-tail_bytes:].decode("utf-8", errors="ignore")
    return head + marker.decode("ascii") + tail


def decisive_result_lines(lines: list[str]) -> list[str]:
    """提取固定格式字段但不解释 STATUS；最终语义仍由 parser 决定。"""
    fields: dict[str, str] = {}
    for item in lines:
        for line in item.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key in _RESULT_FIELD_LIMITS:
                fields[key] = _bounded_utf8_prefix(
                    value.strip(), _RESULT_FIELD_LIMITS[key]
                )
    return [
        f"{key}={fields[key]}"
        for key in ("STATUS", "ERROR_CODE", "ERROR_MESSAGE", "FAILED_STEP")
        if key in fields
    ]


@dataclass(frozen=True, slots=True)
class NormalizedExecutionResult:
    status: ExternalExecutionStatus
    actual_result: str
    error_code: str | None = None
    error_message: str | None = None
    failed_step: str | None = None
    result_location: str | None = None
    log_excerpt: str = ""

    def to_execution_result(self, execution_id: str) -> ExecutionResult:
        return ExecutionResult(
            execution_id=execution_id,
            status=self.status,
            actual_result=self.actual_result,
            failure_signature=self.error_code,
            logs=[],
            error_code=self.error_code,
            error_message=self.error_message,
            failed_step=self.failed_step,
            result_location=self.result_location,
            log_excerpt=self.log_excerpt,
        )


class ExecutionResultParser:
    """解析 deterministic mock execution result format；无法确定时 fail closed。"""

    def __init__(self, *, max_bytes: int = MAX_OBSERVATION_BYTES):
        self.max_bytes = max_bytes

    def parse(
        self,
        execution_id: str,
        lines: list[str],
        *,
        result_location: str | None = None,
        log_excerpt: str | None = None,
    ) -> NormalizedExecutionResult | None:
        if result_location is not None and len(result_location) > MAX_RESULT_LOCATION_CHARS:
            return None
        fields: dict[str, str] = {}
        for line in decisive_result_lines(lines):
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            fields[key] = value
        bounded = bounded_log_excerpt(
            log_excerpt if log_excerpt is not None else "\n".join(lines),
            self.max_bytes,
        )
        status = fields.get("STATUS")
        if status == "SUCCESS":
            return NormalizedExecutionResult(
                ExternalExecutionStatus.SUCCEEDED, "SUCCESS", result_location=result_location, log_excerpt=bounded
            )
        if status == "FAILED":
            return NormalizedExecutionResult(
                ExternalExecutionStatus.FAILED,
                fields.get("ERROR_MESSAGE", "FAILED"),
                error_code=fields.get("ERROR_CODE"),
                error_message=fields.get("ERROR_MESSAGE"),
                failed_step=fields.get("FAILED_STEP"),
                result_location=result_location,
                log_excerpt=bounded,
            )
        return None
