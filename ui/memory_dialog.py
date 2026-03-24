#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""记忆管理弹窗。"""

import requests
from PyQt6.QtCore import Qt
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
    """用于查看和删除持久化消息的管理弹窗。"""

    def __init__(self, api_endpoint: str, parent=None) -> None:
        """初始化记忆管理弹窗。

        Args:
            api_endpoint: 后端记忆管理接口地址。
            parent: Qt 父组件。
        """
        super().__init__(parent)
        self.api_endpoint = api_endpoint
        self.http = requests.Session()
        self.setWindowTitle("Memory")
        self.resize(800, 500)
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

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Select", "ID", "Agent", "Role", "Preview"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)

        layout.addLayout(toolbar)
        layout.addWidget(self.table)

    def _load_data(self) -> None:
        """从后端拉取消息并刷新表格。"""
        self.table.setRowCount(0)
        try:
            response = self.http.get(self.api_endpoint, timeout=5)
            response.raise_for_status()
            messages = response.json().get("messages", [])
            for row_index, message in enumerate(messages):
                self.table.insertRow(row_index)
                check_item = QTableWidgetItem()
                check_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
                check_item.setCheckState(Qt.CheckState.Unchecked)
                preview = message["content"][:80].replace("\n", " ")
                self.table.setItem(row_index, 0, check_item)
                self.table.setItem(row_index, 1, QTableWidgetItem(str(message["id"])))
                self.table.setItem(row_index, 2, QTableWidgetItem(message["agent_id"]))
                self.table.setItem(row_index, 3, QTableWidgetItem(message["role"]))
                self.table.setItem(row_index, 4, QTableWidgetItem(preview))
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
        """删除当前勾选的消息。"""
        selected_ids = self._selected_ids()
        if not selected_ids:
            return
        try:
            self.http.delete(self.api_endpoint, json={"message_ids": selected_ids}, timeout=5)
            self._load_data()
        except Exception as exc:
            QMessageBox.warning(self, "Memory", f"Delete failed: {exc}")

    def _clear_all(self) -> None:
        """清空全部持久化消息。"""
        try:
            self.http.delete(self.api_endpoint, json={"delete_all": True}, timeout=5)
            self._load_data()
        except Exception as exc:
            QMessageBox.warning(self, "Memory", f"Clear failed: {exc}")
