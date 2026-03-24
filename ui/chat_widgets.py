#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""聊天面板中的纯展示组件。"""

import os

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget


class AgentListItemWidget(QWidget):
    """左侧智能体列表中的单个项目组件。"""

    def __init__(self, avatar_path: str, name: str, subtext: str, time_str: str) -> None:
        """初始化侧边栏列表项。"""
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        self.avatar_label = QLabel()
        self.avatar_label.setFixedSize(40, 40)
        if os.path.exists(avatar_path):
            self.avatar_label.setPixmap(
                QPixmap(avatar_path).scaled(
                    40,
                    40,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            self.avatar_label.setStyleSheet("background-color: #d4d4d4; border-radius: 4px;")

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(2)
        self.name_label = QLabel(name)
        self.subtext_label = QLabel(subtext)
        self.time_label = QLabel(time_str)
        self.name_label.setStyleSheet(
            "font-size: 14px; color: #000; background: transparent;"
        )
        self.subtext_label.setStyleSheet(
            "font-size: 12px; color: #999; background: transparent;"
        )
        self.time_label.setStyleSheet(
            "font-size: 11px; color: #b2b2b2; background: transparent;"
        )
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        text_layout.addWidget(self.name_label)
        text_layout.addWidget(self.subtext_label)
        layout.addWidget(self.avatar_label)
        layout.addLayout(text_layout)
        layout.addWidget(self.time_label)


class MessageBubble(QWidget):
    """对话区中的单条消息气泡。"""

    def __init__(self, text: str, is_user: bool, avatar_path: str, agent_name: str = "") -> None:
        """初始化消息气泡组件。"""
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 10, 20, 10)

        avatar = QLabel()
        avatar.setFixedSize(40, 40)
        if os.path.exists(avatar_path):
            avatar.setPixmap(
                QPixmap(avatar_path).scaled(
                    40,
                    40,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )

        self.msg_label = QLabel(text)
        self.msg_label.setWordWrap(True)
        self.msg_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextBrowserInteraction
        )
        self.msg_label.setTextFormat(Qt.TextFormat.RichText)
        self.msg_label.setOpenExternalLinks(True)
        self.msg_label.setMaximumWidth(450)

        if is_user:
            self.msg_label.setStyleSheet(
                "background-color: #95ec69; border-radius: 8px; padding: 12px 15px; color: #111; font-size: 14px;"
            )
            layout.addStretch(1)
            layout.addWidget(self.msg_label)
            layout.addSpacing(10)
            layout.addWidget(avatar)
        else:
            self.msg_label.setStyleSheet(
                "background-color: #fff; border-radius: 8px; padding: 12px 15px; color: #111; font-size: 14px;"
            )
            vbox = QVBoxLayout()
            vbox.setContentsMargins(0, 0, 0, 0)
            vbox.setSpacing(5)
            name_label = QLabel(agent_name)
            name_label.setStyleSheet("color: #888; font-size: 12px;")
            vbox.addWidget(name_label)
            vbox.addWidget(self.msg_label)
            layout.addWidget(avatar)
            layout.addSpacing(10)
            layout.addLayout(vbox)
            layout.addStretch(1)


class OrchestrationStatusWidget(QWidget):
    """展示多智能体编排中间状态的卡片组件。"""

    def __init__(self) -> None:
        """初始化状态卡片。"""
        super().__init__()
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(20, 6, 20, 6)

        card = QWidget()
        card.setStyleSheet(
            "background: #f4f6f8; border: 1px solid #d7dde4; border-radius: 10px;"
        )
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(14, 12, 14, 12)
        card_layout.setSpacing(6)

        title_label = QLabel("Multi-Agent Orchestration")
        title_label.setStyleSheet("font-weight: 600; color: #314456;")
        card_layout.addWidget(title_label)

        self.status_layout = QVBoxLayout()
        self.status_layout.setContentsMargins(0, 0, 0, 0)
        self.status_layout.setSpacing(4)
        card_layout.addLayout(self.status_layout)
        root_layout.addWidget(card)

    def append_status(self, text: str) -> None:
        """追加一条新的编排状态。"""
        label = QLabel(f"- {text}")
        label.setWordWrap(True)
        label.setStyleSheet("color: #516273; font-size: 12px;")
        self.status_layout.addWidget(label)
