#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""聊天主面板。"""

import os
from datetime import datetime

from PyQt6.QtCore import QPoint, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QDragEnterEvent, QDropEvent, QIcon, QMouseEvent, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ui.chat_utils import format_chat_time, render_markdown_html
from ui.chat_widgets import AgentListItemWidget, MessageBubble, OrchestrationStatusWidget
from ui.memory_dialog import MemoryManagerDialog


class ChatPanel(QWidget):
    """桌面端聊天主面板。

    该组件负责渲染多智能体列表、聊天内容、附件状态和流式回复。
    """

    message_sent = pyqtSignal(str, str, str)
    stop_requested = pyqtSignal()
    request_more_history_signal = pyqtSignal(str)
    agent_switched_signal = pyqtSignal(str)
    memory_changed_signal = pyqtSignal(list, bool)

    def __init__(self, api_base_url: str) -> None:
        """初始化聊天主面板。

        Args:
            api_base_url: 后端 API 基础地址。
        """
        super().__init__()
        self.api_base_url = api_base_url
        self.current_agent_id = "core_router"
        self.attached_file_path = ""
        self._is_tracking = False
        self._start_pos = QPoint()

        self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.asset_dir = os.path.join(self.base_dir, "ui", "assets")
        self.user_avatar_path = self._resolve_asset_path("avatar_me.png", "avater_me.png")
        self.active_ai_labels: dict[str, QLabel] = {}
        self.active_ai_texts: dict[str, str] = {}
        self.active_status_widgets: dict[str, OrchestrationStatusWidget] = {}
        # 每个智能体各自维护一个节流定时器，避免多个会话互相抢刷新节奏。
        self._render_timers: dict[str, QTimer] = {}

        self._init_ui()
        self._apply_style()

    def _resolve_asset_path(self, *filenames: str) -> str:
        """按顺序查找可用的资源文件路径。"""
        for filename in filenames:
            candidate = os.path.join(self.asset_dir, filename)
            if os.path.exists(candidate):
                return candidate
        return os.path.join(self.asset_dir, filenames[0])

    def _find_asset_path(self, *filenames: str) -> str | None:
        """返回第一个存在的资源文件路径。

        该方法用于可选资源。若图标尚未放入资源目录，
        组件会退回到文本占位，避免因为缺图直接报错。
        """
        for filename in filenames:
            candidate = os.path.join(self.asset_dir, filename)
            if os.path.exists(candidate):
                return candidate
        return None

    def _build_nav_button(
        self,
        object_name: str,
        fallback_text: str,
        tooltip: str,
        *icon_filenames: str,
    ) -> QPushButton:
        """构建左侧导航按钮。

        Args:
            object_name: 按钮对象名，用于区分样式。
            fallback_text: 图标缺失时的文本占位。
            tooltip: 鼠标悬停提示。
            *icon_filenames: 依次尝试的图标文件名。

        Returns:
            已完成样式与图标配置的按钮。
        """
        button = QPushButton(fallback_text)
        button.setObjectName(object_name)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setToolTip(tooltip)
        button.setFixedSize(36, 36)

        icon_path = self._find_asset_path(*icon_filenames)
        if icon_path:
            button.setIcon(QIcon(icon_path))
            button.setIconSize(QSize(22, 22))
            button.setText("")

        return button

    def _get_agent_meta(self, agent_id: str) -> tuple[str, str]:
        """返回智能体的显示名称与头像路径。"""
        mapping = {
            "core_router": ("核心路由管家", self._resolve_asset_path("avatar_router.png")),
            "data_analyst": ("Excel 数据分析师", self._resolve_asset_path("avatar_excel.png", "avater_excel.png")),
            "code_expert": ("Python 代码专家", self._resolve_asset_path("avatar_code.png", "avater_code.png")),
            "knowledge_expert": ("Knowledge Expert", "avatar_knowledge.png"),
        }
        name, avatar_path = mapping.get(
            agent_id,
            ("AI 助手", self._resolve_asset_path("avatar_router.png")),
        )
        if agent_id == "knowledge_expert":
            return "本地知识专家", self._resolve_asset_path("avatar_knowledge.png")
        return name, avatar_path

    def attach_file(self, file_path: str) -> None:
        """将本地文件挂载到当前输入状态上。"""
        self.attached_file_path = file_path
        self.attachment_label.setText(f"Attached file: {os.path.basename(file_path)}")
        self.attachment_label.show()
        self._on_input_changed()

    def update_agent_sidebar_preview(self, agent_id: str, subtext: str, timestamp_str: str) -> None:
        """更新左侧列表中的摘要和时间信息。"""
        for index in range(self.agent_list.count()):
            item = self.agent_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) != agent_id:
                continue
            widget = self.agent_list.itemWidget(item)
            if isinstance(widget, AgentListItemWidget):
                widget.subtext_label.setText(subtext[:20].replace("\n", " "))
                widget.time_label.setText(format_chat_time(timestamp_str))
            break

    def _ensure_render_timer(self, agent_id: str) -> None:
        """为指定智能体准备流式渲染节流定时器。"""
        timer = self._render_timers.get(agent_id)
        if timer is None:
            timer = QTimer(self)
            timer.setInterval(40)
            timer.setSingleShot(True)
            # 使用 singleShot 节流而不是每个 chunk 立即重绘，避免 QLabel 全量 HTML 重算过于频繁。
            timer.timeout.connect(lambda aid=agent_id: self.flush_ai_render(aid))
            self._render_timers[agent_id] = timer

    def start_ai_msg(self, target_agent_id: str | None = None) -> None:
        """为即将到来的 AI 回复创建空白气泡。"""
        target_id = target_agent_id or self.current_agent_id
        name, avatar_path = self._get_agent_meta(target_id)
        bubble = MessageBubble("", is_user=False, avatar_path=avatar_path, agent_name=name)
        self.chat_layouts[target_id].addWidget(bubble)
        self.active_ai_labels[target_id] = bubble.msg_label
        self.active_ai_texts[target_id] = ""
        self.active_status_widgets.pop(target_id, None)
        self._ensure_render_timer(target_id)
        self._scroll_to_bottom(target_id)

    def append_ai_chunk(self, chunk: str, target_agent_id: str | None = None) -> None:
        """追加流式文本片段并触发节流渲染。"""
        target_id = target_agent_id or self.current_agent_id
        if target_id not in self.active_ai_labels:
            return
        # 只累积原始 Markdown，真正的 HTML 转换延迟到节流周期末尾统一执行。
        self.active_ai_texts[target_id] += chunk
        self._ensure_render_timer(target_id)
        self._render_timers[target_id].start()

    def flush_ai_render(self, agent_id: str) -> None:
        """将缓存中的 Markdown 文本渲染到对应气泡。"""
        label = self.active_ai_labels.get(agent_id)
        if label is None:
            return
        label.setText(render_markdown_html(self.active_ai_texts.get(agent_id, "")))
        self._scroll_to_bottom(agent_id)

    def _insert_message(self, msg: dict, agent_id: str, prepend: bool, index: int = 0) -> None:
        """将消息对象转换为气泡并插入布局。"""
        role = msg.get("role")
        content = msg.get("content", "")
        if role == "user":
            widget = MessageBubble(content, is_user=True, avatar_path=self.user_avatar_path)
        else:
            name, avatar_path = self._get_agent_meta(agent_id)
            widget = MessageBubble("", is_user=False, avatar_path=avatar_path, agent_name=name)
            # 历史消息是完整文本，直接一次性渲染，不走流式缓冲。
            widget.msg_label.setText(render_markdown_html(content))
        if prepend:
            self.chat_layouts[agent_id].insertWidget(index, widget)
        else:
            self.chat_layouts[agent_id].addWidget(widget)

    def load_history_messages(self, messages: list, target_agent_id: str | None = None) -> None:
        """加载首屏历史消息。"""
        target_id = target_agent_id or self.current_agent_id
        for msg in messages:
            self._insert_message(msg, target_id, prepend=False)
        if messages:
            last = messages[-1]
            self.update_agent_sidebar_preview(target_id, last.get("content", ""), last.get("timestamp", ""))
        self._scroll_to_bottom(target_id)

    def prepend_history_messages(self, messages: list, target_agent_id: str | None = None) -> None:
        """在顶部插入更早的历史消息，并补偿滚动位置。"""
        target_id = target_agent_id or self.current_agent_id
        scroll_bar = self.chat_scrolls[target_id].verticalScrollBar()
        old_value = scroll_bar.value()
        old_max = scroll_bar.maximum()
        for offset, msg in enumerate(messages):
            self._insert_message(msg, target_id, prepend=True, index=offset)

        def adjust() -> None:
            # 顶部插入内容后滚动条最大值会变大，这里补偿差值以保持视觉位置不跳动。
            scroll_bar.setValue(scroll_bar.maximum() - old_max + old_value)

        QTimer.singleShot(30, adjust)

    def reset_agent_messages(self, agent_id: str) -> None:
        """清空指定智能体当前画布中的消息气泡与流式状态。"""
        layout = self.chat_layouts.get(agent_id)
        if layout is None:
            return

        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        self.active_ai_labels.pop(agent_id, None)
        self.active_ai_texts.pop(agent_id, None)
        self.active_status_widgets.pop(agent_id, None)
        timer = self._render_timers.get(agent_id)
        if timer is not None:
            timer.stop()
        self.update_agent_sidebar_preview(agent_id, "", "")

    def reset_all_messages(self) -> None:
        """清空所有智能体当前画布中的消息气泡。"""
        for agent_id in list(self.chat_layouts.keys()):
            self.reset_agent_messages(agent_id)

    def _init_ui(self) -> None:
        """构建主面板的全部 UI 结构。"""
        self.setWindowTitle("Local Agent")
        self.resize(900, 650)
        self.setAcceptDrops(True)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self.central_widget = QWidget(self)
        self.central_widget.setObjectName("CentralWidget")
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(5, 5, 5, 5)
        main_layout.addWidget(self.central_widget)

        central_layout = QHBoxLayout(self.central_widget)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)

        nav_widget = QWidget()
        nav_widget.setObjectName("NavBar")
        nav_widget.setFixedWidth(60)
        nav_layout = QVBoxLayout(nav_widget)
        nav_layout.setContentsMargins(0, 30, 0, 20)
        nav_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)

        my_avatar = QLabel()
        my_avatar.setFixedSize(36, 36)
        if os.path.exists(self.user_avatar_path):
            my_avatar.setPixmap(
                QPixmap(self.user_avatar_path).scaled(
                    36,
                    36,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        nav_layout.addWidget(my_avatar)
        nav_layout.addSpacing(25)
        self.chat_nav_btn = self._build_nav_button(
            "NavPrimaryButton",
            "聊",
            "聊天",
            "nav_chat_active.png",
            "nav_chat.png",
        )
        nav_layout.addWidget(self.chat_nav_btn)
        nav_layout.addStretch(1)
        self.settings_btn = self._build_nav_button(
            "NavButton",
            "设",
            "记忆管理",
            "nav_settings.png",
        )
        self.settings_btn.clicked.connect(lambda: self._open_settings_dialog())
        nav_layout.addWidget(self.settings_btn)

        mid_widget = QWidget()
        mid_widget.setObjectName("MidBar")
        mid_widget.setFixedWidth(260)
        mid_layout = QVBoxLayout(mid_widget)
        mid_layout.setContentsMargins(0, 0, 0, 0)
        mid_layout.setSpacing(0)

        search_widget = QWidget()
        search_widget.setFixedHeight(65)
        search_layout = QHBoxLayout(search_widget)
        search_layout.setContentsMargins(12, 25, 12, 10)
        search_bar = QLabel("搜索")
        search_bar.setObjectName("SearchBar")
        search_bar.setFixedHeight(28)
        search_bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        add_btn = QPushButton("+")
        add_btn.setObjectName("AddBtn")
        add_btn.setFixedSize(28, 28)
        search_layout.addWidget(search_bar, stretch=1)
        search_layout.addSpacing(10)
        search_layout.addWidget(add_btn)

        self.agent_list = QListWidget()
        self.agent_list.setObjectName("AgentList")
        self.chat_stack = QStackedWidget()
        self.chat_layouts: dict[str, QVBoxLayout] = {}
        self.chat_scrolls: dict[str, QScrollArea] = {}

        for agent_id in ["core_router", "data_analyst", "code_expert", "knowledge_expert"]:
            name, avatar_path = self._get_agent_meta(agent_id)
            self._add_agent_item(agent_id, name, "No messages", "", avatar_path)

        self.agent_list.setCurrentRow(0)
        self.agent_list.currentRowChanged.connect(self._on_agent_switched)
        mid_layout.addWidget(search_widget)
        mid_layout.addWidget(self.agent_list)

        right_widget = QWidget()
        right_widget.setObjectName("RightPanel")
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        header_widget = QWidget()
        header_widget.setObjectName("RightHeader")
        header_widget.setFixedHeight(65)
        header_layout = QHBoxLayout(header_widget)
        header_layout.setContentsMargins(20, 0, 0, 0)
        self.header_label = QLabel("核心路由管家")
        self.header_label.setStyleSheet("font-size: 20px; color: #000; background: transparent;")
        self.min_btn = QPushButton("—")
        self.min_btn.setObjectName("WindowCtrlBtn")
        self.close_btn = QPushButton("×")
        self.close_btn.setObjectName("WindowCloseBtn")
        self.min_btn.clicked.connect(self.showMinimized)
        self.close_btn.clicked.connect(self.hide)
        header_layout.addWidget(self.header_label)
        header_layout.addStretch(1)
        header_layout.addWidget(self.min_btn)
        header_layout.addWidget(self.close_btn)

        line = QFrame()
        line.setObjectName("InputDivider")
        line.setFixedHeight(1)
        line.setFrameShape(QFrame.Shape.NoFrame)

        toolbar_widget = QWidget()
        toolbar_widget.setFixedHeight(40)
        toolbar_layout = QHBoxLayout(toolbar_widget)
        toolbar_layout.setContentsMargins(20, 5, 20, 0)
        toolbar_layout.setSpacing(15)
        for icon in ["😀", "📁", "✂", "🎤", "📞"]:
            label = QLabel(icon)
            label.setStyleSheet("font-size: 18px; color: #666; background: transparent;")
            toolbar_layout.addWidget(label)
        toolbar_layout.addStretch(1)

        input_widget = QWidget()
        input_widget.setFixedHeight(140)
        input_layout = QVBoxLayout(input_widget)
        input_layout.setContentsMargins(20, 0, 20, 15)
        self.attachment_label = QLabel("")
        self.attachment_label.setStyleSheet("color: #999; font-size: 12px; background: transparent;")
        self.attachment_label.hide()
        self.input_box = QTextEdit()
        self.input_box.setObjectName("InputBox")
        self.input_box.installEventFilter(self)
        self.input_box.textChanged.connect(self._on_input_changed)
        self.send_btn = QPushButton("发送(S)")
        self.send_btn.setObjectName("SendBtn")
        self.send_btn.setFixedSize(85, 30)
        self.send_btn.setEnabled(False)
        self.send_btn.clicked.connect(self._emit_message)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setObjectName("StopBtn")
        self.stop_btn.setFixedSize(85, 30)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_requested.emit)
        button_layout = QHBoxLayout()
        button_layout.addStretch(1)
        button_layout.addWidget(self.send_btn)
        button_layout.addWidget(self.stop_btn)
        input_layout.addWidget(self.attachment_label)
        input_layout.addWidget(self.input_box)
        input_layout.addLayout(button_layout)

        right_layout.addWidget(header_widget)
        right_layout.addWidget(self.chat_stack)
        right_layout.addWidget(line)
        right_layout.addWidget(toolbar_widget)
        right_layout.addWidget(input_widget)

        central_layout.addWidget(nav_widget)
        central_layout.addWidget(mid_widget)
        central_layout.addWidget(right_widget)

        for scroll_area in self.chat_scrolls.values():
            scroll_area.verticalScrollBar().valueChanged.connect(self._on_scroll)

    def _open_settings_dialog(self, event: QMouseEvent | None = None) -> None:
        """打开消息管理弹窗。"""
        if event is not None and event.button() != Qt.MouseButton.LeftButton:
            return
        dialog = MemoryManagerDialog(f"{self.api_base_url}/api/memory", parent=self)
        dialog.memory_changed.connect(self.memory_changed_signal.emit)
        dialog.exec()
        if event is not None:
            event.accept()

    def _on_scroll(self, value: int) -> None:
        """监听滚动条触顶事件并请求更多历史消息。"""
        if value == 0:
            self.request_more_history_signal.emit(self.current_agent_id)

    def _add_agent_item(self, agent_id: str, name: str, subtext: str, time_str: str, avatar_path: str) -> None:
        """同步添加侧边栏项目和会话画布。"""
        item = QListWidgetItem(self.agent_list)
        item.setData(Qt.ItemDataRole.UserRole, agent_id)
        item.setSizeHint(QSize(260, 64))
        widget = AgentListItemWidget(avatar_path, name, subtext, time_str)
        self.agent_list.addItem(item)
        self.agent_list.setItemWidget(item, widget)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 每个智能体独享一套滚动区域和布局，切换时只切栈，不重新创建内容。
        content_widget = QWidget()
        layout = QVBoxLayout(content_widget)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.setContentsMargins(0, 10, 0, 20)
        scroll_area.setWidget(content_widget)

        self.chat_layouts[agent_id] = layout
        self.chat_scrolls[agent_id] = scroll_area
        self.chat_stack.addWidget(scroll_area)

    def _apply_style(self) -> None:
        """注入主面板基础 QSS 样式。"""
        self.setStyleSheet(
            """
            QWidget { font-family: 'Microsoft YaHei', sans-serif; }
            #CentralWidget { background-color: #e6e5e4; border-radius: 10px; border: 1px solid #d4d4d4; }
            #NavBar { background-color: #2e2e2e; border-top-left-radius: 10px; border-bottom-left-radius: 10px; }
            #NavButton, #NavPrimaryButton {
                background-color: transparent;
                border: none;
                border-radius: 8px;
                color: #d8d8d8;
                font-size: 14px;
                font-weight: 600;
            }
            #NavButton:hover {
                background-color: rgba(255, 255, 255, 0.10);
                color: #ffffff;
            }
            #NavPrimaryButton {
                background-color: rgba(255, 255, 255, 0.12);
                color: #ffffff;
            }
            #NavPrimaryButton:hover { background-color: rgba(255, 255, 255, 0.18); }
            #MidBar { background-color: #e6e5e4; border: none; }
            #RightPanel {
                background-color: #f5f5f5;
                border-top-right-radius: 10px;
                border-bottom-right-radius: 10px;
                border-left: 1px solid #e5e5e5;
            }
            #SearchBar {
                background-color: #dbd9d8;
                color: #666;
                border-radius: 4px;
                font-size: 12px;
            }
            #AddBtn {
                background-color: #dbd9d8;
                color: #000;
                border: none;
                border-radius: 4px;
                font-size: 16px;
            }
            #AddBtn:hover { background-color: #d1cfce; }
            #AgentList { background-color: transparent; border: none; outline: none; }
            #AgentList::item:hover { background-color: #d8d8d8; }
            #AgentList::item:selected { background-color: #c6c5c4; }
            #RightHeader { border-bottom: 1px solid #e5e5e5; }
            #InputDivider {
                background-color: #ececec;
                border: none;
                min-height: 1px;
                max-height: 1px;
            }
            QScrollArea { border: none; background-color: transparent; }
            QScrollBar:vertical { border: none; background: transparent; width: 6px; margin: 0; }
            QScrollBar::handle:vertical { background: #c0c0c0; border-radius: 3px; min-height: 20px; }
            QScrollBar::handle:vertical:hover { background: #a0a0a0; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { border: none; background: none; }
            #WindowCtrlBtn {
                background-color: transparent;
                border: none;
                color: #000;
                font-size: 12px;
                min-width: 40px;
                min-height: 30px;
            }
            #WindowCtrlBtn:hover { background-color: #e2e2e2; }
            #WindowCloseBtn {
                background-color: transparent;
                border: none;
                color: #000;
                font-size: 14px;
                border-top-right-radius: 9px;
                min-width: 40px;
                min-height: 30px;
            }
            #WindowCloseBtn:hover { background-color: #fa5151; color: white; }
            #InputBox { background-color: transparent; border: none; font-size: 15px; color: #000; }
            #SendBtn {
                background-color: #e9e9e9;
                color: #b5b5b5;
                border: none;
                border-radius: 4px;
                font-size: 14px;
            }
            #SendBtn:enabled { background-color: #07c160; color: #fff; }
            #SendBtn:enabled:hover { background-color: #06ad56; }
            """
        )

    def _on_agent_switched(self, current_row: int) -> None:
        """处理当前智能体切换事件。"""
        item = self.agent_list.item(current_row)
        if item is None:
            return
        self.current_agent_id = item.data(Qt.ItemDataRole.UserRole)
        widget = self.agent_list.itemWidget(item)
        if isinstance(widget, AgentListItemWidget):
            self.header_label.setText(widget.name_label.text())
        self.chat_stack.setCurrentWidget(self.chat_scrolls[self.current_agent_id])
        # 切换标签后由控制器判断是否需要补拉历史，这里只负责广播状态变化。
        self.agent_switched_signal.emit(self.current_agent_id)
        QTimer.singleShot(30, lambda: self._scroll_to_bottom(self.current_agent_id))

    def _scroll_to_bottom(self, agent_id: str | None = None) -> None:
        """延迟滚动到指定会话的底部。

        这里避免直接调用 ``QApplication.processEvents()``，因为在流式回复、
        状态卡片插入和布局更新同时发生时，强制重入事件循环容易触发 Qt 原生层崩溃。
        """
        target_id = agent_id or self.current_agent_id
        scroll_area = self.chat_scrolls.get(target_id)
        if scroll_area is None:
            return

        def apply_scroll() -> None:
            scroll_bar = scroll_area.verticalScrollBar()
            scroll_bar.setValue(scroll_bar.maximum())

        QTimer.singleShot(0, apply_scroll)

    def _add_message_widget(
        self,
        text: str,
        is_user: bool,
        agent_name: str = "",
        target_agent_id: str | None = None,
    ) -> None:
        """向当前会话尾部追加一条新消息。"""
        target_id = target_agent_id or self.current_agent_id
        name, avatar_path = self._get_agent_meta(target_id)
        bubble = MessageBubble(
            text,
            is_user=is_user,
            avatar_path=self.user_avatar_path if is_user else avatar_path,
            agent_name=agent_name or name,
        )
        self.chat_layouts[target_id].addWidget(bubble)
        self._scroll_to_bottom(target_id)

    def append_system_msg(self, text: str, target_agent_id: str | None = None) -> None:
        """追加一条系统提示消息。"""
        target_id = target_agent_id or self.current_agent_id
        label = QLabel(text)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("color: #888; padding: 10px;")
        self.chat_layouts[target_id].addWidget(label)
        self._scroll_to_bottom(target_id)

    def append_orchestration_status(self, text: str, target_agent_id: str | None = None) -> None:
        """在当前会话中显示一条多智能体编排状态。"""
        target_id = target_agent_id or self.current_agent_id
        widget = self.active_status_widgets.get(target_id)
        if widget is None:
            widget = OrchestrationStatusWidget()
            self.chat_layouts[target_id].addWidget(widget)
            self.active_status_widgets[target_id] = widget
        widget.append_status(text)
        self._scroll_to_bottom(target_id)

    def _on_input_changed(self) -> None:
        """根据输入框和附件状态切换发送按钮可用性。"""
        self.send_btn.setEnabled(not self.stop_btn.isEnabled() and bool(self.input_box.toPlainText().strip() or self.attached_file_path))

    def set_streaming(self, streaming: bool) -> None:
        """切换发送/停止按钮，避免并行写入同一聊天输出。"""
        self.stop_btn.setEnabled(streaming)
        self._on_input_changed()

    def _emit_message(self) -> None:
        """收集输入内容并发出发送信号。"""
        text = self.input_box.toPlainText().strip()
        if not text and not self.attached_file_path:
            return
        display_text = text
        if self.attached_file_path:
            display_text = f"{display_text}\nAttached: {os.path.basename(self.attached_file_path)}".strip()
        self._add_message_widget(display_text, is_user=True)
        self.message_sent.emit(self.current_agent_id, text, self.attached_file_path)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 侧边栏预览优先展示用户刚发送的内容，等模型返回后再被助手摘要覆盖。
        self.update_agent_sidebar_preview(self.current_agent_id, text or display_text, now_str)
        self.input_box.clear()
        self.attached_file_path = ""
        self.attachment_label.hide()
        self._on_input_changed()

    def showEvent(self, event) -> None:
        """窗口显示后自动滚动到底部。"""
        super().showEvent(event)
        QTimer.singleShot(30, lambda: self._scroll_to_bottom(self.current_agent_id))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """支持拖动无边框主窗口。"""
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() < 65:
            self._is_tracking = True
            self._start_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """处理主窗口拖动。"""
        if self._is_tracking:
            self.move(event.globalPosition().toPoint() - self._start_pos)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        """结束主窗口拖动状态。"""
        if event.button() == Qt.MouseButton.LeftButton:
            self._is_tracking = False
            event.accept()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        """允许文件被拖入聊天面板。"""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        """处理拖入的附件文件。"""
        urls = event.mimeData().urls()
        if urls:
            self.attach_file(urls[0].toLocalFile())

    def eventFilter(self, obj, event) -> bool:
        """拦截输入框中的回车发送行为。"""
        if obj is self.input_box and event.type() == event.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if event.modifiers() == Qt.KeyboardModifier.ShiftModifier:
                    return False
                self._emit_message()
                return True
        return super().eventFilter(obj, event)
