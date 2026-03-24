#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""聊天 UI 的公共工具函数。"""

from datetime import datetime

import markdown


def format_chat_time(timestamp_str: str) -> str:
    """将数据库时间格式转换为侧边栏展示文本。

    Args:
        timestamp_str: ``YYYY-mm-dd HH:MM:SS`` 格式字符串。

    Returns:
        str: 格式化后的时间文本。
    """
    if not timestamp_str:
        return ""
    try:
        msg_time = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ""

    now = datetime.now()
    delta_days = (now.date() - msg_time.date()).days
    if delta_days == 0:
        return msg_time.strftime("%H:%M")
    if delta_days == 1:
        return "昨天"
    if delta_days == 2:
        return "前天"
    return "更早"


def render_markdown_html(text: str) -> str:
    """将 Markdown 文本转换为带样式的 HTML。

    Args:
        text: 原始 Markdown 文本。

    Returns:
        str: 可直接写入 ``QLabel`` 的 HTML 字符串。
    """
    safe_text = text
    # 流式输出中代码块经常尚未闭合；这里做一次自动补全，减少中途渲染抖动。
    if safe_text.count("```") % 2:
        safe_text += "\n```"
    body = markdown.markdown(safe_text, extensions=["fenced_code", "tables", "nl2br"])
    return (
        "<style>"
        "body { font-size: 14px; color: #111; font-family: 'Microsoft YaHei'; }"
        "strong, b { font-weight: bold; }"
        "pre { background: #f0f0f0; padding: 10px; }"
        "code { background: #f0f0f0; color: #b00020; }"
        "p { margin-top: 4px; margin-bottom: 4px; }"
        "</style>"
        f"{body}"
    )
