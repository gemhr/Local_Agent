#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""TraceExporter 协议：transport-neutral、envelope-only 的 exporter contract。

WP4-B 的公共 adapter 契约：adapter 只接受 ``TraceExportEnvelope`` 并对每个
envelope 执行恰好一次 external delivery attempt；它不拥有 projection、
compatibility、queue、retry、batch 或 serialization 语义（这些属于
``TraceExportDispatcher`` 与 WP4-A contract Owner）。协议只用于类型标注；
真正的 runtime 类型边界由 ``TraceExportDispatcher`` 在构造与调用路径上
enforce（见 ``core/runtime/trace_export_dispatcher.py``）。

本模块不包含任何 transport、vendor、HTTP 或 generic wire serialization 语义。
"""

from __future__ import annotations

from typing import Protocol

from core.runtime.trace_export_contract import TraceExportEnvelope


class TraceExporter(Protocol):
    """Envelope-only、transport-neutral 的 exporter protocol。

    - ``send``：对单个 ``TraceExportEnvelope`` 执行恰好一次 transport
      attempt。成功返回只表示 adapter 定义的 attempt 边界完成，不代表 remote
      durable persistence 或 remote acknowledged。
    - ``close``：adapter 持有资源时的 bounded 物理关闭，必须在传入
      ``timeout_seconds`` 内返回 truthful bool。
    - 无 ``start/open``：adapter 构造即资源获取。
    - 无 adapter ``flush``：queue/pending-work 只由 dispatcher 拥有。
    - 禁止接收 raw ``SpanRecord``、mapping、dict 或 JSON 字符串。
    """

    def send(self, envelope: TraceExportEnvelope) -> None: ...

    def close(self, timeout_seconds: float) -> bool: ...


__all__ = ["TraceExporter"]
