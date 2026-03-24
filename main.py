#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""桌面端应用入口。

该模块负责启动 PyQt 应用、协调桌宠与聊天面板，并通过 HTTP
与后端 FastAPI 服务通信。
"""

import os
import sys
import threading
import json
import faulthandler
from datetime import datetime

import requests
from PyQt6.QtCore import QObject, QThread, pyqtSignal
from PyQt6.QtWidgets import QApplication, QInputDialog, QMessageBox

from core.settings import Settings
from ui.chat_panel import ChatPanel
from ui.desktop_pet import DesktopPet

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from core.knowledge_base.wiki_crawler import WikiCrawler
except Exception:  # pragma: no cover
    BackgroundScheduler = None
    WikiCrawler = None


settings = Settings.load()
# 全局配置在进程启动时只解析一次，避免各组件重复读取环境变量。

ORCHESTRATION_EVENT_PREFIX = "[[ORCH]]"
_crash_log_dir = os.path.join(settings.project_root, "data", "logs")
os.makedirs(_crash_log_dir, exist_ok=True)
_crash_log_file = open(os.path.join(_crash_log_dir, "ui_crash.log"), "a", encoding="utf-8")
faulthandler.enable(_crash_log_file, all_threads=True)


class ApiWorker(QThread):
    """在 UI 线程之外执行流式聊天请求。

    Attributes:
        chunk_signal: 向界面发送缓冲后的增量文本。
        finished_signal: 当前请求完成时发出。
        error_signal: 当前请求失败时发出错误提示。
    """

    chunk_signal = pyqtSignal(str)
    status_signal = pyqtSignal(dict)
    finished_signal = pyqtSignal()
    error_signal = pyqtSignal(str)

    def __init__(self, api_url: str) -> None:
        """初始化工作线程。

        Args:
            api_url: 后端聊天接口地址。
        """
        super().__init__()
        self.api_url = api_url
        self.agent_id = ""
        self.query = ""
        self.file_path = ""
        self.http = requests.Session()
        self.protocol_buffer = ""

    def set_task(self, agent_id: str, query: str, file_path: str = "") -> None:
        """设置下一次执行所需的请求参数。

        Args:
            agent_id: 当前选中的智能体标识。
            query: 用户输入文本。
            file_path: 可选的附件路径。
        """
        self.agent_id = agent_id
        self.query = query
        self.file_path = file_path

    def run(self) -> None:
        """向后端发送流式请求并分批发射文本片段。"""
        try:
            self.protocol_buffer = ""
            response = self.http.post(
                self.api_url,
                json={
                    "agent_id": self.agent_id,
                    "query": self.query,
                    "file_path": self.file_path,
                },
                stream=True,
                timeout=300,
            )
            response.raise_for_status()

            buffer = ""
            for chunk in response.iter_content(chunk_size=128, decode_unicode=True):
                if not chunk:
                    continue
                buffer += chunk
                if len(buffer) >= 64 or "\n" in buffer:
                    self._emit_stream_payload(buffer)
                    buffer = ""

            if buffer:
                self._emit_stream_payload(buffer)
            if self.protocol_buffer:
                self.chunk_signal.emit(self.protocol_buffer)
                self.protocol_buffer = ""
            self.finished_signal.emit()
        except Exception as exc:
            self.error_signal.emit(f"API request failed: {exc}")

    def _emit_stream_payload(self, payload: str) -> None:
        """解析流中的编排事件，并转发普通文本片段。"""
        self.protocol_buffer += payload
        plain_parts: list[str] = []

        while True:
            marker_index = self.protocol_buffer.find(ORCHESTRATION_EVENT_PREFIX)
            if marker_index < 0:
                keep_length = self._get_protocol_suffix_length(self.protocol_buffer)
                if keep_length:
                    plain_parts.append(self.protocol_buffer[:-keep_length])
                    self.protocol_buffer = self.protocol_buffer[-keep_length:]
                else:
                    plain_parts.append(self.protocol_buffer)
                    self.protocol_buffer = ""
                break

            if marker_index > 0:
                plain_parts.append(self.protocol_buffer[:marker_index])
                self.protocol_buffer = self.protocol_buffer[marker_index:]

            line_end = self.protocol_buffer.find("\n")
            if line_end < 0:
                break

            marker_payload = self.protocol_buffer[len(ORCHESTRATION_EVENT_PREFIX) : line_end]
            self.protocol_buffer = self.protocol_buffer[line_end + 1 :]
            try:
                event = json.loads(marker_payload)
            except json.JSONDecodeError:
                plain_parts.append(f"{ORCHESTRATION_EVENT_PREFIX}{marker_payload}\n")
                continue
            self.status_signal.emit(event)

        plain_text = "".join(plain_parts)
        if plain_text:
            self.chunk_signal.emit(plain_text)

    @staticmethod
    def _get_protocol_suffix_length(text: str) -> int:
        """返回文本尾部与协议前缀重叠的长度。"""
        max_length = min(len(text), len(ORCHESTRATION_EVENT_PREFIX) - 1)
        for length in range(max_length, 0, -1):
            if ORCHESTRATION_EVENT_PREFIX.startswith(text[-length:]):
                return length
        return 0


class MainController(QObject):
    """协调桌面端 UI 状态与后端请求。"""

    history_ready_signal = pyqtSignal(list, str)
    history_prepend_signal = pyqtSignal(list, str)

    def __init__(self) -> None:
        """初始化控制器、窗口组件和后台任务。"""
        super().__init__()
        self.project_root = settings.project_root
        self.asset_dir = os.path.join(self.project_root, "ui", "assets")
        self.api_base_url = settings.api_base_url
        self.chat_api_url = f"{self.api_base_url}/api/chat"
        self.http = requests.Session()

        self.worker = ApiWorker(self.chat_api_url)
        self.worker.chunk_signal.connect(self._on_worker_chunk)
        self.worker.status_signal.connect(self._on_worker_status)
        self.worker.finished_signal.connect(self._on_worker_finished)
        self.worker.error_signal.connect(self._on_worker_error)

        # 每个智能体单独维护分页偏移量，避免切换会话时互相污染。
        self.agent_history_offsets: dict[str, int] = {}
        # 用集合做“进行中”标记，防止重复点击或重复滚动触发并发请求。
        self.fetching_history_agents: set[str] = set()
        # 首屏历史只加载一次，后续切换标签直接复用 UI 中已经存在的数据。
        self.loaded_history_agents: set[str] = set()

        self.pet = DesktopPet(
            default_img_path=os.path.join(self.asset_dir, "default.png"),
            drag_img_path=os.path.join(self.asset_dir, "drag.png"),
            initial_opacity=0.9,
        )
        self.chat_panel = ChatPanel(api_base_url=self.api_base_url)

        self._connect_signals()
        self._fetch_and_load_history(self.chat_panel.current_agent_id)
        self._start_daily_sync_task()
        self.pet.show()

    def _connect_signals(self) -> None:
        """连接桌宠、聊天面板和控制器之间的信号。"""
        self.pet.chat_requested.connect(self._show_chat_panel)
        self.pet.file_dropped.connect(self._handle_pet_file_drop)
        self.pet.quit_requested.connect(self._quit_application)
        self.chat_panel.message_sent.connect(self._handle_user_message)
        self.chat_panel.request_more_history_signal.connect(self._fetch_older_history)
        self.chat_panel.agent_switched_signal.connect(self._load_agent_history_if_needed)
        self.history_ready_signal.connect(self.chat_panel.load_history_messages)
        self.history_prepend_signal.connect(self.chat_panel.prepend_history_messages)

        from PyQt6.QtGui import QKeySequence, QShortcut

        self.search_shortcut = QShortcut(QKeySequence("Ctrl+F"), self.chat_panel)
        self.search_shortcut.activated.connect(self._open_search_dialog)

    def _start_daily_sync_task(self) -> None:
        """在配置开启时启动知识库定时同步任务。"""
        if not settings.sync_enabled or BackgroundScheduler is None or WikiCrawler is None:
            return

        self.scheduler = BackgroundScheduler()

        def sync_job():
            """执行一次完整的 Wiki 同步。"""
            if not settings.wiki_cookie:
                return
            # 同步任务只依赖配置，不接触前端状态，便于后续迁移到独立进程。
            crawler = WikiCrawler(
                cookie_str=settings.wiki_cookie,
                output_base_dir=settings.local_knowledge_base_dir,
            )
            crawler.run_full_sync(start_space=1, end_space=45)

        self.scheduler.add_job(sync_job, "cron", hour=2, minute=0)
        self.scheduler.start()

    def _load_agent_history_if_needed(self, agent_id: str) -> None:
        """按需加载某个智能体的首屏历史。

        Args:
            agent_id: 目标智能体标识。
        """
        if agent_id in self.loaded_history_agents:
            return
        self._fetch_and_load_history(agent_id)

    def _fetch_and_load_history(self, agent_id: str) -> None:
        """请求某个智能体的最新一页历史消息。"""
        self.agent_history_offsets[agent_id] = 0
        self._fetch_history_data(agent_id, is_prepend=False)

    def _fetch_older_history(self, agent_id: str) -> None:
        """请求某个智能体更早的一页历史消息。"""
        self._fetch_history_data(agent_id, is_prepend=True)

    def _fetch_history_data(self, agent_id: str, is_prepend: bool) -> None:
        """在后台线程中拉取历史消息。

        Args:
            agent_id: 目标智能体标识。
            is_prepend: 是否将结果插入到当前会话顶部。
        """
        if agent_id in self.fetching_history_agents:
            return
        self.fetching_history_agents.add(agent_id)
        # offset 代表“已经从最新开始消费了多少条”，与后端分页接口保持一致。
        offset = self.agent_history_offsets.get(agent_id, 0)
        limit = 10

        def fetch_task() -> None:
            """执行一次分页历史请求。"""
            try:
                with requests.Session() as http:
                    response = http.get(
                        f"{self.api_base_url}/api/history/{agent_id}",
                        params={"limit": limit, "offset": offset},
                        timeout=5,
                    )
                response.raise_for_status()
                messages = response.json().get("messages", [])
                if not is_prepend:
                    self.loaded_history_agents.add(agent_id)
                if messages:
                    # 只在后端真的返回数据时推进 offset，避免空页导致本地状态漂移。
                    self.agent_history_offsets[agent_id] = offset + len(messages)
                    signal = self.history_prepend_signal if is_prepend else self.history_ready_signal
                    signal.emit(messages, agent_id)
            except Exception as exc:
                print(f"[UI] history fetch failed for {agent_id}: {exc}")
            finally:
                self.fetching_history_agents.discard(agent_id)

        threading.Thread(target=fetch_task, daemon=True).start()

    def _open_search_dialog(self) -> None:
        """弹出搜索框并展示历史搜索结果。"""
        keyword, ok = QInputDialog.getText(self.chat_panel, "Search", "Enter a keyword:")
        if not ok or not keyword.strip():
            return

        try:
            response = self.http.get(
                f"{self.api_base_url}/api/search",
                params={"keyword": keyword.strip()},
                timeout=5,
            )
            response.raise_for_status()
            results = response.json().get("results", [])
            if not results:
                QMessageBox.information(self.chat_panel, "Search", "No messages found.")
                return

            lines = []
            for record in results[:20]:
                snippet = record["content"][:50].replace("\n", " ")
                lines.append(f"[{record['timestamp']}] {record['role']}: {snippet}")
            QMessageBox.information(self.chat_panel, "Search", "\n".join(lines))
        except Exception as exc:
            QMessageBox.warning(self.chat_panel, "Search", f"Search failed: {exc}")

    def _show_chat_panel(self) -> None:
        """显示并聚焦聊天窗口。"""
        self.chat_panel.showNormal()
        self.chat_panel.activateWindow()

    def _handle_pet_file_drop(self, file_path: str) -> None:
        """处理桌宠接收到的文件拖入事件。

        Args:
            file_path: 被拖入的本地文件路径。
        """
        self._show_chat_panel()
        self.chat_panel.attach_file(file_path)

    def _handle_user_message(self, agent_id: str, text: str, file_path: str) -> None:
        """发起新的对话请求。

        Args:
            agent_id: 当前智能体标识。
            text: 用户输入文本。
            file_path: 当前挂载的附件路径。
        """
        if self.worker.isRunning():
            # 单 worker 模式下直接拒绝第二个请求，避免前端状态与返回流交叉。
            self.chat_panel.append_system_msg("Assistant is still generating.", agent_id)
            return
        self.chat_panel.start_ai_msg(agent_id)
        self.worker.set_task(agent_id, text, file_path)
        self.worker.start()

    def _on_worker_chunk(self, chunk: str) -> None:
        """将流式文本片段推送给聊天面板。"""
        self.chat_panel.append_ai_chunk(chunk, target_agent_id=self.worker.agent_id)

    def _on_worker_status(self, event: dict) -> None:
        """将多智能体编排事件转换为 UI 可读状态。"""
        status_text = self._format_orchestration_status(event)
        if not status_text:
            return
        self.chat_panel.append_orchestration_status(status_text, target_agent_id=self.worker.agent_id)

    def _format_orchestration_status(self, event: dict) -> str:
        """将后端编排事件格式化为中文状态文案。"""
        event_type = event.get("type")
        if event_type == "planning_started":
            return "核心 Agent 正在判断是否需要委派专属 Agent。"
        if event_type == "planning_skipped":
            return "本轮无需委派，核心 Agent 直接回答。"
        if event_type == "delegates_selected":
            agents = event.get("agents", [])
            names = "、".join(item.get("agent_name", item.get("agent_id", "")) for item in agents if item)
            return f"已选择协作智能体：{names}" if names else "已生成协作计划。"
        if event_type == "delegate_started":
            agent_name = event.get("agent_name", event.get("agent_id", "专属 Agent"))
            task = event.get("task", "")
            return f"{agent_name} 开始处理：{task}" if task else f"{agent_name} 开始处理子任务。"
        if event_type == "delegate_finished":
            agent_name = event.get("agent_name", event.get("agent_id", "专属 Agent"))
            summary = event.get("summary", "")
            return f"{agent_name} 已返回结果：{summary}" if summary else f"{agent_name} 已完成子任务。"
        if event_type == "synthesis_started":
            return "核心 Agent 正在汇总专属 Agent 的结果。"
        return ""

    def _on_worker_finished(self) -> None:
        """在流式输出结束后完成最终渲染和侧边栏更新。"""
        agent_id = self.worker.agent_id
        # 节流渲染下最后一批文本可能仍在缓冲区，结束时强制刷新一次。
        self.chat_panel.flush_ai_render(agent_id)
        final_text = self.chat_panel.active_ai_texts.get(agent_id, "")
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.chat_panel.update_agent_sidebar_preview(agent_id, final_text, now_str)

    def _on_worker_error(self, error_msg: str) -> None:
        """将请求错误显示在聊天面板中。"""
        self.chat_panel.append_system_msg(error_msg, target_agent_id=self.worker.agent_id)

    def _quit_application(self) -> None:
        """安全关闭后台任务并退出应用。"""
        if self.worker.isRunning():
            self.worker.requestInterruption()
            self.worker.wait(2000)
        if hasattr(self, "scheduler"):
            self.scheduler.shutdown(wait=False)
        self.chat_panel.close()
        QApplication.quit()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    controller = MainController()
    sys.exit(app.exec())
