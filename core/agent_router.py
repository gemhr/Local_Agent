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
    DIRECT_MEMORY_SCOPE = "direct"
    ORCHESTRATION_MEMORY_SCOPE = "orchestration"

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
        """初始化路由器依赖与本地编排参数。"""
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
        self.tool_plan_max_tokens = 48
        self.summary_plan_max_tokens = 256
        self.knowledge_rewrite_max_tokens = 24
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

    def _build_system_prompt(
        self,
        agent_id: str,
        *,
        allow_delegation: bool = False,
    ) -> str:
        """为指定智能体构造回答提示词。"""
        config = self.agents_config.get(agent_id, self.agents_config["core_router"])
        lines = [
            f"You are {config['name']}.",
            f"Your role: {config['role']}",
            "Reply clearly and concisely in Chinese unless the user asks otherwise.",
        ]
        if agent_id == "knowledge_expert":
            lines.extend(
                [
                    "你必须优先依据本地知识库信源回答。",
                    "如果用户消息包含【系统提供的参考资料】，请优先使用资料内容并在答案末尾增加“参考来源：”列表。",
                    "参考来源格式固定为“[序号] 文件路径或来源名”。",
                    "如果没有可用本地信源，先明确写“未找到对应信源”，再给出通用回答。",
                ]
            )
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
        return "\n".join(lines)

    def _build_tool_planner_prompt(self, agent_id: str) -> str:
        """构造工具规划提示词。"""
        config = self.agents_config.get(agent_id, self.agents_config["core_router"])
        lines = [
            f"You are deciding whether {config['name']} needs a local tool before answering.",
            "Return exactly one line.",
            "If no tool is needed, return: NO_TOOL",
            "If one tool is needed, return: CALL: tool_name(argument_text)",
            "Do not answer the user directly.",
        ]
        if self.tools:
            lines.append("Available tools:")
            for tool_name, tool_info in self.tools.items():
                lines.append(f"- {tool_name}: {tool_info['description']}")
        return "\n".join(lines)

    def _truncate_text(self, text: str, max_chars: int) -> str:
        """按字符数截断文本。"""
        compact = " ".join(text.split())
        if len(compact) <= max_chars:
            return compact
        return compact[: max_chars - 3] + "..."

    def _collect_model_response(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: Optional[int] = None,
        temperature: float = 0.7,
    ) -> str:
        """收集一次完整的模型输出。"""
        response_text = ""
        for chunk in self.llm.generate(
            messages,
            max_tokens=max_tokens or self.max_tokens,
            temperature=temperature,
        ):
            response_text += chunk
        return response_text

    def _fallback_summary(self, existing_summary: str, new_messages: list[dict[str, str]]) -> str:
        """在 LLM 摘要失败时使用规则压缩旧消息。"""
        summary_lines = []
        if existing_summary:
            summary_lines.append(existing_summary)

        for message in new_messages:
            role = "用户" if message["role"] == "user" else "助手"
            summary_lines.append(f"{role}: {self._truncate_text(message['content'], 120)}")

        merged_summary = "\n".join(summary_lines)
        if len(merged_summary) > self.summary_max_chars:
            merged_summary = merged_summary[-self.summary_max_chars :]
        return merged_summary

    def _distill_summary(self, existing_summary: str, new_messages: list[dict[str, str]]) -> str:
        """将旧对话蒸馏为结构化摘要。"""
        transcript = []
        for message in new_messages:
            role = "用户" if message["role"] == "user" else "助手"
            transcript.append(f"{role}: {self._truncate_text(message['content'], 180)}")

        summary_messages = [
            {
                "role": "system",
                "content": (
                    "你是对话记忆整理器。请把给定对话蒸馏为长期有用的结构化摘要。\n"
                    "保留四类信息：长期事实、偏好约束、关键结论、待办事项。\n"
                    "输出要求：\n"
                    "1. 只使用中文。\n"
                    "2. 小节标题固定为：长期事实、偏好约束、关键结论、待办事项。\n"
                    "3. 每节最多 3 条，缺失时写“无”。\n"
                    "4. 不要复述纯闲聊，不要输出解释。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"已有摘要：\n{existing_summary or '无'}\n\n"
                    f"新增历史：\n{chr(10).join(transcript) or '无'}"
                ),
            },
        ]
        summary = self._collect_model_response(
            summary_messages,
            max_tokens=self.summary_plan_max_tokens,
            temperature=0.1,
        ).strip()
        if not summary:
            return self._fallback_summary(existing_summary, new_messages)
        if len(summary) > self.summary_max_chars:
            summary = summary[-self.summary_max_chars :]
        return summary

    def _update_summary_if_needed(self, agent_id: str) -> str:
        """在历史超出阈值时滚动压缩旧消息。"""
        total_messages = self.memory_manager.count_messages(
            agent_id,
            memory_scope=self.DIRECT_MEMORY_SCOPE,
        )
        if total_messages <= self.summary_trigger_messages:
            return self.memory_manager.get_summary_record(agent_id)["summary"]

        recent_messages = self.memory_manager.get_chat_history(
            agent_id=agent_id,
            limit=self.summary_keep_recent,
            ascending=False,
            memory_scope=self.DIRECT_MEMORY_SCOPE,
        )
        if not recent_messages:
            return self.memory_manager.get_summary_record(agent_id)["summary"]
        recent_messages = list(reversed(recent_messages))

        print(
            "[Summary] trigger detected: "
            f"agent={agent_id}, total_messages={total_messages}, "
            f"threshold={self.summary_trigger_messages}, keep_recent={self.summary_keep_recent}"
        )

        cutoff_id = recent_messages[0]["id"] - 1
        summary_record = self.memory_manager.get_summary_record(agent_id)
        new_messages = self.memory_manager.get_messages_for_summary(
            agent_id=agent_id,
            after_id=int(summary_record["last_message_id"]),
            before_id=cutoff_id,
            memory_scope=self.DIRECT_MEMORY_SCOPE,
        )
        if not new_messages:
            print(
                "[Summary] skipped: "
                f"agent={agent_id}, reason=no_new_messages, "
                f"last_message_id={summary_record['last_message_id']}, cutoff_id={cutoff_id}"
            )
            return str(summary_record["summary"])

        try:
            merged_summary = self._distill_summary(str(summary_record["summary"]), new_messages)
        except Exception:
            merged_summary = self._fallback_summary(str(summary_record["summary"]), new_messages)

        self.memory_manager.save_summary(agent_id, merged_summary, cutoff_id)
        print(
            "[Summary] updated: "
            f"agent={agent_id}, summarized_messages={len(new_messages)}, "
            f"new_last_message_id={cutoff_id}"
        )
        return merged_summary

    def _extract_query_terms(self, rewritten_query: str, user_query: str) -> list[str]:
        """提取用于重排的关键词集合。"""
        terms = set()
        for source in (rewritten_query, user_query):
            for token in re.findall(r"[A-Za-z_][A-Za-z0-9_./-]{1,}|[\u4e00-\u9fff]{2,}", source):
                cleaned = token.strip().lower()
                if len(cleaned) >= 2:
                    terms.add(cleaned)
        return sorted(terms)

    def _rewrite_knowledge_query(self, user_query: str) -> str:
        """提纯知识库检索词。"""
        rewrite_messages = [
            {
                "role": "system",
                "content": (
                    "你是搜索词提取器。请从用户提问中提取最核心的专有名词、模块名、接口名或业务关键词。"
                    "不要解释，只输出关键词本身，多个词用空格分隔。"
                ),
            },
            {"role": "user", "content": user_query},
        ]
        rewritten = self._collect_model_response(
            rewrite_messages,
            max_tokens=self.knowledge_rewrite_max_tokens,
            temperature=0.1,
        )
        cleaned = rewritten.strip().strip("\"'")
        return cleaned or user_query

    def _score_rag_candidate(
        self,
        content: str,
        relevance_score: float,
        query_terms: list[str],
    ) -> float:
        """结合向量分数、关键词覆盖与正文密度做二次排序。"""
        normalized = content.lower()
        lexical_hits = sum(1 for term in query_terms if term in normalized)
        length_bonus = min(len(content), 600) / 600 * 0.08
        coverage_bonus = min(lexical_hits, 4) * 0.06
        title_like = (
            len(content) < 90
            or (content.lstrip().startswith("#") and len(content) < 160)
            or (content.count("\n") <= 1 and len(content.split()) <= 12)
        )
        title_penalty = 0.18 if title_like else 0.0
        return relevance_score + coverage_bonus + length_bonus - title_penalty

    def _build_rag_context(self, user_query: str) -> str:
        """构建重写、扩召回与重排后的知识库上下文。"""
        if not self.db_manager:
            return ""

        rewritten_query = self._rewrite_knowledge_query(user_query)
        query_terms = self._extract_query_terms(rewritten_query, user_query)
        candidate_k = max(self.rag_top_k * 2, 8)

        try:
            docs_with_scores = self.db_manager.search_with_scores(rewritten_query, k=candidate_k)
        except Exception:
            docs_with_scores = [(doc, 0.0) for doc in self.db_manager.search(rewritten_query, k=candidate_k)]

        ranked_candidates = []
        for doc, score in docs_with_scores:
            normalized = " ".join(doc.page_content.split())
            if not normalized:
                continue
            rerank_score = self._score_rag_candidate(normalized, float(score), query_terms)
            ranked_candidates.append((rerank_score, doc))

        ranked_candidates.sort(key=lambda item: item[0], reverse=True)

        seen = set()
        segments = []
        total_chars = 0
        for _, doc in ranked_candidates:
            normalized = " ".join(doc.page_content.split())
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

            source = self._format_source_label(doc.metadata)
            segments.append(f"[来源: {source}]\n{snippet}")
            total_chars += len(snippet)
            if len(segments) >= self.rag_top_k or total_chars >= self.rag_context_max_chars:
                break

        if not segments:
            return ""
        return "\n\n".join(segments)

    @staticmethod
    def _format_source_label(metadata: dict) -> str:
        """格式化来源标签，优先补充页码与章节信息。"""
        source = str(metadata.get("source", "未知来源"))
        page_start = metadata.get("page_start")
        page_end = metadata.get("page_end")
        section_parts = [metadata.get("section_h1"), metadata.get("section_h2"), metadata.get("section_h3")]
        section = "/".join(str(part) for part in section_parts if part)

        suffix_parts = []
        if page_start is not None:
            if page_end is not None and page_end != page_start:
                suffix_parts.append(f"p.{page_start}-{page_end}")
            else:
                suffix_parts.append(f"p.{page_start}")
        if section:
            suffix_parts.append(f"section: {section}")
        if not suffix_parts:
            return source
        return f"{source} ({', '.join(suffix_parts)})"

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
        history_scope: str = DIRECT_MEMORY_SCOPE,
    ) -> list[dict[str, str]]:
        """构建一次推理所需的完整消息序列。"""
        summary_text = ""
        if history_scope == self.DIRECT_MEMORY_SCOPE:
            summary_text = self._update_summary_if_needed(agent_id)

        history = self.memory_manager.get_chat_history(
            agent_id=agent_id,
            limit=self.history_window_size,
            ascending=True,
            memory_scope=history_scope,
        )
        history = self._dedupe_current_user_message(history, user_query)

        system_prompt = self._build_system_prompt(agent_id, allow_delegation=allow_delegation)
        if summary_text:
            system_prompt = f"{system_prompt}\n\nConversation summary:\n{summary_text}"

        messages = [
            {
                "role": "system",
                "content": system_prompt,
            }
        ]
        messages.extend({"role": row["role"], "content": row["content"]} for row in history)

        if agent_id == "knowledge_expert" and self.db_manager:
            context = self._build_rag_context(user_query)
            if context:
                user_query = (
                    f"【系统提供的参考资料】\n{context}\n\n"
                    f"【用户实际提问】\n{user_query}"
                )
            else:
                user_query = (
                    "【系统提示】未检索到本地知识库信源。请先说明“未找到对应信源”，"
                    "再基于通用知识回答。\n\n"
                    f"【用户实际提问】\n{user_query}"
                )

        messages.append({"role": "user", "content": user_query})
        return messages

    def _parse_tool_call(self, response_text: str) -> Optional[tuple[str, str]]:
        """解析工具规划结果。"""
        normalized = response_text.strip()
        if not normalized or normalized.upper() == "NO_TOOL":
            return None

        for pattern in (
            re.compile(r"^CALL:\s*(\w+)\((.*)\)\s*$", re.IGNORECASE | re.DOTALL),
            re.compile(r"^Action:\s*(\w+)\((.*)\)\s*$", re.IGNORECASE | re.DOTALL),
        ):
            match = pattern.match(normalized)
            if not match:
                continue
            tool_name = match.group(1)
            tool_args = match.group(2).strip()
            if tool_name in self.tools:
                return tool_name, tool_args
        return None

    def _tool_intent_likely(self, user_query: str) -> bool:
        """通过轻量规则判断当前问题是否像是工具型请求。"""
        lowered = user_query.lower()
        keywords = [
            ".csv",
            ".xlsx",
            ".xls",
            "excel",
            "csv",
            "file",
            "folder",
            "directory",
            "path",
            "list files",
            "system status",
            "cpu",
            "memory",
            "文件",
            "目录",
            "路径",
            "系统状态",
            "内存",
            "cpu",
        ]
        return any(keyword in lowered for keyword in keywords)

    def _plan_tool_call(
        self,
        messages: list[dict[str, str]],
        agent_id: str,
    ) -> Optional[tuple[str, str]]:
        """决定当前回答前是否需要调用工具。"""
        if not self.tools:
            return None
        if not self._tool_intent_likely(messages[-1]["content"]):
            return None

        planner_messages = list(messages)
        planner_messages[0] = {
            "role": "system",
            "content": self._build_tool_planner_prompt(agent_id),
        }
        planner_response = self._collect_model_response(
            planner_messages,
            max_tokens=self.tool_plan_max_tokens,
            temperature=0.1,
        )
        return self._parse_tool_call(planner_response)

    def _prepare_answer_messages(
        self,
        agent_id: str,
        user_query: str,
        *,
        history_scope: str = DIRECT_MEMORY_SCOPE,
    ) -> list[dict[str, str]]:
        """构建回答消息，并在需要时注入工具观察结果。"""
        messages = self._build_messages(
            user_query=user_query,
            agent_id=agent_id,
            allow_delegation=False,
            history_scope=history_scope,
        )
        tool_call = self._plan_tool_call(messages, agent_id)
        if not tool_call:
            return messages

        tool_name, tool_args = tool_call
        observation = str(self.tools[tool_name]["func"](tool_args))
        observation = self._truncate_text(observation, 1600)
        messages[0]["content"] += (
            "\n\n"
            f"Tool used: {tool_name}\n"
            f"Tool observation:\n{observation}\n\n"
            "Use the observation to answer the user directly. "
            "Do not expose tool protocol."
        )
        return messages

    def _stream_final_response(
        self,
        agent_id: str,
        user_query: str,
        *,
        history_scope: str = DIRECT_MEMORY_SCOPE,
    ) -> Generator[str, None, str]:
        """流式生成最终可见回答。"""
        messages = self._prepare_answer_messages(
            agent_id=agent_id,
            user_query=user_query,
            history_scope=history_scope,
        )
        final_response = ""
        for chunk in self.llm.generate(messages, max_tokens=self.max_tokens):
            final_response += chunk
            yield chunk
        return final_response

    def _complete_final_response(
        self,
        agent_id: str,
        user_query: str,
        *,
        history_scope: str = DIRECT_MEMORY_SCOPE,
    ) -> str:
        """同步生成最终回答文本。"""
        messages = self._prepare_answer_messages(
            agent_id=agent_id,
            user_query=user_query,
            history_scope=history_scope,
        )
        return self._collect_model_response(messages)

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
            memory_scope=self.DIRECT_MEMORY_SCOPE,
        )
        history = self._dedupe_current_user_message(history, user_query)
        system_prompt = self._build_system_prompt("core_router", allow_delegation=True)
        if summary_text:
            system_prompt = f"{system_prompt}\n\nConversation summary:\n{summary_text}"
        messages = [
            {
                "role": "system",
                "content": system_prompt,
            }
        ]
        messages.extend({"role": row["role"], "content": row["content"]} for row in history)
        messages.append({"role": "user", "content": user_query})
        return messages

    def _should_orchestrate(self, agent_id: str) -> bool:
        """判断当前轮是否允许触发多智能体编排。"""
        return self.orchestration_enabled and agent_id == "core_router"

    def _run_agent_once(
        self,
        agent_id: str,
        user_query: str,
        *,
        persist: bool = True,
        persist_scope: str = DIRECT_MEMORY_SCOPE,
        history_scope: str = DIRECT_MEMORY_SCOPE,
    ) -> str:
        """执行一次非流式智能体调用。"""
        if persist:
            self.memory_manager.add_message(
                agent_id,
                "user",
                user_query,
                memory_scope=persist_scope,
            )
        final_response = self._complete_final_response(
            agent_id=agent_id,
            user_query=user_query,
            history_scope=history_scope,
        )
        if persist:
            self.memory_manager.add_message(
                agent_id,
                "assistant",
                final_response,
                memory_scope=persist_scope,
            )
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
        sections.append(
            "Please synthesize the specialist outputs into one final answer for the user. "
            "Do not emit Delegate lines. Do not ask the specialists again. "
            "Keep the answer coherent and directly useful."
        )
        return "\n".join(sections)

    def _stream_single_agent(
        self,
        user_query: str,
        agent_id: str,
    ) -> Generator[str, None, None]:
        """执行单智能体流式回复并持久化结果。"""
        final_response = yield from self._stream_final_response(
            agent_id=agent_id,
            user_query=user_query,
            history_scope=self.DIRECT_MEMORY_SCOPE,
        )
        self.memory_manager.add_message(
            agent_id,
            "assistant",
            final_response,
            memory_scope=self.DIRECT_MEMORY_SCOPE,
        )

    def _stream_core_with_orchestration(
        self,
        user_query: str,
    ) -> Generator[str, None, None]:
        """先执行编排，再流式输出核心 Agent 的最终汇总结论。"""
        yield self._build_orchestration_event("planning_started")
        orchestration_result = self._plan_orchestration(user_query)
        delegates = orchestration_result["delegates"]

        if not delegates:
            yield self._build_orchestration_event("planning_skipped")
            final_response = yield from self._stream_final_response(
                agent_id="core_router",
                user_query=user_query,
                history_scope=self.DIRECT_MEMORY_SCOPE,
            )
            self.memory_manager.add_message(
                "core_router",
                "assistant",
                final_response,
                memory_scope=self.DIRECT_MEMORY_SCOPE,
            )
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
            result = self._run_agent_once(
                agent_id=agent_id,
                user_query=task,
                persist=True,
                persist_scope=self.ORCHESTRATION_MEMORY_SCOPE,
                history_scope=self.DIRECT_MEMORY_SCOPE,
            )
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
        final_response = yield from self._stream_final_response(
            agent_id="core_router",
            user_query=synthesis_query,
            history_scope=self.DIRECT_MEMORY_SCOPE,
        )
        self.memory_manager.add_message(
            "core_router",
            "assistant",
            final_response,
            metadata={
                "orchestration": [
                    {
                        "agent_id": item["agent_id"],
                        "task": item["task"],
                        "result_preview": self._truncate_text(item["result"], 180),
                    }
                    for item in specialist_outputs
                ]
            },
            memory_scope=self.DIRECT_MEMORY_SCOPE,
        )

    def chat_stream(self, user_query: str, agent_id: str = "core_router") -> Generator[str, None, None]:
        """执行一次对话，并持久化用户与助手消息。"""
        self.memory_manager.add_message(
            agent_id,
            "user",
            user_query,
            memory_scope=self.DIRECT_MEMORY_SCOPE,
        )
        if self._should_orchestrate(agent_id):
            yield from self._stream_core_with_orchestration(user_query=user_query)
            return
        yield from self._stream_single_agent(user_query=user_query, agent_id=agent_id)

    def get_agent_meta(self, agent_id: str) -> tuple[str, str]:
        """返回智能体的显示名称与头像文件名。"""
        config = self.agents_config.get(agent_id, self.agents_config["core_router"])
        return str(config["name"]), str(config["avatar"])
