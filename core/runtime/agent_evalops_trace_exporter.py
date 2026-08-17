#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AgentEvalOps Trace exporter：同步 PycURL/libcurl easy-handle HTTP transport。

WP4-C 的最小真实 transport：``TraceExportDispatcher`` 的单一 worker 串行调用
``send(envelope)``，本 exporter 用 WP4-B standalone serializer 得到 exact bytes
作为 HTTP body，对 AgentEvalOps 兼容端点执行**恰好一次**同步 HTTP POST，然后
分类 transport 结果。

冻结语义（见 handoff ``25_codex_transport_deadline_architecture_reentry.md``）：

- 一个 envelope = 一次 ``curl.perform()`` = 至多一个 wire POST；无 retry、无
  redirect、无 proxy、无 100-continue 协商。
- 总 deadline（``CURLOPT_TIMEOUT_MS``）覆盖 DNS/connect/TLS/upload/server
  processing/response 全程；connect 子界限（``CURLOPT_CONNECTTIMEOUT_MS``）
  包含在总 deadline 内。
- 只有一个 application-scoped easy handle，仅由 dispatcher worker 线程串行使用；
  ``close()`` 幂等，重复 close 不做二次物理 cleanup。
- 错误分类使用 bounded content-free local code；异常 ``str`` 只含固定 code，
  ``repr`` 只额外含 http status / curl code / duration 等安全事实。绝不包含
  API key、envelope、body、URL、ID 或 response 内容。

本模块顶层不 import pycurl：disabled 模式下即使导入本模块也不产生 PycURL
依赖；``send``/``close``/能力探测在首次需要时才 import pycurl，能力缺失或
PycURL 不可用时以固定 code 明确失败（enabled 时由 lifespan 构造路径使
startup-fatal）。
"""

from __future__ import annotations

import math
import time

from core.runtime.trace_export_contract import TraceExportEnvelope
from core.runtime.trace_export_serialization import (
    TraceExportSerializationError,
    serialize_trace_export_envelope,
)

# --- frozen transport constants（code-owned，不可配置） ----------------------
AGENTEVALOPS_TRACE_EXPORT_PATH = "/integrations/localagent/v1/trace-envelopes"
AGENTEVALOPS_MAX_RESPONSE_BODY_BYTES = 4096
AGENTEVALOPS_NOSIGNAL = 1
# libcurl CURLOPT_*_MS 是 long；Windows long 为 32-bit，显式封顶 fail closed。
AGENTEVALOPS_MAX_TIMEOUT_MS = 2_147_483_647

# --- stable local error codes（bounded vocabulary，不构成大 taxonomy） -------
AGENTEVALOPS_TRACE_EXPORT_FAILED = "AGENTEVALOPS_TRACE_EXPORT_FAILED"
AGENTEVALOPS_TRACE_EXPORT_DEADLINE_EXCEEDED = (
    "AGENTEVALOPS_TRACE_EXPORT_DEADLINE_EXCEEDED"
)
AGENTEVALOPS_TRACE_EXPORT_RESPONSE_TOO_LARGE = (
    "AGENTEVALOPS_TRACE_EXPORT_RESPONSE_TOO_LARGE"
)
AGENTEVALOPS_TRACE_EXPORT_CAPABILITY_MISSING = (
    "AGENTEVALOPS_TRACE_EXPORT_CAPABILITY_MISSING"
)
AGENTEVALOPS_TRACE_EXPORT_CLOSED = "AGENTEVALOPS_TRACE_EXPORT_CLOSED"
AGENTEVALOPS_TRACE_EXPORT_SERIALIZATION_FAILED = (
    "AGENTEVALOPS_TRACE_EXPORT_SERIALIZATION_FAILED"
)

# pycurl.version_info() 返回固定 tuple；features bitmask 与 protocols 位置稳定。
_LIBCURL_INFO_FEATURES_INDEX = 4
_LIBCURL_INFO_PROTOCOLS_INDEX = 8

# module-level 惰性缓存：顶层不 import pycurl，disabled 模式不产生 PycURL 依赖。
_pycurl_module = None


class AgentEvalOpsTraceExportError(RuntimeError):
    """Bounded content-free transport 失败。

    ``str`` 只包含固定 local error code；``repr`` 只额外包含 http status、
    curl error code 与 duration 等安全事实。任何构造路径都不接受 body、envelope、
    URL、ID 或 API key。
    """

    def __init__(
        self,
        error_code: str,
        *,
        http_status: int | None = None,
        curl_code: int | None = None,
        duration_ms: float | None = None,
    ) -> None:
        if not isinstance(error_code, str) or not error_code:
            raise ValueError("error_code must be a non-empty string")
        self.error_code = error_code
        self.http_status = http_status
        self.curl_code = curl_code
        self.duration_ms = duration_ms
        super().__init__(error_code)

    def __repr__(self) -> str:
        facts = f"error_code={self.error_code!r}"
        if self.http_status is not None:
            facts += f" http_status={self.http_status}"
        if self.curl_code is not None:
            facts += f" curl_code={self.curl_code}"
        if self.duration_ms is not None:
            facts += f" duration_ms={self.duration_ms:.3f}"
        return f"AgentEvalOpsTraceExportError({facts})"


def _get_pycurl():
    """惰性 import PycURL；不可用/导入失败以固定 code 明确失败。"""
    global _pycurl_module
    if _pycurl_module is None:
        try:
            import pycurl
        except Exception as exc:
            raise AgentEvalOpsTraceExportError(
                AGENTEVALOPS_TRACE_EXPORT_CAPABILITY_MISSING
            ) from exc
        _pycurl_module = pycurl
    return _pycurl_module


def _libcurl_capabilities(pycurl) -> tuple[int, tuple[str, ...]]:
    """返回 runtime libcurl 的 (features bitmask, protocols)。"""
    try:
        info = pycurl.version_info()
        features = info[_LIBCURL_INFO_FEATURES_INDEX]
        protocols = info[_LIBCURL_INFO_PROTOCOLS_INDEX]
    except Exception:
        return 0, ()
    if not isinstance(features, int) or not isinstance(protocols, tuple):
        return 0, ()
    return features, protocols


def _as_int_ms(seconds: float) -> int:
    """把正有限秒换算为整数毫秒；非整数毫秒或越界 fail closed。

    Settings 已做同等校验；构造函数防御重复此校验，保证任何构造路径都不会把
    非法/越界 timeout 传给 libcurl。
    """
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise ValueError("timeout must be a finite positive number of seconds")
    value = float(seconds)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("timeout must be a finite positive number of seconds")
    millis_float = value * 1000.0
    millis = round(millis_float)
    if not math.isclose(millis_float, millis) or not (
        1 <= millis <= AGENTEVALOPS_MAX_TIMEOUT_MS
    ):
        raise ValueError("timeout does not convert to a valid integer millisecond bound")
    return millis


def _require_http_url(base_url: str) -> None:
    """base_url 只允许 http/https origin URL（无 path/query/fragment/userinfo）。"""
    from urllib.parse import urlsplit

    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("base_url must be a non-empty http(s) URL")
    parsed = urlsplit(base_url.strip())
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("base_url must use http or https")
    if (
        not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("base_url must be an http(s) origin without path/query/fragment")


class AgentEvalOpsTraceExporter:
    """Envelope-only、同步、单 easy-handle 的 AgentEvalOps HTTP exporter。

    责任 ONLY：接收 ``TraceExportEnvelope`` → 用既有 serializer 序列化 → 一次
    同步 HTTP POST → 分类 transport 结果 → bounded close。不包含 projection、
    fingerprint、retry、batching、queue、ownership、digest 或 AgentEvalOps 语义。

    线程模型：只允许 dispatcher worker 串行调用 ``send``；``close`` 也由同一
    worker 经 sentinel 路径调用，因此 handle 无并发访问。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        project_id: str,
        connect_timeout_seconds: float,
        total_deadline_seconds: float,
    ) -> None:
        pycurl = _get_pycurl()
        _require_http_url(base_url)
        if not isinstance(api_key, str):
            raise TypeError("api_key must be a str")
        if not isinstance(project_id, str):
            raise TypeError("project_id must be a str")
        connect_ms = _as_int_ms(connect_timeout_seconds)
        total_ms = _as_int_ms(total_deadline_seconds)
        if connect_ms > total_ms:
            raise ValueError("connect timeout must not exceed total deadline")
        self._assert_libcurl_capabilities(pycurl, base_url)
        self._url = f"{base_url.rstrip('/')}{AGENTEVALOPS_TRACE_EXPORT_PATH}"
        self._api_key = api_key
        self._project_id = project_id
        self._connect_timeout_ms = connect_ms
        self._total_deadline_ms = total_ms
        # easy handle 只由 dispatcher worker 使用；构造即获取。
        self._curl = pycurl.Curl()
        self._closed = False
        # send-local response 缓冲状态（worker 串行，无并发访问）。
        self._response_body = bytearray()
        self._response_capped = False

    @staticmethod
    def _assert_libcurl_capabilities(pycurl, base_url: str) -> None:
        """enabled 启动门禁：AsynchDNS 必须存在；HTTPS 目标要求 TLS 能力。

        缺失任一能力即 fail closed，绝不 fallback 到其他 transport。
        """
        features, protocols = _libcurl_capabilities(pycurl)
        if not (features & pycurl.VERSION_ASYNCHDNS):
            raise AgentEvalOpsTraceExportError(
                AGENTEVALOPS_TRACE_EXPORT_CAPABILITY_MISSING
            )
        if base_url.strip().lower().startswith("https://"):
            if not (features & pycurl.VERSION_SSL) or "https" not in protocols:
                raise AgentEvalOpsTraceExportError(
                    AGENTEVALOPS_TRACE_EXPORT_CAPABILITY_MISSING
                )

    # --- TraceExporter protocol --------------------------------------------

    def send(self, envelope: TraceExportEnvelope) -> None:
        """对单个 envelope 执行恰好一次 transport attempt；成功返回 ``None``。

        序列化 bytes 原样作为 HTTP body（不重建 DTO/序列化路径）。非 2xx、
        3xx、deadline、response 超限或任何 transport 异常都抛
        ``AgentEvalOpsTraceExportError``（bounded、content-free）；本方法绝不在
        内部重发或重试。``send`` 只接受 ``TraceExportEnvelope``。
        """
        pycurl = _get_pycurl()
        if self._closed:
            raise AgentEvalOpsTraceExportError(AGENTEVALOPS_TRACE_EXPORT_CLOSED)
        if not isinstance(envelope, TraceExportEnvelope):
            raise TypeError("envelope must be a TraceExportEnvelope")
        try:
            body = serialize_trace_export_envelope(envelope)
        except TraceExportSerializationError as exc:
            raise AgentEvalOpsTraceExportError(
                AGENTEVALOPS_TRACE_EXPORT_SERIALIZATION_FAILED
            ) from exc
        started = time.monotonic()
        self._response_body = bytearray()
        self._response_capped = False
        try:
            self._perform(pycurl, body)
        except AgentEvalOpsTraceExportError:
            raise
        except Exception as exc:
            raise AgentEvalOpsTraceExportError(
                AGENTEVALOPS_TRACE_EXPORT_FAILED,
                duration_ms=(time.monotonic() - started) * 1000.0,
            ) from exc
        elapsed_ms = (time.monotonic() - started) * 1000.0
        status = self._curl.getinfo(pycurl.RESPONSE_CODE)
        if status < 200 or status >= 300:
            raise AgentEvalOpsTraceExportError(
                AGENTEVALOPS_TRACE_EXPORT_FAILED,
                http_status=status,
                duration_ms=elapsed_ms,
            )
        if self._response_capped:
            # 4096 上限在传输中已被 WRITEFUNCTION 触发 abort；属于 transport
            # failure 而不是 success。
            raise AgentEvalOpsTraceExportError(
                AGENTEVALOPS_TRACE_EXPORT_RESPONSE_TOO_LARGE,
                duration_ms=elapsed_ms,
            )

    def close(self, timeout_seconds: float) -> bool:
        """bounded/idempotent 物理关闭 easy handle；重复 close 不做二次 cleanup。"""
        if self._closed:
            return True
        self._closed = True
        try:
            self._curl.close()
        except Exception:
            return False
        return True

    # --- internal transport -------------------------------------------------

    def _perform(self, pycurl, body: bytes) -> None:
        """配置一次 request-local options 并执行唯一一次 ``curl.perform()``。

        ``curl.reset()`` 只重置 options、保留 connection cache；每个 send 都
        重建完整 request-local 配置。redirect/proxy/retry/100-continue 全部
        显式禁用；TLS 校验显式开启。
        """
        curl = self._curl
        curl.reset()
        curl.setopt(pycurl.URL, self._url)
        curl.setopt(pycurl.POST, 1)
        curl.setopt(pycurl.POSTFIELDS, body)
        curl.setopt(
            pycurl.HTTPHEADER,
            [
                "Content-Type: application/json",
                f"X-API-Key: {self._api_key}",
                f"X-Project-ID: {self._project_id}",
                "Expect:",
            ],
        )
        curl.setopt(pycurl.CONNECTTIMEOUT_MS, self._connect_timeout_ms)
        curl.setopt(pycurl.TIMEOUT_MS, self._total_deadline_ms)
        curl.setopt(pycurl.NOSIGNAL, AGENTEVALOPS_NOSIGNAL)
        curl.setopt(pycurl.FOLLOWLOCATION, 0)
        curl.setopt(pycurl.MAXREDIRS, 0)
        curl.setopt(pycurl.PROXY, "")
        curl.setopt(pycurl.SSL_VERIFYPEER, 1)
        curl.setopt(pycurl.SSL_VERIFYHOST, 2)
        curl.setopt(pycurl.HTTP_VERSION, pycurl.CURL_HTTP_VERSION_1_1)
        curl.setopt(pycurl.WRITEFUNCTION, self._on_body)
        try:
            curl.perform()
        except pycurl.error as exc:
            curl_code = exc.args[0] if exc.args else None
            if self._response_capped:
                raise AgentEvalOpsTraceExportError(
                    AGENTEVALOPS_TRACE_EXPORT_RESPONSE_TOO_LARGE,
                    curl_code=curl_code,
                ) from exc
            if curl_code == pycurl.E_OPERATION_TIMEDOUT:
                # 区分 connect 子界限超时（连接从未完成 → connection failed
                # family）与总 transfer deadline 到期（连接已建立后被中断）。
                # ``CURLINFO_CONNECT_TIME`` 为 0 表示连接阶段未完成。
                connect_time = self._curl.getinfo(pycurl.CONNECT_TIME)
                if connect_time is None or connect_time <= 0:
                    raise AgentEvalOpsTraceExportError(
                        AGENTEVALOPS_TRACE_EXPORT_FAILED,
                        curl_code=curl_code,
                    ) from exc
                raise AgentEvalOpsTraceExportError(
                    AGENTEVALOPS_TRACE_EXPORT_DEADLINE_EXCEEDED,
                    curl_code=curl_code,
                ) from exc
            raise AgentEvalOpsTraceExportError(
                AGENTEVALOPS_TRACE_EXPORT_FAILED,
                curl_code=curl_code,
            ) from exc

    def _on_body(self, chunk: bytes) -> int:
        """bounded WRITEFUNCTION：最多保留 4096 bytes，超限立即 short-count abort。"""
        remaining = AGENTEVALOPS_MAX_RESPONSE_BODY_BYTES - len(self._response_body)
        if remaining <= 0:
            self._response_capped = True
            return 0
        keep = min(len(chunk), remaining)
        self._response_body += chunk[:keep]
        if len(chunk) > keep:
            self._response_capped = True
        return keep


__all__ = [
    "AGENTEVALOPS_MAX_RESPONSE_BODY_BYTES",
    "AGENTEVALOPS_MAX_TIMEOUT_MS",
    "AGENTEVALOPS_NOSIGNAL",
    "AGENTEVALOPS_TRACE_EXPORT_CAPABILITY_MISSING",
    "AGENTEVALOPS_TRACE_EXPORT_CLOSED",
    "AGENTEVALOPS_TRACE_EXPORT_DEADLINE_EXCEEDED",
    "AGENTEVALOPS_TRACE_EXPORT_FAILED",
    "AGENTEVALOPS_TRACE_EXPORT_PATH",
    "AGENTEVALOPS_TRACE_EXPORT_RESPONSE_TOO_LARGE",
    "AGENTEVALOPS_TRACE_EXPORT_SERIALIZATION_FAILED",
    "AgentEvalOpsTraceExportError",
    "AgentEvalOpsTraceExporter",
]
