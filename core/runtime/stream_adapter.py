#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 Runtime Event 适配为当前桌面 UI 可消费的自定义文本块。"""

from __future__ import annotations

import json

from core.runtime.events import OutputDeltaPayload, RuntimeEvent


class RuntimeEventTextAdapter:
    """当前纯文本兼容层；这里没有实现标准 SSE frame 语义。"""

    ORCHESTRATION_EVENT_PREFIX = "[[ORCH]]"

    def encode(self, event: RuntimeEvent) -> str:
        if isinstance(event.payload, OutputDeltaPayload):
            return event.payload.text
        payload = event.to_safe_dict(include_output=False)
        return (
            self.ORCHESTRATION_EVENT_PREFIX
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            + "\n"
        )
