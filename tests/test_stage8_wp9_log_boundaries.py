"""Stage8-WP9 日志解析与 Tool output 的精确大小边界。"""

import json

from core.stage8.observation import (
    MAX_ERROR_CODE_BYTES,
    MAX_ERROR_MESSAGE_BYTES,
    MAX_FAILED_STEP_BYTES,
    MAX_OBSERVATION_BYTES,
    MAX_RESULT_LOCATION_CHARS,
    ExecutionResultParser,
)
from core.stage8.platforms import (
    DeterministicMockPlatform,
    LogRecord,
    build_stage8_tool_adapters,
)


def _result_adapter(platform):
    return dict(
        (name, adapter)
        for name, _, adapter in build_stage8_tool_adapters(platform)
    )["stage8_get_execution_result"]


def test_parser_finds_terminal_marker_after_16_kib_and_persists_bounded_tail():
    lines = ["detail=" + "x" * (MAX_OBSERVATION_BYTES + 100), "STATUS=SUCCESS"]

    result = ExecutionResultParser().parse("EXEC-LARGE", lines)

    assert result is not None
    assert result.status.value == "SUCCEEDED"
    assert len(result.log_excerpt.encode("utf-8")) <= MAX_OBSERVATION_BYTES
    assert "STATUS=SUCCESS" in result.log_excerpt
    execution_result = result.to_execution_result("EXEC-LARGE")
    assert execution_result.logs == []
    assert execution_result.log_excerpt == result.log_excerpt


def test_mock_result_keeps_decisive_tail_after_one_thousand_lines():
    platform = DeterministicMockPlatform.seeded()
    platform.logs["EXEC-LINES"] = LogRecord(
        execution_id="EXEC-LINES",
        result_location="provider://result/lines",
        lines=[f"detail-{index}" for index in range(1_500)]
        + ["STATUS=FAILED", "ERROR_CODE=ASSERTION", "ERROR_MESSAGE=tail failure"],
    )

    record = platform.get_execution_result("EXEC-LINES", 1000)
    parsed = ExecutionResultParser().parse(
        "EXEC-LINES",
        record.lines,
        result_location=record.result_location,
        log_excerpt=record.log_excerpt,
    )

    assert record.lines == [
        "STATUS=FAILED",
        "ERROR_CODE=ASSERTION",
        "ERROR_MESSAGE=tail failure",
    ]
    assert parsed is not None
    assert parsed.status.value == "FAILED"
    assert "detail-1499" in parsed.log_excerpt


def test_result_tool_json_stays_within_runtime_limit_for_escaping_and_unicode():
    platform = DeterministicMockPlatform.seeded()
    platform.logs["EXEC-OUTPUT"] = LogRecord(
        execution_id="EXEC-OUTPUT",
        result_location="provider://" + "路" * 500,
        lines=["\\\"界" * 20_000, "STATUS=FAILED", "ERROR_MESSAGE=" + "错\\\"" * 2_000],
    )
    adapter = _result_adapter(platform)
    invocation = adapter.build_invocation(
        json.dumps({"execution_id": "EXEC-OUTPUT", "max_lines": 1000})
    )

    response = adapter.invoke_once(invocation, object())
    payload = json.loads(response.content)

    assert len(response.content.encode("utf-8")) <= MAX_OBSERVATION_BYTES
    assert payload["lines"][0] == "STATUS=FAILED"
    assert len(payload["lines"][1].split("=", 1)[1].encode("utf-8")) <= MAX_ERROR_MESSAGE_BYTES
    assert len(payload["log_excerpt"].encode("utf-8")) <= MAX_OBSERVATION_BYTES


def test_utf8_excerpt_and_failure_fields_obey_strict_byte_limits():
    result = ExecutionResultParser(max_bytes=31).parse(
        "EXEC-UTF8",
        [
            "STATUS=FAILED",
            "ERROR_CODE=" + "码" * 100,
            "ERROR_MESSAGE=" + "错" * 2_000,
            "FAILED_STEP=" + "步" * 1_000,
        ],
        log_excerpt="前" * 100,
    )

    assert result is not None
    assert len(result.log_excerpt.encode("utf-8")) <= 31
    assert len(result.error_code.encode("utf-8")) <= MAX_ERROR_CODE_BYTES
    assert len(result.error_message.encode("utf-8")) <= MAX_ERROR_MESSAGE_BYTES
    assert len(result.failed_step.encode("utf-8")) <= MAX_FAILED_STEP_BYTES


def test_oversized_result_location_fails_closed():
    result = ExecutionResultParser().parse(
        "EXEC-LOCATION",
        ["STATUS=SUCCESS"],
        result_location="x" * (MAX_RESULT_LOCATION_CHARS + 1),
    )

    assert result is None
