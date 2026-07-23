#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""桌面端独立取消短请求。"""

from __future__ import annotations

from collections.abc import Callable


def request_run_cancellation(post: Callable[..., object], cancel_url: str) -> bool:
    """发送短取消 POST；调用者可在独立线程中执行，不依赖流式读取线程。"""
    try:
        post(cancel_url, timeout=2)
        return True
    except Exception:
        return False
