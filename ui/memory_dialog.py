#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""记忆管理弹窗。"""

import json

import requests
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)


class MemoryManagerDialog(QDialog):
    """用于查看、删除消息以及查看滚动摘要的弹窗。"""

    memory_changed = pyqtSignal(list, bool)

    def __init__(self, api_endpoint: str, parent=None, client_trust_env: bool = True) -> None:
        """初始化记忆管理弹窗。

        Args:
            api_endpoint: 后端记忆管理接口地址。
            parent: Qt 父组件。
            client_trust_env: 是否让本弹窗的 HTTP Session 继承系统 proxy；
                由 ChatPanel 从 main.py 启动期 Settings 快照透传，不做第二配置读取。
        """
        super().__init__(parent)
        self.api_endpoint = api_endpoint
        self.http = requests.Session()
        self.http.trust_env = client_trust_env
        self.setWindowTitle("Memory")
        self.resize(980, 680)
        self._init_ui()
        self._load_data()

    def _init_ui(self) -> None:
        """创建弹窗内部的表格与操作按钮。"""
        layout = QVBoxLayout(self)
        toolbar = QHBoxLayout()

        title = QLabel("Stored Messages")
        title.setStyleSheet("font-size: 16px; font-weight: bold;")

        refresh_btn = QPushButton("Refresh")
        delete_btn = QPushButton("Delete Selected")
        clear_btn = QPushButton("Clear All")
        refresh_btn.clicked.connect(self._load_data)
        delete_btn.clicked.connect(self._delete_selected)
        clear_btn.clicked.connect(self._clear_all)

        toolbar.addWidget(title)
        toolbar.addStretch(1)
        toolbar.addWidget(refresh_btn)
        toolbar.addWidget(delete_btn)
        toolbar.addWidget(clear_btn)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["Select", "ID", "Agent", "Scope", "Role", "Preview", "Metadata"]
        )
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)

        summary_title = QLabel("Conversation Summaries")
        summary_title.setStyleSheet("font-size: 15px; font-weight: bold; margin-top: 8px;")

        self.summary_table = QTableWidget(0, 4)
        self.summary_table.setHorizontalHeaderLabels(
            ["Agent", "Last Message ID", "Updated At", "Summary"]
        )
        self.summary_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.summary_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.summary_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.summary_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)

        layout.addLayout(toolbar)
        layout.addWidget(self.table)
        layout.addWidget(summary_title)
        layout.addWidget(self.summary_table)

    def _load_data(self) -> None:
        """从后端拉取消息与摘要并刷新表格。"""
        self.table.setRowCount(0)
        self.summary_table.setRowCount(0)
        try:
            response = self.http.get(self.api_endpoint, timeout=5)
            response.raise_for_status()
            payload = response.json()
            messages = payload.get("messages", [])
            summaries = payload.get("summaries", [])

            for row_index, message in enumerate(messages):
                self.table.insertRow(row_index)
                check_item = QTableWidgetItem()
                check_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
                check_item.setCheckState(Qt.CheckState.Unchecked)
                preview = message["content"][:80].replace("\n", " ")
                metadata_preview = json.dumps(
                    message.get("metadata", {}),
                    ensure_ascii=False,
                )[:120]
                self.table.setItem(row_index, 0, check_item)
                self.table.setItem(row_index, 1, QTableWidgetItem(str(message["id"])))
                self.table.setItem(row_index, 2, QTableWidgetItem(message["agent_id"]))
                self.table.setItem(row_index, 3, QTableWidgetItem(message.get("memory_scope", "direct")))
                self.table.setItem(row_index, 4, QTableWidgetItem(message["role"]))
                self.table.setItem(row_index, 5, QTableWidgetItem(preview))
                self.table.setItem(row_index, 6, QTableWidgetItem(metadata_preview))

            for row_index, summary in enumerate(summaries):
                self.summary_table.insertRow(row_index)
                self.summary_table.setItem(row_index, 0, QTableWidgetItem(summary["agent_id"]))
                self.summary_table.setItem(
                    row_index,
                    1,
                    QTableWidgetItem(str(summary["last_message_id"])),
                )
                self.summary_table.setItem(row_index, 2, QTableWidgetItem(summary["updated_at"]))
                self.summary_table.setItem(row_index, 3, QTableWidgetItem(summary["summary"]))
        except Exception as exc:
            QMessageBox.warning(self, "Memory", f"Failed to load memory: {exc}")

    def _selected_ids(self) -> list[int]:
        """收集当前表格中被勾选的消息 ID。"""
        ids = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                ids.append(int(self.table.item(row, 1).text()))
        return ids

    def _delete_selected(self) -> None:
        """删除当前勾选的消息，并通知主界面刷新。"""
        selected_ids = self._selected_ids()
        if not selected_ids:
            return
        try:
            response = self.http.delete(
                self.api_endpoint,
                json={"message_ids": selected_ids},
                timeout=5,
            )
            response.raise_for_status()
            payload = response.json()
            self._load_data()
            self.memory_changed.emit(payload.get("refresh_agent_ids", []), False)
        except Exception as exc:
            QMessageBox.warning(self, "Memory", f"Delete failed: {exc}")

    def _clear_all(self) -> None:
        """清空全部持久化消息，并通知主界面刷新。"""
        try:
            response = self.http.delete(
                self.api_endpoint,
                json={"delete_all": True},
                timeout=5,
            )
            response.raise_for_status()
            payload = response.json()
            self._load_data()
            self.memory_changed.emit(payload.get("refresh_agent_ids", []), True)
        except Exception as exc:
            QMessageBox.warning(self, "Memory", f"Clear failed: {exc}")
