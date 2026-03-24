#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""智能体路由与多智能体编排模块。"""

import json
import re
from typing import Callable, Dict, Generator, Optional

from core.knowledge_base.vector_db_manager import VectorDBManager
from core.llm_engine import LocalLLMEngine
from core.memory_manager import MemoryManager


class AgentRouter:
    """协调提示词、工具调用、记忆、知识检索与多智能体协作。"""

    ORCHESTRATION_EVENT_PREFIX = "[[ORCH]]"

    def __init__(
        self,
        llm_engine: LocalLLMEngine,
        memory_manager: MemoryManager,
        db_manager: Optional[VectorDBManager] = None,
        *,
        history_window_size: int = 8,
        summary_trigger_messages: int = 16,
        summary_keep_recent: int = 8,
        summary_max_chars: int = 1000,
        rag_top_k: int = 3,
        rag_doc_max_chars: int = 700,
        rag_context_max_chars: int = 1500,
        max_tokens: int = 640,
        orchestration_enabled: bool = True,
        orchestration_max_agents: int = 3,
    ) -> None:
        """初始化路由器依赖与本地编排参数。

        Args:
            llm_engine: 本地大模型推理封装。
            memory_manager: 会话记忆持久化管理器。
            db_manager: 可选的本地向量检索管理器。
            history_window_size: 每轮推理保留的原始历史消息数。
            summary_trigger_messages: 触发滚动摘要的消息阈值。
            summary_keep_recent: 执行摘要后仍保留的近期消息数。
            summary_max_chars: 摘要最大字符数。
            rag_top_k: 知识库检索返回的文档数上限。
            rag_doc_max_chars: 单条检索文档的裁剪长度。
            rag_context_max_chars: 注入到提示词中的知识上下文总长度。
            max_tokens: 单轮生成的最大 token 数。
            orchestration_enabled: 是否允许核心 Agent 进行多智能体委派。
            orchestration_max_agents: 单次问题最多委派的专属 Agent 数量。
        """
        self.llm = llm_engine
        self.memory_manager = memory_manager
        self.db_manager = db_manager
        self.history_window_size = history_window_size
        self.summary_trigger_messages = summary_trigger_messages
        self.summary_keep_recent = summary_keep_recent
        self.summary_max_chars = summary_max_chars
        self.rag_top_k = rag_top_k
        self.rag_doc_max_chars = rag_doc_max_chars
        self.rag_context_max_chars = rag_context_max_chars
        self.max_tokens = max_tokens
        self.orchestration_enabled = orchestration_enabled
        self.orchestration_max_agents = orchestration_max_agents
        self.tools: Dict[str, Dict[str, object]] = {}
        self.agents_config = {
            "core_router": {
                "name": "Core Router",
                "role": "Route generic questions and coordinate helper agents.",
                "avatar": "avatar_router.png",
            },
            "data_analyst": {
                "name": "Data Analyst",
                "role": "Analyze CSV and Excel files and summarize insights.",
                "avatar": "avatar_excel.png",
            },
            "code_expert": {
                "name": "Code Expert",
                "role": "Review code, debug issues, and improve architecture.",
                "avatar": "avatar_code.png",
            },
            "knowledge_expert": {
                "name": "Knowledge Expert",
                "role": "Answer questions using the local knowledge base when available.",
                "avatar": "avatar_knowledge.png",
            },
        }
        self.delegate_agent_ids = ["data_analyst", "code_expert", "knowledge_expert"]

    def register_tool(self, name: str, func: Callable[[str], str], description: str) -> None:
        """注册一个可由模型触发的工具。"""
        self.tools[name] = {"func": func, "description": description}

    def _build_system_prompt(self, agent_id: str, allow_delegation: bool = False) -> str:
        """为指定智能体构造系统提示词。"""
        config = self.agents_config.get(agent_id, self.agents_config["core_router"])
        lines = [
            f"You are {config['name']}.",
            f"Your role: {config['role']}",
            "Reply clearly and concisely.",
        ]
        if allow_delegation and agent_id == "core_router":
            lines.extend(
                [
                    "You are allowed to orchestrate specialist agents when needed.",
                    "Available specialist agents:",
                    "- data_analyst: Analyze CSV, Excel, tabular data, metrics, and trends.",
                    "- code_expert: Review code, debug issues, explain implementation, and improve architecture.",
                    "- knowledge_expert: Answer questions grounded in the local knowledge base.",
                    (
                        "If specialists are needed, output ONLY one or more lines in this exact format: "
                        "Delegate: agent_id | task"
                    ),
                    "Do not delegate to core_router.",
                    "Do not include any extra commentary when emitting Delegate lines.",
                    "If specialists are not needed, answer the user directly.",
                ]
            )

        lines.extend(
            [
                "If a tool is required, output exactly one line in this format:",
                "Action: tool_name(argument_text)",
                "Available tools:",
            ]
        )
        for tool_name, tool_info in self.tools.items():
            lines.append(f"- {tool_name}: {tool_info['description']}")
        return "\n".join(lines)

    def _truncate_text(self, text: str, max_chars: int) -> str:
        """按字符数截断文本。"""
        compact = " ".join(text.split())
        if len(compact) <= max_chars:
            return compact
        return compact[: max_chars - 3] + "..."

    def _update_summary_if_needed(self, agent_id: str) -> str:
        """在历史超出阈值时滚动压缩旧消息。"""
        total_messages = self.memory_manager.count_messages(agent_id)
        if total_messages <= self.summary_trigger_messages:
            return self.memory_manager.get_summary_record(agent_id)["summary"]

        recent_messages = self.memory_manager.get_chat_history(
            agent_id=agent_id,
            limit=self.summary_keep_recent,
            ascending=True,
        )
        if not recent_messages:
            return self.memory_manager.get_summary_record(agent_id)["summary"]

        cutoff_id = recent_messages[0]["id"] - 1
        summary_record = self.memory_manager.get_summary_record(agent_id)
        new_messages = self.memory_manager.get_messages_for_summary(
            agent_id=agent_id,
            after_id=int(summary_record["last_message_id"]),
            before_id=cutoff_id,
        )
        if not new_messages:
            return str(summary_record["summary"])

        summary_lines = []
        if summary_record["summary"]:
            summary_lines.append(summary_record["summary"])

        for message in new_messages:
            role = "用户" if message["role"] == "user" else "助手"
            summary_lines.append(f"{role}: {self._truncate_text(message['content'], 120)}")

        merged_summary = "\n".join(summary_lines)
        if len(merged_summary) > self.summary_max_chars:
            merged_summary = merged_summary[-self.summary_max_chars :]

        self.memory_manager.save_summary(agent_id, merged_summary, cutoff_id)
        return merged_summary

    def _build_rag_context(self, user_query: str) -> str:
        """构建裁剪去重后的知识库上下文。"""
        if not self.db_manager:
            return ""

        docs = self.db_manager.search(user_query, k=self.rag_top_k)
        if not docs:
            return ""

        seen = set()
        segments = []
        total_chars = 0
        for doc in docs:
            normalized = " ".join(doc.page_content.split())
            if not normalized:
                continue
            dedup_key = normalized[:200]
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            snippet = normalized[: self.rag_doc_max_chars]
            if total_chars + len(snippet) > self.rag_context_max_chars:
                remaining = self.rag_context_max_chars - total_chars
                if remaining <= 0:
                    break
                snippet = snippet[:remaining]
            segments.append(snippet)
            total_chars += len(snippet)
            if total_chars >= self.rag_context_max_chars:
                break

        return "\n\n".join(segments)

    def _dedupe_current_user_message(
        self,
        history: list[dict[str, str]],
        user_query: str,
    ) -> list[dict[str, str]]:
        """移除历史尾部与当前输入重复的用户消息。"""
        if not history:
            return history
        last_message = history[-1]
        if last_message["role"] == "user" and last_message["content"] == user_query:
            return history[:-1]
        return history

    def _build_messages(
        self,
        user_query: str,
        agent_id: str,
        *,
        allow_delegation: bool = False,
    ) -> list[dict[str, str]]:
        """构建一次推理所需的完整消息序列。"""
        summary_text = self._update_summary_if_needed(agent_id)
        history = self.memory_manager.get_chat_history(
            agent_id=agent_id,
            limit=self.history_window_size,
            ascending=True,
        )
        history = self._dedupe_current_user_message(history, user_query)
        messages = [
            {
                "role": "system",
                "content": self._build_system_prompt(agent_id, allow_delegation=allow_delegation),
            }
        ]
        if summary_text:
            messages.append({"role": "system", "content": f"Conversation summary:\n{summary_text}"})
        messages.extend({"role": row["role"], "content": row["content"]} for row in history)

        if agent_id == "knowledge_expert" and self.db_manager:
            context = self._build_rag_context(user_query)
            if context:
                user_query = f"Knowledge base context:\n{context}\n\nUser question:\n{user_query}"

        messages.append({"role": "user", "content": user_query})
        return messages

    def _collect_model_response(self, messages: list[dict[str, str]]) -> str:
        """收集一次完整的模型输出文本。"""
        response_text = ""
        for chunk in self.llm.generate(messages, max_tokens=self.max_tokens):
            response_text += chunk
        return response_text

    def _run_tool_if_needed_text(
        self,
        response_text: str,
        messages: list[dict[str, str]],
    ) -> str:
        """在非流式场景下执行单轮工具调用。"""
        tool_match = re.search(r"Action:\s*(\w+)\((.*)\)", response_text)
        if not tool_match:
            return response_text

        tool_name = tool_match.group(1)
        tool_args = tool_match.group(2).strip()
        tool_info = self.tools.get(tool_name)
        if not tool_info:
            return response_text

        observation = str(tool_info["func"](tool_args))
        followup_messages = list(messages)
        followup_messages.append({"role": "assistant", "content": response_text})
        followup_messages.append({"role": "system", "content": f"Tool observation:\n{observation}"})
        return self._collect_model_response(followup_messages)

    def _run_tool_if_needed(
        self,
        response_text: str,
        messages: list[dict[str, str]],
    ) -> Generator[str, None, str]:
        """在流式场景下执行单轮工具调用。"""
        tool_match = re.search(r"Action:\s*(\w+)\((.*)\)", response_text)
        if not tool_match:
            return response_text

        tool_name = tool_match.group(1)
        tool_args = tool_match.group(2).strip()
        tool_info = self.tools.get(tool_name)
        if not tool_info:
            return response_text

        yield f"\n\n[tool] {tool_name}\n"
        observation = str(tool_info["func"](tool_args))
        messages.append({"role": "assistant", "content": response_text})
        messages.append({"role": "system", "content": f"Tool observation:\n{observation}"})

        final_response = ""
        for chunk in self.llm.generate(messages, max_tokens=self.max_tokens):
            final_response += chunk
            yield chunk
        return final_response

    def _complete_text(self, messages: list[dict[str, str]]) -> str:
        """在内存中完成一轮完整回复，包括必要的工具跟进。"""
        initial_response = self._collect_model_response(messages)
        return self._run_tool_if_needed_text(initial_response, messages)

    def _parse_delegate_plan(self, response_text: str) -> list[dict[str, str]]:
        """解析核心 Agent 输出的委派计划。"""
        delegates = []
        pattern = re.compile(r"^Delegate:\s*([a-z_]+)\s*\|\s*(.+)$", re.IGNORECASE)
        for line in response_text.splitlines():
            match = pattern.match(line.strip())
            if not match:
                continue
            delegate_id = match.group(1).lower()
            task = match.group(2).strip()
            if delegate_id not in self.delegate_agent_ids or not task:
                continue
            if any(item["agent_id"] == delegate_id for item in delegates):
                continue
            delegates.append({"agent_id": delegate_id, "task": task})
            if len(delegates) >= self.orchestration_max_agents:
                break
        return delegates

    def _build_orchestration_messages(self, user_query: str) -> list[dict[str, str]]:
        """构建核心 Agent 的委派规划消息。"""
        summary_text = self._update_summary_if_needed("core_router")
        history = self.memory_manager.get_chat_history(
            agent_id="core_router",
            limit=self.history_window_size,
            ascending=True,
        )
        history = self._dedupe_current_user_message(history, user_query)
        messages = [
            {
                "role": "system",
                "content": self._build_system_prompt("core_router", allow_delegation=True),
            }
        ]
        if summary_text:
            messages.append({"role": "system", "content": f"Conversation summary:\n{summary_text}"})
        messages.extend({"role": row["role"], "content": row["content"]} for row in history)
        messages.append({"role": "user", "content": user_query})
        return messages

    def _should_orchestrate(self, agent_id: str) -> bool:
        """判断当前轮是否允许触发多智能体编排。"""
        return self.orchestration_enabled and agent_id == "core_router"

    def _run_agent_once(self, agent_id: str, user_query: str, persist: bool = True) -> str:
        """执行一次非流式智能体调用。"""
        if persist:
            self.memory_manager.add_message(agent_id, "user", user_query)
        messages = self._build_messages(
            user_query=user_query,
            agent_id=agent_id,
            allow_delegation=False,
        )
        final_response = self._complete_text(messages)
        if persist:
            self.memory_manager.add_message(agent_id, "assistant", final_response)
        return final_response

    def _build_orchestration_event(self, event_type: str, **payload: object) -> str:
        """构建前后端约定的编排状态事件。"""
        event = {"type": event_type, **payload}
        return f"{self.ORCHESTRATION_EVENT_PREFIX}{json.dumps(event, ensure_ascii=False)}\n"

    def _plan_orchestration(self, user_query: str) -> dict[str, object]:
        """执行核心 Agent 的委派规划阶段。"""
        planning_messages = self._build_orchestration_messages(user_query)
        planning_response = self._collect_model_response(planning_messages)
        delegates = self._parse_delegate_plan(planning_response)
        return {
            "planning_messages": planning_messages,
            "planning_response": planning_response,
            "delegates": delegates,
        }

    def _build_synthesis_query(
        self,
        user_query: str,
        specialist_outputs: list[dict[str, str]],
    ) -> str:
        """将专属 Agent 结果整理为核心 Agent 的最终汇总输入。"""
        sections = [
            "User question:",
            user_query,
            "",
            "Specialist outputs:",
        ]
        for item in specialist_outputs:
            sections.extend(
                [
                    f"[{item['agent_name']}]",
                    f"Task: {item['task']}",
                    item["result"],
                    "",
                ]
            )
        sections.extend(
            [
                (
                    "Please synthesize the specialist outputs into one final answer for the user. "
                    "Do not emit Delegate lines. Do not ask the specialists again. "
                    "Keep the answer coherent and directly useful."
                )
            ]
        )
        return "\n".join(sections)

    def _stream_single_agent(
        self,
        user_query: str,
        agent_id: str,
    ) -> Generator[str, None, None]:
        """执行单智能体流式回复并持久化结果。"""
        messages = self._build_messages(
            user_query=user_query,
            agent_id=agent_id,
            allow_delegation=False,
        )
        initial_response = ""
        for chunk in self.llm.generate(messages, max_tokens=self.max_tokens):
            initial_response += chunk
            yield chunk

        tool_response = yield from self._run_tool_if_needed(initial_response, messages)
        final_response = tool_response if tool_response is not None else initial_response
        self.memory_manager.add_message(agent_id, "assistant", final_response)

    def _stream_core_with_orchestration(
        self,
        user_query: str,
    ) -> Generator[str, None, None]:
        """先执行编排，再流式输出核心 Agent 的最终汇总结果。"""
        yield self._build_orchestration_event("planning_started")
        orchestration_result = self._plan_orchestration(user_query)
        delegates = orchestration_result["delegates"]

        if not delegates:
            yield self._build_orchestration_event("planning_skipped")
            initial_response = str(orchestration_result["planning_response"])
            planning_messages = list(orchestration_result["planning_messages"])
            yield initial_response
            tool_response = yield from self._run_tool_if_needed(initial_response, planning_messages)
            final_response = tool_response if tool_response is not None else initial_response
            self.memory_manager.add_message("core_router", "assistant", final_response)
            return

        yield self._build_orchestration_event(
            "delegates_selected",
            agents=[
                {
                    "agent_id": item["agent_id"],
                    "agent_name": self.agents_config[item["agent_id"]]["name"],
                    "task": self._truncate_text(item["task"], 80),
                }
                for item in delegates
            ],
        )

        specialist_outputs = []
        for delegate in delegates:
            agent_id = delegate["agent_id"]
            task = delegate["task"]
            yield self._build_orchestration_event(
                "delegate_started",
                agent_id=agent_id,
                agent_name=self.agents_config[agent_id]["name"],
                task=self._truncate_text(task, 120),
            )
            result = self._run_agent_once(agent_id=agent_id, user_query=task, persist=True)
            specialist_outputs.append(
                {
                    "agent_id": agent_id,
                    "agent_name": self.agents_config[agent_id]["name"],
                    "task": task,
                    "result": result,
                }
            )
            yield self._build_orchestration_event(
                "delegate_finished",
                agent_id=agent_id,
                agent_name=self.agents_config[agent_id]["name"],
                summary=self._truncate_text(result, 120),
            )

        yield self._build_orchestration_event("synthesis_started")
        synthesis_query = self._build_synthesis_query(
            user_query=user_query,
            specialist_outputs=specialist_outputs,
        )
        messages = self._build_messages(
            user_query=synthesis_query,
            agent_id="core_router",
            allow_delegation=False,
        )
        initial_response = ""
        for chunk in self.llm.generate(messages, max_tokens=self.max_tokens):
            initial_response += chunk
            yield chunk

        tool_response = yield from self._run_tool_if_needed(initial_response, messages)
        final_response = tool_response if tool_response is not None else initial_response
        self.memory_manager.add_message(
            "core_router",
            "assistant",
            final_response,
            metadata={
                "orchestration": [
                    {
                        "agent_id": item["agent_id"],
                        "task": item["task"],
                    }
                    for item in specialist_outputs
                ]
            },
        )

    def chat_stream(self, user_query: str, agent_id: str = "core_router") -> Generator[str, None, None]:
        """执行一次对话，并持久化用户与助手消息。"""
        self.memory_manager.add_message(agent_id, "user", user_query)
        if self._should_orchestrate(agent_id):
            yield from self._stream_core_with_orchestration(user_query=user_query)
            return
        yield from self._stream_single_agent(user_query=user_query, agent_id=agent_id)

    def get_agent_meta(self, agent_id: str) -> tuple[str, str]:
        """返回智能体的显示名称与头像文件名。"""
        config = self.agents_config.get(agent_id, self.agents_config["core_router"])
        return str(config["name"]), str(config["avatar"])
