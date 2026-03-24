#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""桌面宠物悬浮组件。"""

import os

from PyQt6.QtCore import QPoint, QSettings, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QDragEnterEvent, QDropEvent, QMouseEvent, QPixmap
from PyQt6.QtWidgets import QLabel, QMenu, QVBoxLayout, QWidget


class DesktopPet(QWidget):
    """提供拖拽、双击唤起和右键菜单的桌面入口组件。"""

    file_dropped = pyqtSignal(str)
    chat_requested = pyqtSignal()
    quit_requested = pyqtSignal()

    def __init__(self, default_img_path: str, drag_img_path: str, initial_opacity: float = 0.9) -> None:
        """初始化桌面宠物窗口。

        Args:
            default_img_path: 默认状态图片路径。
            drag_img_path: 拖动状态图片路径。
            initial_opacity: 初始透明度。
        """
        super().__init__()
        self.default_img_path = default_img_path
        self.drag_img_path = drag_img_path
        self.drag_position = QPoint()
        self.settings = QSettings("LocalAgent", "DesktopPet")

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowOpacity(initial_opacity)
        self.setAcceptDrops(True)

        self.image_label = QLabel(self)
        self.image_label.setScaledContents(True)
        self._load_image(self.default_img_path)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.image_label)
        self.move(self.settings.value("pet_position", QPoint(100, 100)))

    def _load_image(self, path: str) -> None:
        """加载桌宠图片资源。"""
        if os.path.exists(path):
            pixmap = QPixmap(path)
            self.image_label.setPixmap(
                pixmap.scaled(150, 150, Qt.AspectRatioMode.KeepAspectRatio)
            )
        else:
            self.image_label.setText("Image missing")

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """进入拖动准备状态。"""
        if event.button() == Qt.MouseButton.LeftButton:
            self._load_image(self.drag_img_path)
            self.drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """随着鼠标移动更新桌宠位置。"""
        if event.buttons() == Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self.drag_position)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        """结束拖动并保存当前位置。"""
        if event.button() == Qt.MouseButton.LeftButton:
            self._load_image(self.default_img_path)
            self.settings.setValue("pet_position", self.pos())
            event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        """双击时请求打开聊天窗口。"""
        if event.button() == Qt.MouseButton.LeftButton:
            self.chat_requested.emit()
            event.accept()

    def contextMenuEvent(self, event) -> None:
        """显示右键菜单并分发命令。"""
        menu = QMenu(self)
        show_action = QAction("Open Chat", self)
        quit_action = QAction("Quit", self)
        menu.addAction(show_action)
        menu.addAction(quit_action)
        action = menu.exec(self.mapToGlobal(event.pos()))
        if action == show_action:
            self.chat_requested.emit()
        elif action == quit_action:
            self.settings.setValue("pet_position", self.pos())
            self.quit_requested.emit()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        """允许文件拖入桌宠区域。"""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        """将第一个拖入文件通过信号抛出。"""
        urls = event.mimeData().urls()
        if urls:
            self.file_dropped.emit(urls[0].toLocalFile())
