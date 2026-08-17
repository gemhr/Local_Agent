#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WP4-C：AgentEvalOpsTraceExporter 最小 HTTP adapter 测试。

覆盖（任务 §26/§32/§33 + 冻结 reentry §27-§29）：

- 单元级（fake pycurl module）：URL 构造、必需 headers、serializer bytes 原样
  传递、timeout options、TLS verify、redirect/proxy 禁用、2xx/非 2xx 分类、
  bounded response、no retry、deadline 翻译、close 幂等、能力门禁、错误
  content-free。
- 真实本地 HTTP server：201/200/500/401、connection refused、硬总 deadline、
  slow trickle、response >4096、3xx no-follow、body-after-reset 不重发、
  stale keepalive 每 envelope 恰好一个 POST。
- 真实 TraceExportDispatcher 集成与 bounded shutdown。

真实测试只使用 127.0.0.1 本机 socket 与合成安全 ID，不携带真实密钥或业务正文。
"""

from __future__ import annotations

import http.server
import socket
import struct
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import pycurl as real_pycurl

import core.runtime.agent_evalops_trace_exporter as exporter_module
from core.runtime.agent_evalops_trace_exporter import (
    AGENTEVALOPS_MAX_RESPONSE_BODY_BYTES,
    AGENTEVALOPS_TRACE_EXPORT_CAPABILITY_MISSING,
    AGENTEVALOPS_TRACE_EXPORT_CLOSED,
    AGENTEVALOPS_TRACE_EXPORT_DEADLINE_EXCEEDED,
    AGENTEVALOPS_TRACE_EXPORT_FAILED,
    AGENTEVALOPS_TRACE_EXPORT_PATH,
    AGENTEVALOPS_TRACE_EXPORT_RESPONSE_TOO_LARGE,
    AgentEvalOpsTraceExportError,
    AgentEvalOpsTraceExporter,
)
from core.runtime.tracing import InMemorySpanRecorder, SpanStatus
from core.runtime.trace_export_contract import (
    TRACE_EXPORT_CONTRACT_IDENTITY,
    TRACE_EXPORT_CONTRACT_VERSION,
    TraceExportEnvelope,
)
from core.runtime.trace_export_dispatcher import TraceExportDispatcher
from core.runtime.trace_export_serialization import (
    serialize_trace_export_envelope,
)

_TEST_FINGERPRINT = "6fc033bb4310c7671541d7dc9e7297fdf0d0bb32605651b840ccd0fd173390ab"
_DEADLINE_TOLERANCE = 0.25

# 冻结 reentry：默认 CONNECTTIMEOUT_MS=500、TIMEOUT_MS=3000。
_DEFAULT_CONNECT_MS = 500
_DEFAULT_TOTAL_MS = 3000


def make_envelope(**overrides) -> TraceExportEnvelope:
    """构造一个语义合法的合成 TraceExportEnvelope。"""
    now = datetime.now(UTC)
    values = dict(
        contract_identity=TRACE_EXPORT_CONTRACT_IDENTITY,
        contract_version=TRACE_EXPORT_CONTRACT_VERSION,
        contract_fingerprint=_TEST_FINGERPRINT,
        run_id="run-wp4c-test-1",
        trace_id="trace-wp4c-test-1",
        span_id="span-wp4c-test-1",
        parent_span_id=None,
        step_id=None,
        operation="runtime.run",
        component="test",
        started_at=now - timedelta(seconds=1),
        completed_at=now,
        duration_ms=1000.0,
        status=SpanStatus.OK,
        error_code=None,
        attributes={"plan_id": "plan-wp4c-1", "step_count": 3},
    )
    values.update(overrides)
    return TraceExportEnvelope(**values)


def default_exporter(**overrides) -> AgentEvalOpsTraceExporter:
    values = dict(
        base_url="http://127.0.0.1:8001",
        api_key="API_KEY_TEST_1",
        project_id="PROJECT_TEST_1",
        connect_timeout_seconds=0.5,
        total_deadline_seconds=3.0,
    )
    values.update(overrides)
    return AgentEvalOpsTraceExporter(**values)


# ---------------------------------------------------------------------------
# fake pycurl module（单元测试 seam；不替换生产代码）
# ---------------------------------------------------------------------------


class FakeCurl:
    """记录 setopt 的 fake easy handle；perform 行为由测试控制。"""

    def __init__(self, module) -> None:
        self._module = module
        self.options: dict[int, object] = {}
        self.perform_calls = 0
        self.closed = False
        self.status = 200
        self.connect_time = 0.1
        self.perform_error: tuple[int, str] | None = None
        self.response_chunks: list[bytes] = [b'{"status":"PERSISTED","error_code":null}']

    def setopt(self, option: int, value: object) -> None:
        self.options[option] = value

    def getinfo(self, option: int) -> object:
        if option == self._module.RESPONSE_CODE:
            return self.status
        if option == self._module.CONNECT_TIME:
            return self.connect_time
        return None

    def reset(self) -> None:
        self.options = {}

    def perform(self) -> None:
        self.perform_calls += 1
        if self.perform_error is not None:
            raise self._module.error(self.perform_error[0], self.perform_error[1])
        write = self.options.get(self._module.WRITEFUNCTION)
        if write is not None:
            for chunk in self.response_chunks:
                write(chunk)

    def close(self) -> None:
        self.closed = True


def make_fake_pycurl(*, asyncdns: bool = True, ssl: bool = True) -> SimpleNamespace:
    """构造带能力常量与 Curl 工厂的 fake pycurl module。"""
    curl_instances: list[FakeCurl] = []

    def curl_factory() -> FakeCurl:
        curl = FakeCurl(module)
        curl_instances.append(curl)
        return curl

    features = 0
    if asyncdns:
        features |= 128  # VERSION_ASYNCHDNS
    if ssl:
        features |= 4  # VERSION_SSL
    protocols = ("dict", "file", "http", "https", "ftp", "smtp") if ssl else ("dict", "file", "http", "ftp")

    module = SimpleNamespace(
        VERSION_ASYNCHDNS=128,
        VERSION_SSL=4,
        E_OPERATION_TIMEDOUT=28,
        RESPONSE_CODE=2097154,
        CONNECT_TIME=3145733,
        CURL_HTTP_VERSION_1_1=2,
        URL=1,
        POST=2,
        POSTFIELDS=3,
        HTTPHEADER=4,
        CONNECTTIMEOUT_MS=5,
        TIMEOUT_MS=6,
        NOSIGNAL=7,
        FOLLOWLOCATION=8,
        MAXREDIRS=9,
        PROXY=10,
        SSL_VERIFYPEER=11,
        SSL_VERIFYHOST=12,
        HTTP_VERSION=13,
        WRITEFUNCTION=14,
        error=type("PycurlError", (Exception,), {}),
        Curl=curl_factory,
        version_info=lambda: (
            11,
            "8.20.0",
            529408,
            "Windows",
            features,
            "(OpenSSL/3.6.3) Schannel",
            0,
            "1.3.2",
            protocols,
            None,
            0,
            None,
        ),
    )
    module._curl_instances = curl_instances  # type: ignore[attr-defined]
    return module


@pytest.fixture
def fake_pycurl(monkeypatch):
    """把所有 exporter 单元测试绑定到 fake pycurl module。"""
    fake = make_fake_pycurl()
    monkeypatch.setattr(exporter_module, "_pycurl_module", fake)
    return fake


@pytest.fixture
def curl_instances(fake_pycurl):
    return fake_pycurl._curl_instances  # type: ignore[attr-defined]


# --- 单元级：options / URL / headers / serializer bytes ---------------------


def test_url_construction(fake_pycurl, curl_instances) -> None:
    exporter = default_exporter(base_url="https://agent-eval.example:8443/")
    assert exporter._url == (
        "https://agent-eval.example:8443" + AGENTEVALOPS_TRACE_EXPORT_PATH
    )
    exporter.send(make_envelope())
    assert curl_instances[0].options[fake_pycurl.URL] == (
        "https://agent-eval.example:8443" + AGENTEVALOPS_TRACE_EXPORT_PATH
    )


def test_required_headers(fake_pycurl, curl_instances) -> None:
    exporter = default_exporter(
        api_key="API_KEY_TEST_1", project_id="PROJECT_TEST_1"
    )
    exporter.send(make_envelope())
    headers = curl_instances[0].options[fake_pycurl.HTTPHEADER]
    assert "Content-Type: application/json" in headers
    assert "X-API-Key: API_KEY_TEST_1" in headers
    assert "X-Project-ID: PROJECT_TEST_1" in headers
    # 禁用 Expect: 100-continue，避免 body/reset 场景的协商重发路径。
    assert "Expect:" in headers


def test_serializer_bytes_passed_unchanged(fake_pycurl, curl_instances) -> None:
    exporter = default_exporter()
    envelope = make_envelope()
    exporter.send(envelope)
    expected = serialize_trace_export_envelope(envelope)
    assert curl_instances[0].options[fake_pycurl.POSTFIELDS] == expected
    assert isinstance(curl_instances[0].options[fake_pycurl.POSTFIELDS], bytes)


def test_timeout_options(fake_pycurl, curl_instances) -> None:
    exporter = default_exporter(
        connect_timeout_seconds=0.5, total_deadline_seconds=3.0
    )
    exporter.send(make_envelope())
    options = curl_instances[0].options
    assert options[fake_pycurl.CONNECTTIMEOUT_MS] == _DEFAULT_CONNECT_MS
    assert options[fake_pycurl.TIMEOUT_MS] == _DEFAULT_TOTAL_MS
    assert options[fake_pycurl.NOSIGNAL] == 1


def test_tls_verify_redirect_proxy_options(fake_pycurl, curl_instances) -> None:
    exporter = default_exporter()
    exporter.send(make_envelope())
    options = curl_instances[0].options
    assert options[fake_pycurl.SSL_VERIFYPEER] == 1
    assert options[fake_pycurl.SSL_VERIFYHOST] == 2
    assert options[fake_pycurl.FOLLOWLOCATION] == 0
    assert options[fake_pycurl.MAXREDIRS] == 0
    assert options[fake_pycurl.PROXY] == ""
    assert options[fake_pycurl.POST] == 1
    assert options[fake_pycurl.HTTP_VERSION] == fake_pycurl.CURL_HTTP_VERSION_1_1


def test_one_send_one_perform_no_retry(fake_pycurl, curl_instances) -> None:
    exporter = default_exporter()
    exporter.send(make_envelope())
    assert curl_instances[0].perform_calls == 1
    # 成功与失败路径都绝不重发。
    curl_instances[0].perform_error = (7, "couldn't connect")
    with pytest.raises(AgentEvalOpsTraceExportError) as captured:
        exporter.send(make_envelope())
    assert captured.value.error_code == AGENTEVALOPS_TRACE_EXPORT_FAILED
    assert curl_instances[0].perform_calls == 2


def test_2xx_success_201_and_200(fake_pycurl, curl_instances) -> None:
    exporter = default_exporter()
    for status in (201, 200):
        curl_instances[0].status = status
        assert exporter.send(make_envelope()) is None


def test_non_2xx_failure_classification(fake_pycurl, curl_instances) -> None:
    exporter = default_exporter()
    for status in (401, 500, 302):
        curl_instances[0].status = status
        with pytest.raises(AgentEvalOpsTraceExportError) as captured:
            exporter.send(make_envelope())
        assert captured.value.error_code == AGENTEVALOPS_TRACE_EXPORT_FAILED
        assert captured.value.http_status == status


def test_deadline_translation(fake_pycurl, curl_instances) -> None:
    exporter = default_exporter()
    curl_instances[0].perform_error = (28, "Operation timed out")
    with pytest.raises(AgentEvalOpsTraceExportError) as captured:
        exporter.send(make_envelope())
    assert captured.value.error_code == AGENTEVALOPS_TRACE_EXPORT_DEADLINE_EXCEEDED
    assert captured.value.curl_code == 28


def test_response_bounded_write_function(fake_pycurl, curl_instances) -> None:
    exporter = default_exporter()
    big_chunk = b"x" * (AGENTEVALOPS_MAX_RESPONSE_BODY_BYTES + 100)
    curl_instances[0].response_chunks = [big_chunk]
    with pytest.raises(AgentEvalOpsTraceExportError) as captured:
        exporter.send(make_envelope())
    assert captured.value.error_code == AGENTEVALOPS_TRACE_EXPORT_RESPONSE_TOO_LARGE
    assert len(exporter._response_body) <= AGENTEVALOPS_MAX_RESPONSE_BODY_BYTES


def test_send_after_close_raises_closed(fake_pycurl, curl_instances) -> None:
    exporter = default_exporter()
    assert exporter.close(1.0) is True
    assert exporter.close(1.0) is True
    assert curl_instances[0].closed is True
    with pytest.raises(AgentEvalOpsTraceExportError) as captured:
        exporter.send(make_envelope())
    assert captured.value.error_code == AGENTEVALOPS_TRACE_EXPORT_CLOSED
    # 重复 close 不再次物理 cleanup。
    assert curl_instances[0].closed is True


def test_error_is_content_free(fake_pycurl, curl_instances) -> None:
    exporter = default_exporter(api_key="API_KEY_TEST_1", project_id="PROJECT_TEST_1")
    curl_instances[0].status = 500
    with pytest.raises(AgentEvalOpsTraceExportError) as captured:
        exporter.send(make_envelope())
    error = captured.value
    for secret in ("API_KEY_TEST_1", "PROJECT_TEST_1", "run-wp4c-test-1", "http://127.0.0.1"):
        assert secret not in str(error)
        assert secret not in repr(error)
    assert "500" in repr(error)


def test_capability_gate_asyncdns_required(monkeypatch) -> None:
    fake = make_fake_pycurl(asyncdns=False)
    monkeypatch.setattr(exporter_module, "_pycurl_module", fake)
    with pytest.raises(AgentEvalOpsTraceExportError) as captured:
        default_exporter()
    assert captured.value.error_code == AGENTEVALOPS_TRACE_EXPORT_CAPABILITY_MISSING


def test_capability_gate_https_requires_ssl(monkeypatch) -> None:
    fake = make_fake_pycurl(asyncdns=True, ssl=False)
    monkeypatch.setattr(exporter_module, "_pycurl_module", fake)
    with pytest.raises(AgentEvalOpsTraceExportError) as captured:
        default_exporter(base_url="https://agent-eval.example")
    assert captured.value.error_code == AGENTEVALOPS_TRACE_EXPORT_CAPABILITY_MISSING
    # http 目标不要求 SSL 能力。
    exporter = default_exporter(base_url="http://agent-eval.example")
    assert exporter.close(1.0) is True


def test_constructor_rejects_invalid_input(fake_pycurl) -> None:
    with pytest.raises(ValueError):
        default_exporter(base_url="ftp://agent-eval.example")
    with pytest.raises(ValueError):
        default_exporter(base_url="http://agent-eval.example/path")
    with pytest.raises(ValueError):
        default_exporter(connect_timeout_seconds=3.0, total_deadline_seconds=1.0)
    with pytest.raises(ValueError):
        default_exporter(connect_timeout_seconds=0.0001)
    with pytest.raises(ValueError):
        default_exporter(total_deadline_seconds=0)


def test_close_failure_returns_false(fake_pycurl, curl_instances, monkeypatch) -> None:
    exporter = default_exporter()
    original_close = FakeCurl.close

    def failing_close(self) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(FakeCurl, "close", failing_close)
    assert exporter.close(1.0) is False
    monkeypatch.setattr(FakeCurl, "close", original_close)
    # 失败后仍视为已关闭（不再尝试二次 cleanup）。
    assert exporter.close(1.0) is True


# ---------------------------------------------------------------------------
# 真实本地 HTTP server 测试
# ---------------------------------------------------------------------------


def make_server(requests: list, behavior) -> http.server.ThreadingHTTPServer:
    """构造 127.0.0.1 随机端口的真实 HTTP server，记录每个 POST。"""

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            requests.append(
                {"path": self.path, "headers": dict(self.headers), "body": body}
            )
            behavior(self, body)

        def log_message(self, *args) -> None:  # noqa: D102
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


@pytest.fixture
def real_pycurl_module(monkeypatch):
    """真实 HTTP 测试使用真实 pycurl。"""
    monkeypatch.setattr(exporter_module, "_pycurl_module", real_pycurl)
    return real_pycurl


def ok_behavior(status: int = 201, *, body: bytes | None = None, close_connection: bool = False):
    def behavior(handler, request_body: bytes) -> None:
        payload = body if body is not None else b'{"status":"PERSISTED","error_code":null}'
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        if close_connection:
            handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(payload)
        if close_connection:
            handler.close_connection = True

    return behavior


def error_behavior(status: int):
    def behavior(handler, request_body: bytes) -> None:
        payload = b'{"status":"REJECTED","error_code":"TEST_REJECTED"}'
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    return behavior


def silent_behavior(handler, request_body: bytes) -> None:
    time.sleep(30)


def trickle_behavior(handler, request_body: bytes) -> None:
    """每 0.2s 写一个 byte、持续远超 deadline；总响应时间超过 total deadline。"""
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.end_headers()
    for _ in range(60):
        handler.wfile.write(b"x")
        handler.wfile.flush()
        time.sleep(0.2)


def reset_after_body_behavior(handler, request_body: bytes) -> None:
    """收到完整 body 后以 RST 关闭连接，不发送任何响应。"""
    handler.connection.setsockopt(
        socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
    )
    handler.connection.close()


def test_real_http_201_and_200(real_pycurl_module) -> None:
    requests: list = []
    server = make_server(requests, ok_behavior(201))
    try:
        exporter = default_exporter(base_url=f"http://127.0.0.1:{server.server_address[1]}")
        envelope = make_envelope()
        assert exporter.send(envelope) is None
        assert requests[0]["path"] == AGENTEVALOPS_TRACE_EXPORT_PATH
        assert requests[0]["headers"].get("Content-Type") == "application/json"
        assert requests[0]["headers"].get("X-API-Key") == "API_KEY_TEST_1"
        assert requests[0]["headers"].get("X-Project-ID") == "PROJECT_TEST_1"
        assert requests[0]["body"] == serialize_trace_export_envelope(envelope)
        exporter.close(1.0)
    finally:
        server.shutdown()

    requests2: list = []
    server2 = make_server(requests2, ok_behavior(200))
    try:
        exporter2 = default_exporter(base_url=f"http://127.0.0.1:{server2.server_address[1]}")
        envelope2 = make_envelope(span_id="span-wp4c-dup-1")
        assert exporter2.send(envelope2) is None
        assert requests2[0]["body"] == serialize_trace_export_envelope(envelope2)
        exporter2.close(1.0)
    finally:
        server2.shutdown()


def test_real_http_4xx_5xx_failure(real_pycurl_module) -> None:
    for status in (401, 500):
        requests: list = []
        server = make_server(requests, error_behavior(status))
        try:
            exporter = default_exporter(
                base_url=f"http://127.0.0.1:{server.server_address[1]}"
            )
            with pytest.raises(AgentEvalOpsTraceExportError) as captured:
                exporter.send(make_envelope())
            assert captured.value.error_code == AGENTEVALOPS_TRACE_EXPORT_FAILED
            assert captured.value.http_status == status
            exporter.close(1.0)
        finally:
            server.shutdown()


def test_real_http_connection_refused(real_pycurl_module) -> None:
    # 绑定后立即关闭，得到一个必然 refused 的端口。
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    exporter = default_exporter(base_url=f"http://127.0.0.1:{port}")
    try:
        with pytest.raises(AgentEvalOpsTraceExportError) as captured:
            exporter.send(make_envelope())
        assert captured.value.error_code == AGENTEVALOPS_TRACE_EXPORT_FAILED
    finally:
        exporter.close(1.0)


def test_real_http_hard_total_deadline(real_pycurl_module) -> None:
    requests: list = []
    server = make_server(requests, silent_behavior)
    exporter = default_exporter(
        base_url=f"http://127.0.0.1:{server.server_address[1]}",
        total_deadline_seconds=1.0,
    )
    try:
        started = time.monotonic()
        with pytest.raises(AgentEvalOpsTraceExportError) as captured:
            exporter.send(make_envelope())
        elapsed = time.monotonic() - started
        assert captured.value.error_code == AGENTEVALOPS_TRACE_EXPORT_DEADLINE_EXCEEDED
        assert elapsed <= 1.0 + _DEADLINE_TOLERANCE
        assert len(requests) == 1
    finally:
        exporter.close(1.0)
        server.shutdown()


def test_real_http_slow_trickle_deadline(real_pycurl_module) -> None:
    """server 每 0.2s 返回一个 byte（总时长 > deadline）：必须硬 deadline 中断。"""
    requests: list = []
    server = make_server(requests, trickle_behavior)
    exporter = default_exporter(
        base_url=f"http://127.0.0.1:{server.server_address[1]}",
        total_deadline_seconds=1.0,
    )
    try:
        started = time.monotonic()
        with pytest.raises(AgentEvalOpsTraceExportError) as captured:
            exporter.send(make_envelope())
        elapsed = time.monotonic() - started
        assert captured.value.error_code == AGENTEVALOPS_TRACE_EXPORT_DEADLINE_EXCEEDED
        assert elapsed <= 1.0 + _DEADLINE_TOLERANCE
        assert len(requests) == 1
    finally:
        exporter.close(1.0)
        server.shutdown()


def test_real_http_response_too_large(real_pycurl_module) -> None:
    big = b"x" * (AGENTEVALOPS_MAX_RESPONSE_BODY_BYTES + 2048)
    requests: list = []
    server = make_server(requests, ok_behavior(201, body=big))
    exporter = default_exporter(
        base_url=f"http://127.0.0.1:{server.server_address[1]}"
    )
    try:
        started = time.monotonic()
        with pytest.raises(AgentEvalOpsTraceExportError) as captured:
            exporter.send(make_envelope())
        assert captured.value.error_code == (
            AGENTEVALOPS_TRACE_EXPORT_RESPONSE_TOO_LARGE
        )
        assert time.monotonic() - started <= 3.0 + _DEADLINE_TOLERANCE
        assert len(exporter._response_body) <= AGENTEVALOPS_MAX_RESPONSE_BODY_BYTES
        assert len(requests) == 1
    finally:
        exporter.close(1.0)
        server.shutdown()


def test_real_http_3xx_no_follow_single_post(real_pycurl_module) -> None:
    requests: list = []
    location_target: list = []

    def redirect_behavior(handler, request_body: bytes) -> None:
        handler.send_response(302)
        handler.send_header("Location", "http://127.0.0.1:1/integrations/localagent/v1/trace-envelopes")
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            requests.append(self.path)
            redirect_behavior(self, b"")

        def log_message(self, *args) -> None:  # noqa: D102
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    exporter = default_exporter(
        base_url=f"http://127.0.0.1:{server.server_address[1]}"
    )
    try:
        with pytest.raises(AgentEvalOpsTraceExportError) as captured:
            exporter.send(make_envelope())
        assert captured.value.error_code == AGENTEVALOPS_TRACE_EXPORT_FAILED
        assert captured.value.http_status == 302
        # redirect 目标（port 1，必然 refused）绝不被联系：恰一个 POST。
        assert requests == [AGENTEVALOPS_TRACE_EXPORT_PATH]
    finally:
        exporter.close(1.0)
        server.shutdown()


def test_real_http_body_then_reset_no_resend(real_pycurl_module) -> None:
    """Case B：server 收到 POST body 后 RST 连接；绝无第二个 POST。

    该 Windows libcurl 构建在等待响应时收到 RST，表现为 0 bytes received 的
    total deadline 到期（code 28）而不是即时 recv error；因此允许 FAILED 或
    DEADLINE_EXCEEDED 两种 bounded 分类——核心要求是恰一个 POST、无重发、
    在总 deadline 内返回。
    """
    requests: list = []
    server = make_server(requests, reset_after_body_behavior)
    exporter = default_exporter(
        base_url=f"http://127.0.0.1:{server.server_address[1]}"
    )
    try:
        envelope = make_envelope()
        started = time.monotonic()
        with pytest.raises(AgentEvalOpsTraceExportError) as captured:
            exporter.send(envelope)
        assert captured.value.error_code in {
            AGENTEVALOPS_TRACE_EXPORT_FAILED,
            AGENTEVALOPS_TRACE_EXPORT_DEADLINE_EXCEEDED,
        }
        # 整个 attempt 必须 bounded（总 deadline 内返回）。
        assert time.monotonic() - started <= 3.0 + _DEADLINE_TOLERANCE
        assert len(requests) == 1
        assert requests[0]["body"] == serialize_trace_export_envelope(envelope)
    finally:
        exporter.close(1.0)
        server.shutdown()


def test_real_http_stale_keepalive_one_post_per_envelope(real_pycurl_module) -> None:
    """Case A：server 在第一个响应后关闭 keepalive；第二个 envelope 恰一个 POST。"""
    requests: list = []
    server = make_server(requests, ok_behavior(201, close_connection=True))
    exporter = default_exporter(
        base_url=f"http://127.0.0.1:{server.server_address[1]}"
    )
    try:
        first = make_envelope(span_id="span-wp4c-stale-1")
        assert exporter.send(first) is None
        assert len(requests) == 1
        second = make_envelope(span_id="span-wp4c-stale-2")
        assert exporter.send(second) is None
        assert len(requests) == 2
        assert requests[1]["body"] == serialize_trace_export_envelope(second)
        # 同一 exporter 复用 easy handle；第二次仍是单次 POST，无重复。
        assert [len(r["body"]) for r in requests] == [
            len(serialize_trace_export_envelope(first)),
            len(serialize_trace_export_envelope(second)),
        ]
    finally:
        exporter.close(1.0)
        server.shutdown()


# ---------------------------------------------------------------------------
# 真实 TraceExportDispatcher 集成与 bounded shutdown
# ---------------------------------------------------------------------------


def test_dispatcher_integration_sends_via_exporter(real_pycurl_module) -> None:
    requests: list = []
    server = make_server(requests, ok_behavior(201))
    exporter = default_exporter(
        base_url=f"http://127.0.0.1:{server.server_address[1]}"
    )
    dispatcher = TraceExportDispatcher(exporter=exporter, queue_capacity=16)
    recorder = InMemorySpanRecorder(
        completion_observer=dispatcher.observe_completed_span
    )
    try:
        handle = recorder.start_span(
            trace_id="trace-wp4c-int-1",
            run_id="run-wp4c-int-1",
            component="test",
            operation="runtime.run",
        )
        handle.end_ok()
        assert dispatcher.flush(5.0) is True
        assert len(requests) == 1
        assert dispatcher.health().sent_total == 1
        # 投影产生的 envelope 时间戳来自 recorder，不能与合成 envelope 逐字节
        # 比较；校验 wire 关键字段与 serializer 结构一致。
        import json as json_module

        sent = json_module.loads(requests[0]["body"].decode("utf-8"))
        assert sent["contract_identity"] == TRACE_EXPORT_CONTRACT_IDENTITY
        assert sent["contract_version"] == TRACE_EXPORT_CONTRACT_VERSION
        assert sent["run_id"] == "run-wp4c-int-1"
        assert sent["trace_id"] == "trace-wp4c-int-1"
        assert sent["span_id"] == handle.context.span_id
        assert sent["operation"] == "runtime.run"
        assert sent["component"] == "test"
        assert sent["status"] == "OK"
        assert sent["attributes"] == {}
    finally:
        recorder.close()
        dispatcher.close(5.0)
        server.shutdown()


def test_dispatcher_shutdown_bounded_closes_exporter(real_pycurl_module) -> None:
    requests: list = []
    server = make_server(requests, silent_behavior)
    exporter = default_exporter(
        base_url=f"http://127.0.0.1:{server.server_address[1]}",
        total_deadline_seconds=1.0,
    )
    dispatcher = TraceExportDispatcher(exporter=exporter, queue_capacity=8)
    recorder = InMemorySpanRecorder(
        completion_observer=dispatcher.observe_completed_span
    )
    try:
        handle = recorder.start_span(
            trace_id="trace-wp4c-close-1",
            run_id="run-wp4c-close-1",
            component="test",
            operation="runtime.run",
        )
        handle.end_ok()
        # in-flight transport 被硬 deadline 终止为 ordinary failure，worker 继续。
        started = time.monotonic()
        assert dispatcher.close(5.0) is True
        assert time.monotonic() - started < 5.0
        health = dispatcher.health()
        assert health.state.value == "CLOSED"
        assert health.attempted_total == 1
        assert health.sent_total == 0
        assert health.failed_total == 1
        assert len(requests) == 1
        # exporter 已关闭：close 幂等，重复调用返回 True 且不再物理 cleanup。
        assert exporter.close(1.0) is True
    finally:
        recorder.close()
        server.shutdown()
