#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Client 启动期的一次性 bounded readiness probe。

本模块是 Client Readiness Worker 的实现 Owner，**不是** Server lifecycle /
admission Owner。它只消费 `GET /readyz` 的 HTTP 响应，不写回任何 Runtime 状态。

模型（第一版冻结）：
- startup-only：一次 Client startup readiness probe；
- 一个 QThread（ReadinessWorker）；
- bounded probe：固定 deadline / per-request timeout / fixed interval；
- 无 continuous monitoring、无 reconnect state machine、无 manual button。

Worker 不读取 Settings / env；`api_base_url` 与 `client_trust_env` 由
main.py 启动期 Settings 快照传入。
"""

from __future__ import annotations

import time
from typing import Callable

import requests
from PyQt6.QtCore import QThread, pyqtSignal

# Client startup readiness policy 的代码常量；不是 operator-facing Runtime
# configuration，不写入 Settings。不得修改这些数字。
PER_REQUEST_TIMEOUT_SECONDS = 1.0
TOTAL_READINESS_DEADLINE_SECONDS = 30.0
RETRY_INTERVAL_SECONDS = 0.5
JITTER = 0.0  # JITTER = NONE

# WP1-C typed readiness contract 的两个合法 handshake success 组合。
# Client 只消费 HTTP contract，不复制 Server projector，也不引入 Runtime
# lifecycle/admission Authority；以下均为本模块私有的 immutable 常量。
_READY_STATUS = "READY"
_READY_DEGRADED_STATUS = "READY_DEGRADED"
_READY_LIFECYCLE = "READY"
_READY_ADMISSION = "ACCEPTING"

# 合法四字段 schema。
_REQUIRED_FIELDS = ("status", "lifecycle", "admission", "degraded")


def _is_valid_ready_body(body: object) -> bool:
    """校验 /readyz body 是否属于两个合法 ready combination 之一。

    成功必须同时满足：
    - body 是 dict 且恰好包含 status/lifecycle/admission/degraded 四字段；
    - status/lifecycle/admission 均为 str，degraded 为 bool
      （bool 是 int 子类；不接受 "false"/0/None 等非 bool 值）；
    - 且 body 完整等于下列组合之一：
      status=READY + lifecycle=READY + admission=ACCEPTING + degraded=False；
      status=READY_DEGRADED + lifecycle=READY + admission=ACCEPTING
        + degraded=True。

    任何 missing/extra key、未知值或跨字段矛盾都 fail closed 为 False，
    由调用方在 deadline 内继续 bounded retry。不尝试“纠正”或猜测
    Server 意图。
    """
    if not isinstance(body, dict):
        return False
    if set(body.keys()) != set(_REQUIRED_FIELDS):
        return False
    status = body.get("status")
    lifecycle = body.get("lifecycle")
    admission = body.get("admission")
    degraded = body.get("degraded")
    if not isinstance(status, str):
        return False
    if not isinstance(lifecycle, str):
        return False
    if not isinstance(admission, str):
        return False
    if not isinstance(degraded, bool):
        return False
    if lifecycle != _READY_LIFECYCLE or admission != _READY_ADMISSION:
        return False
    if status == _READY_STATUS:
        return degraded is False
    if status == _READY_DEGRADED_STATUS:
        return degraded is True
    return False


def _probe_once(
    session: requests.Session,
    readyz_url: str,
    timeout: float,
) -> bool:
    """执行一次 GET /readyz 并判断是否 ready。

    任何非 200、malformed JSON、invalid schema、200 + non-ready status
    都视为本 attempt 未成功（返回 False），由调用方在 deadline 内重试。
    """
    try:
        response = session.get(readyz_url, timeout=timeout)
    except requests.RequestException:
        return False
    if response.status_code != 200:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    return _is_valid_ready_body(body)


def run_bounded_readiness_probe(
    session: requests.Session,
    readyz_url: str,
    *,
    interruption_check: Callable[[], bool] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """在固定 deadline 内执行 bounded readiness probe。

    Args:
        session: worker 自建的 requests.Session（trust_env 已由调用方设置）。
        readyz_url: 完整 `GET /readyz` URL。
        interruption_check: 返回 True 表示请求中断；None 表示不检查。
        monotonic: 单调时钟（测试可注入）。
        sleep: 固定间隔 sleep（测试可注入）。

    Returns:
        True 表示在 deadline 内成功（HTTP 200 + typed ready body）；
        False 表示 deadline 耗尽或 interruption。
    """
    deadline = monotonic() + TOTAL_READINESS_DEADLINE_SECONDS
    while True:
        if interruption_check is not None and interruption_check():
            return False
        remaining = deadline - monotonic()
        if remaining <= 0:
            return False
        request_timeout = min(PER_REQUEST_TIMEOUT_SECONDS, remaining)
        if _probe_once(session, readyz_url, request_timeout):
            return True
        if interruption_check is not None and interruption_check():
            return False
        remaining = deadline - monotonic()
        if remaining <= 0:
            return False
        sleep(min(RETRY_INTERVAL_SECONDS, remaining))


class ReadinessWorker(QThread):
    """一次 Client startup readiness probe 的 QThread wrapper。

    Owner：MainController。active lifetime：一次 startup probe。
    成功发出 ready_signal；deadline/cancel/failure 发出一次 unavailable_signal
    后退出。Session 在 worker terminal 时关闭。
    """

    ready_signal = pyqtSignal()
    unavailable_signal = pyqtSignal()

    def __init__(self, api_base_url: str, client_trust_env: bool) -> None:
        """初始化 readiness worker。

        Args:
            api_base_url: Server API 基础地址（由 main.py Settings 快照传入）。
            client_trust_env: 是否让本 worker 的 Session 继承系统 proxy。
        """
        super().__init__()
        self._readyz_url = f"{api_base_url}/readyz"
        self._client_trust_env = client_trust_env
        self._session: requests.Session | None = None

    def run(self) -> None:
        """执行一次 bounded readiness probe 并发出 terminal signal。"""
        session = requests.Session()
        session.trust_env = self._client_trust_env
        self._session = session
        try:
            ready = run_bounded_readiness_probe(
                session,
                self._readyz_url,
                interruption_check=self.isInterruptionRequested,
            )
            if ready:
                self.ready_signal.emit()
            else:
                self.unavailable_signal.emit()
        finally:
            session.close()
            self._session = None


__all__ = [
    "JITTER",
    "PER_REQUEST_TIMEOUT_SECONDS",
    "RETRY_INTERVAL_SECONDS",
    "ReadinessWorker",
    "TOTAL_READINESS_DEADLINE_SECONDS",
    "run_bounded_readiness_probe",
]