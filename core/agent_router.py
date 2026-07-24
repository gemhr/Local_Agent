#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""智能体路由与多智能体编排模块。"""

import json
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Dict, Generator, Optional

from core.memory_manager import MemoryManager
from core.runtime import (
    ContextBuildRequest, ContextBuilder, ContextItem, ContextSourceType, ContextTrustLevel,
    DeterministicTokenEstimator, ModelContextRequirements, ModelCostProfile, ModelPreference, ModelProfile,
    ModelProfileId, ModelResolver, ModelSelectionPolicy, ModelSelectionRequest,
    Plan, RiskLevel, RunContext, TaskCapabilityRequirements, create_single_step_plan,
    BudgetUsage, UsageSource, BudgetedModelStream,
    GeneratorModelAdapter, ModelAdapterResolver, ModelCircuitBreakerRegistry,
    ModelInvocationResult, ModelInvocationRouter, ModelRoutingPolicy,
    StepEventEmitter,
)

if TYPE_CHECKING:
    from core.knowledge_base.vector_db_manager import VectorDBManager
    from core.llm_engine import LocalLLMEngine


class KnowledgeBaseUnavailableError(RuntimeError):
    """知识库问答请求没有可用检索后端时引发。"""


class KnowledgeSourceNotFoundError(LookupError):
    """检索未找到足够相关的本地信源时引发。"""


class AgentRouter:
    """协调提示词、工具调用、记忆、知识检索与多智能体协作。"""

    ORCHESTRATION_EVENT_PREFIX = "[[ORCH]]"
    DIRECT_MEMORY_SCOPE = "direct"
    ORCHESTRATION_MEMORY_SCOPE = "orchestration"

    def __init__(
        self,
        llm_engine: "LocalLLMEngine",
        memory_manager: MemoryManager,
        db_manager: Optional["VectorDBManager"] = None,
        *,
        history_window_size: int = 8,
        summary_trigger_messages: int = 16,
        summary_keep_recent: int = 8,
        summary_max_chars: int = 1000,
        rag_top_k: int = 3,
        rag_min_score: float = 0.55,
        rag_doc_max_chars: int = 700,
        rag_context_max_chars: int = 1500,
        max_tokens: int = 640,
        model_context_window: int = 4096,
        orchestration_enabled: bool = True,
        orchestration_max_agents: int = 3,
        knowledge_base_error: str | None = None,
        model_profiles: tuple[ModelProfile, ...] | None = None,
        model_resolver: ModelResolver | None = None,
        model_adapter_resolver: ModelAdapterResolver | None = None,
        model_invocation_router: ModelInvocationRouter | None = None,
        circuit_breaker_registry: ModelCircuitBreakerRegistry | None = None,
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
        self.rag_min_score = min(1.0, max(0.0, float(rag_min_score)))
        self.rag_doc_max_chars = rag_doc_max_chars
        self.rag_context_max_chars = rag_context_max_chars
        self.max_tokens = max_tokens
        self.model_context_window = model_context_window
        self.context_builder = ContextBuilder(DeterministicTokenEstimator())
        self.orchestration_enabled = orchestration_enabled
        self.orchestration_max_agents = orchestration_max_agents
        self.knowledge_base_error = knowledge_base_error
        default_cost = ModelCostProfile(
            ModelProfileId.LOCAL_FAST,
            False,
            fixed_call_cost_units=1,
            estimated_latency_ms=1,
        )
        default_profile = ModelProfile(
            ModelProfileId.LOCAL_FAST,
            model_context_window,
            max_tokens,
            False,
            False,
            False,
            False,
            1,
            1,
            default_cost,
            False,
            "local_default",
        )
        self.model_profiles = model_profiles or (default_profile,)
        self.model_selection_policy = ModelSelectionPolicy()
        self.model_routing_policy = ModelRoutingPolicy()
        # hybrid 使用可用 Profile 的最大安全窗口构建一次上下文，避免先按本地窗口裁剪。
        self.model_context_window = max(self.model_selection_policy.maximum_safe_context_window(profile.context_window) for profile in self.model_profiles)
        self.model_resolver = model_resolver or ModelResolver({self.model_profiles[0].profile_id: llm_engine})
        self.model_adapter_resolver = model_adapter_resolver or ModelAdapterResolver(
            {
                profile.profile_id: GeneratorModelAdapter(
                    self.model_resolver.resolve(profile.profile_id)
                )
                for profile in self.model_profiles
            }
        )
        self.model_invocation_router = model_invocation_router or ModelInvocationRouter(
            self.model_routing_policy
        )
        # AgentRouter 为应用生命周期对象，因此默认 Registry 跨 Run 共享。
        self.circuit_breaker_registry = (
            circuit_breaker_registry or ModelCircuitBreakerRegistry()
        )
        self.tool_plan_max_tokens = 48
        self.summary_plan_max_tokens = 256
        self.knowledge_rewrite_max_tokens = 128
        self.tools: Dict[str, Dict[str, object]] = {}
        self.agents_config = {
            "core_router": {
                "name": "Core Router",
                "role": "处理通用问题，并协调辅助智能体。",
                "avatar": "avatar_router.png",
            },
            "data_analyst": {
                "name": "Data Analyst",
                "role": "分析 CSV 和 Excel 文件，并总结洞见。",
                "avatar": "avatar_excel.png",
            },
            "code_expert": {
                "name": "Code Expert",
                "role": "审查代码、排查问题并改进架构。",
                "avatar": "avatar_code.png",
            },
            "knowledge_expert": {
                "name": "Knowledge Expert",
                "role": "在可用时依据本地知识库回答问题。",
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
            f"你是 {config['name']}。",
            f"你的职责：{config['role']}",
            "除非用户另有要求，否则请用中文清晰、简洁地回答。",
        ]
        if agent_id == "knowledge_expert":
            lines.extend(
                [
                    "你必须优先依据本地知识库信源回答。",
                    "如果用户消息包含【系统提供的参考资料】，请优先使用资料内容并在答案末尾增加“参考来源：”列表。",
                    "参考来源格式固定为“[序号] 文件路径或来源名”。",
                    "如果没有可用本地信源，只能明确说明“未找到对应信源”，不得使用通用知识补写事实。",
                ]
            )
        if allow_delegation and agent_id == "core_router":
            lines.extend(
                [
                    "你可以在需要时编排专业智能体。",
                    "可用的专业智能体：",
                    "- data_analyst：分析 CSV、Excel、表格数据、指标和趋势。",
                    "- code_expert：审查代码、排查问题、解释实现并改进架构。",
                    "- knowledge_expert：依据本地知识库回答问题。",
                    (
                        "如需专业智能体，只能输出一行或多行，且每行必须严格使用以下格式："
                        "Delegate: agent_id | task"
                    ),
                    "其中 `Delegate:`、agent_id、竖线和 task 的格式必须保持不变。",
                    "不得委派给 core_router。",
                    "输出 Delegate: 行时不得附加任何说明文字。",
                    "如不需要专业智能体，请直接回答用户。",
                ]
            )
        return "\n".join(lines)

    def _build_tool_planner_prompt(self, agent_id: str) -> str:
        """构造工具规划提示词。"""
        config = self.agents_config.get(agent_id, self.agents_config["core_router"])
        lines = [
            f"你正在判断 {config['name']} 在回答前是否需要使用本地工具。",
            "只能输出一行。",
            "无需工具时仅输出 `NO_TOOL`。",
            "需要一个工具时，仅输出 `CALL: tool_name(argument_text)`。",
            "其中 `CALL:`、工具名称和括号格式必须保持不变。",
            "不要直接回答用户。",
        ]
        if self.tools:
            lines.append("可用工具：")
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
        enable_thinking: bool | None = None,
    ) -> str:
        """收集一次完整的模型输出。"""
        response_text = ""
        for chunk in self.llm.generate(
            messages,
            max_tokens=max_tokens or self.max_tokens,
            temperature=temperature,
            enable_thinking=enable_thinking,
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
            enable_thinking=False,
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
            enable_thinking=False,
        )
        cleaned = rewritten.strip().strip("\"'")
        return cleaned or user_query

    def _score_rag_candidate(
        self,
        content: str,
        relevance_score: float,
        query_terms: list[str],
        metadata: dict | None = None,
    ) -> float:
        """结合向量分数、正文命中和来源元数据做二次排序。"""
        normalized = content.lower()
        metadata = metadata or {}
        metadata_text = " ".join(
            str(metadata.get(key, ""))
            for key in (
                "source",
                "file_name",
                "document_title",
                "section_path",
                "section_h1",
                "section_h2",
                "section_h3",
            )
        ).lower()
        lexical_hits = sum(1 for term in query_terms if term in normalized)
        metadata_hits = sum(1 for term in query_terms if term in metadata_text)
        term_frequency = sum(min(normalized.count(term), 3) for term in query_terms)
        length_bonus = min(len(content), 600) / 600 * 0.08
        coverage_bonus = min(lexical_hits, 4) * 0.06
        frequency_bonus = min(term_frequency, 5) * 0.015
        metadata_bonus = min(metadata_hits, 3) * 0.18
        title_like = (
            len(content) < 90
            or (content.lstrip().startswith("#") and len(content) < 160)
            or (content.count("\n") <= 1 and len(content.split()) <= 12)
        )
        title_penalty = 0.12 if title_like and not metadata_hits else 0.0
        return min(
            1.0,
            relevance_score
            + coverage_bonus
            + frequency_bonus
            + metadata_bonus
            + length_bonus
            - title_penalty,
        )

    @staticmethod
    def _rag_candidate_key(doc: object, normalized_content: str) -> str:
        """优先使用稳定元数据标识对多路召回结果去重。"""
        metadata = getattr(doc, "metadata", {}) or {}
        for key in ("chunk_id", "id", "document_id"):
            value = metadata.get(key)
            if value:
                return f"{key}:{value}"
        source = metadata.get("source") or metadata.get("file_name") or ""
        return f"content:{source}:{normalized_content[:300]}"

    def _build_rag_context(self, user_query: str) -> str:
        """构建重写、扩召回与重排后的知识库上下文。"""
        if not self.db_manager:
            return ""

        try:
            rewritten_query = self._rewrite_knowledge_query(user_query)
        except Exception:
            rewritten_query = user_query
        query_terms = self._extract_query_terms(rewritten_query, user_query)
        candidate_k = max(self.rag_top_k * 2, 8)
        candidates: dict[str, tuple[object, float]] = {}

        for query in dict.fromkeys((rewritten_query, user_query)):
            try:
                docs_with_scores = self.db_manager.search_with_scores(
                    query,
                    k=candidate_k,
                )
            except Exception:
                try:
                    docs_with_scores = [
                        (doc, 0.0)
                        for doc in self.db_manager.search(query, k=candidate_k)
                    ]
                except Exception:
                    docs_with_scores = []
            for doc, score in docs_with_scores:
                normalized = " ".join(doc.page_content.split())
                if not normalized:
                    continue
                candidate_key = self._rag_candidate_key(doc, normalized)
                previous = candidates.get(candidate_key)
                normalized_score = min(1.0, max(0.0, float(score)))
                if previous is None or normalized_score > previous[1]:
                    candidates[candidate_key] = (doc, normalized_score)

        keyword_search = getattr(self.db_manager, "keyword_search", None)
        if callable(keyword_search) and query_terms:
            try:
                keyword_docs = keyword_search(query_terms, k=candidate_k)
            except Exception:
                keyword_docs = []
            for doc in keyword_docs:
                normalized = " ".join(doc.page_content.split())
                if not normalized:
                    continue
                candidate_key = self._rag_candidate_key(doc, normalized)
                previous = candidates.get(candidate_key)
                keyword_score = 0.55
                if previous is None or keyword_score > previous[1]:
                    candidates[candidate_key] = (doc, keyword_score)

        ranked_candidates = []
        for doc, score in candidates.values():
            normalized = " ".join(doc.page_content.split())
            rerank_score = self._score_rag_candidate(
                normalized,
                score,
                query_terms,
                doc.metadata,
            )
            ranked_candidates.append((rerank_score, doc))

        ranked_candidates.sort(key=lambda item: item[0], reverse=True)
        if not ranked_candidates:
            return ""
        relevance_floor = max(
            self.rag_min_score,
            ranked_candidates[0][0] - 0.20,
        )

        seen = set()
        segments = []
        total_chars = 0
        for score, doc in ranked_candidates:
            if score < relevance_floor:
                continue
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

    def _estimate_messages_tokens(self, messages: list[dict[str, str]]) -> int:
        """估算 Builder 之外既有消息正文的近似 Token 数。"""
        return sum(
            self.context_builder.estimator.estimate(message["content"])
            for message in messages
        )

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
        context_requirements_out: list[ModelContextRequirements] | None = None,
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
        if summary_text and agent_id != "knowledge_expert":
            system_prompt = f"{system_prompt}\n\n对话摘要：\n{summary_text}"

        messages = [
            {
                "role": "system",
                "content": system_prompt,
            }
        ]
        messages.extend({"role": row["role"], "content": row["content"]} for row in history)

        if agent_id == "knowledge_expert":
            if self.db_manager is None:
                raise KnowledgeBaseUnavailableError(
                    "本地知识库当前不可用，请检查服务启动日志中的 [KB Runtime] 初始化错误。"
                )
            context = self._build_rag_context(user_query)
            if context:
                preexisting_messages_tokens = self._estimate_messages_tokens(messages)
                preexisting_mandatory_tokens = self.context_builder.estimator.estimate(system_prompt)
                context_items = [
                    ContextItem("knowledge-user-request", ContextSourceType.CURRENT_USER_REQUEST,
                        ContextTrustLevel.USER_CONTENT, user_query, 1000, datetime.now(timezone.utc)),
                    ContextItem("knowledge-rag-documents", ContextSourceType.RAG_DOCUMENT,
                        ContextTrustLevel.UNTRUSTED_EXTERNAL, context, 500, datetime.now(timezone.utc),
                        source_ref="knowledge_base", citation_id="local-kb"),
                ]
                if summary_text:
                    context_items.append(
                        ContextItem("knowledge-memory-summary", ContextSourceType.MEMORY_SUMMARY,
                            ContextTrustLevel.USER_CONTENT, summary_text, 700, datetime.now(timezone.utc),
                            source_ref="memory_summary")
                    )
                context_result = self.context_builder.build(
                    ContextBuildRequest(
                        run_id="legacy-router", agent_id=agent_id,
                        items=context_items,
                        max_input_tokens=self.model_context_window,
                        reserved_output_tokens=self.max_tokens,
                        preexisting_messages_tokens=preexisting_messages_tokens,
                        preexisting_mandatory_tokens=preexisting_mandatory_tokens,
                    )
                )
                user_query = context_result.rendered_text
                if context_requirements_out is not None:
                    context_requirements_out.append(context_result.model_requirements)
            else:
                raise KnowledgeSourceNotFoundError(
                    "未找到足够相关的本地知识库信源，知识专家已停止回答。"
                )

        messages.append({"role": "user", "content": user_query})
        return messages

    def _capability_requirements(self, agent_id: str, user_query: str) -> TaskCapabilityRequirements:
        """从既有确定性路由信息生成最小能力需求，不保存用户正文。"""
        return TaskCapabilityRequirements(
            requires_rag=agent_id == "knowledge_expert",
            requires_tools=self._tool_intent_likely(user_query),
            requires_multi_agent=False,
            requires_code_reasoning=agent_id == "code_expert",
            risk_level=RiskLevel.LOW,
            estimated_steps=1,
        )

    def build_single_agent_plan(self, agent_id: str, user_query: str) -> Plan:
        """为 Coordinated 单 Agent 路径创建一次确定性 Plan。"""
        return create_single_step_plan(
            agent_id, self._capability_requirements(agent_id, user_query)
        )

    def _select_model(
        self,
        agent_id: str,
        user_query: str,
        messages: list[dict[str, str]],
        context_requirements: ModelContextRequirements | None = None,
        run_context: RunContext | None = None,
        capability_requirements: TaskCapabilityRequirements | None = None,
    ) -> tuple[object, ModelProfile]:
        """使用完整消息的近似上下文特征选择并解析一次首选模型。"""
        decision, profile, _requirements = self._select_model_decision(
            agent_id,
            user_query,
            messages,
            context_requirements,
            run_context,
            capability_requirements,
        )
        return self.model_resolver.resolve(decision.selected_profile), profile

    def _select_model_decision(
        self,
        agent_id: str,
        user_query: str,
        messages: list[dict[str, str]],
        context_requirements: ModelContextRequirements | None = None,
        run_context: RunContext | None = None,
        capability_requirements: TaskCapabilityRequirements | None = None,
    ):
        """返回 Selection Decision、首次 Profile 与最终上下文需求。"""
        estimated = self._estimate_messages_tokens(messages)
        requirements = context_requirements or ModelContextRequirements(estimated, estimated + self.max_tokens, estimated >= 2048, False, False, len(messages), 0, 0, False, False)
        capabilities = capability_requirements
        if capabilities is None:
            # Legacy 路径仍保留临时 Plan；Coordinated 路径传入 Coordinator
            # 长期持有的 PlanStep 能力需求，不在模型选择期创建第二个 Plan。
            capabilities = self.build_single_agent_plan(
                agent_id, user_query
            ).steps[0].capability_requirements
        decision = self.model_selection_policy.select(ModelSelectionRequest(agent_id, capabilities, requirements, ModelPreference.AUTO, self.model_profiles, run_context.budget_ledger.snapshot() if run_context is not None and run_context.budget_ledger is not None else None))
        profile = next(profile for profile in self.model_profiles if profile.profile_id == decision.selected_profile)
        final_required = self.model_selection_policy.required_context_window(requirements.minimum_context_window)
        if profile.context_window < final_required:
            raise ValueError("所选模型无法满足最终消息的安全上下文窗口")
        return decision, profile, requirements

    def _reserve_model_call(self, run_context: RunContext | None, messages: list[dict[str, str]], max_tokens: int, profile: ModelProfile):
        """在真实模型请求前执行原子预留；未注入账本时保持旧路径兼容。"""
        if run_context is None or run_context.budget_ledger is None:
            return None
        input_tokens = self._estimate_messages_tokens(messages)
        metadata = profile.cost_profile
        budget = run_context.budget_ledger.budget
        if metadata is None and (budget.max_remote_model_calls is not None or budget.max_cost_units is not None):
            from core.runtime.model_selection import ModelSelectionError
            raise ModelSelectionError("MODEL_BUDGET_METADATA_MISSING", requested_profile=profile.profile_id)
        cost = 0 if metadata is None else metadata.fixed_call_cost_units + (input_tokens * metadata.input_cost_units_per_1k_tokens + 999) // 1000 + (max_tokens * metadata.output_cost_units_per_1k_tokens + 999) // 1000
        return run_context.budget_ledger.reserve(BudgetUsage(model_calls=1, remote_model_calls=int(metadata.is_remote) if metadata else 0, input_tokens=input_tokens, output_tokens=max_tokens, total_tokens=input_tokens + max_tokens, cost_units=cost), reservation_type="model_call")

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
            "complex workflow",
            "workflow simulator",
            "complex_workflow_simulator",
            "batch workflow",
            "\u590d\u6742\u6d41\u7a0b",
            "\u6a21\u62df\u5de5\u5177",
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
            enable_thinking=False,
        )
        return self._parse_tool_call(planner_response)

    def _prepare_answer_messages(
        self,
        agent_id: str,
        user_query: str,
        *,
        history_scope: str = DIRECT_MEMORY_SCOPE,
        run_context: RunContext | None = None,
        context_requirements_out: list[ModelContextRequirements] | None = None,
    ) -> list[dict[str, str]]:
        """构建回答消息，并在需要时注入工具观察结果。"""
        if run_context is not None:
            run_context.raise_if_inactive()
        messages = self._build_messages(
            user_query=user_query,
            agent_id=agent_id,
            allow_delegation=False,
            history_scope=history_scope,
            context_requirements_out=context_requirements_out,
        )
        tool_call = self._plan_tool_call(messages, agent_id)
        if not tool_call:
            return messages

        tool_name, tool_args = tool_call
        if run_context is not None:
            run_context.raise_if_inactive()
        tool_reservation = None
        if run_context is not None and run_context.budget_ledger is not None:
            tool_reservation = run_context.budget_ledger.reserve(BudgetUsage(tool_calls=1), reservation_type="tool_call")
        try:
            observation = str(self.tools[tool_name]["func"](tool_args))
        finally:
            # 已进入真实 Tool 边界后，即使失败也正式计一次调用。
            if tool_reservation is not None:
                run_context.budget_ledger.commit(tool_reservation, BudgetUsage(tool_calls=1), usage_source=UsageSource.ESTIMATED)
        if run_context is not None:
            run_context.raise_if_inactive()
        observation = self._truncate_text(observation, 1600)
        messages[0]["content"] += (
            "\n\n"
            f"已使用工具：{tool_name}\n"
            f"工具观察结果：\n{observation}\n\n"
            "请依据观察结果直接回答用户。"
            "不要向用户暴露工具调用协议。"
        )
        return messages

    def _stream_final_response(
        self,
        agent_id: str,
        user_query: str,
        *,
        history_scope: str = DIRECT_MEMORY_SCOPE,
        run_context: RunContext | None = None,
    ) -> Generator[str, None, str]:
        """流式生成最终可见回答。"""
        context_requirements_out: list[ModelContextRequirements] = []
        messages = self._prepare_answer_messages(
            agent_id=agent_id,
            user_query=user_query,
            history_scope=history_scope,
            run_context=run_context,
            context_requirements_out=context_requirements_out,
        )
        if run_context is not None:
            run_context.raise_if_inactive()
        final_response = ""
        selected_model, selected_profile = self._select_model(agent_id, user_query, messages, context_requirements_out[0] if context_requirements_out else None, run_context)
        model_reservation = self._reserve_model_call(run_context, messages, self.max_tokens, selected_profile)
        stream = selected_model.generate(messages, max_tokens=self.max_tokens)
        if model_reservation is not None:
            stream = BudgetedModelStream(stream, run_context.budget_ledger, model_reservation)
        try:
            for chunk in stream:
                if run_context is not None:
                    run_context.raise_if_inactive()
                final_response += chunk
                yield chunk
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                close()
        return final_response

    def _complete_final_response(
        self,
        agent_id: str,
        user_query: str,
        *,
        history_scope: str = DIRECT_MEMORY_SCOPE,
        run_context: RunContext | None = None,
        capability_requirements: TaskCapabilityRequirements | None = None,
        unified_invocation: bool = False,
        invocation_result_out: list[ModelInvocationResult] | None = None,
        event_emitter: StepEventEmitter | None = None,
    ) -> str:
        """同步生成最终回答文本。"""
        context_requirements_out: list[ModelContextRequirements] = []
        messages = self._prepare_answer_messages(
            agent_id=agent_id,
            user_query=user_query,
            history_scope=history_scope,
            run_context=run_context,
            context_requirements_out=context_requirements_out,
        )
        if run_context is not None:
            run_context.raise_if_inactive()
        selection_args = (
            agent_id,
            user_query,
            messages,
            context_requirements_out[0] if context_requirements_out else None,
            run_context,
        )
        if unified_invocation:
            if (
                run_context is None
                or run_context.budget_ledger is None
                or capability_requirements is None
            ):
                raise RuntimeError("统一 Invocation 路径需要 RunContext、BudgetLedger 和能力需求")
            decision, _selected_profile, requirements = self._select_model_decision(
                *selection_args,
                capability_requirements=capability_requirements,
            )
            breaker_snapshots = self.circuit_breaker_registry.snapshots(
                tuple(profile.effective_breaker_key for profile in self.model_profiles)
            )
            routing_decision = self.model_routing_policy.route(
                selection_decision=decision,
                capability_requirements=capability_requirements,
                context_requirements=requirements,
                profiles=self.model_profiles,
                preference=ModelPreference.AUTO,
                budget_snapshot=run_context.budget_ledger.snapshot(),
                breaker_snapshots=breaker_snapshots,
            )
            invocation_result = self.model_invocation_router.invoke(
                run_context=run_context,
                budget_ledger=run_context.budget_ledger,
                routing_decision=routing_decision,
                messages=messages,
                adapter_resolver=self.model_adapter_resolver,
                circuit_breaker_registry=self.circuit_breaker_registry,
                token_estimate=self._estimate_messages_tokens(messages),
                max_tokens=self.max_tokens,
                output_started=False,
                event_emitter=event_emitter,
            )
            if invocation_result_out is not None:
                invocation_result_out.append(invocation_result)
            return invocation_result.output
        if capability_requirements is None:
            selected_model, selected_profile = self._select_model(*selection_args)
        else:
            selected_model, selected_profile = self._select_model(
                *selection_args, capability_requirements=capability_requirements
            )
        model_reservation = self._reserve_model_call(
            run_context,
            messages,
            self.max_tokens,
            selected_profile,
        )
        stream = selected_model.generate(messages, max_tokens=self.max_tokens)
        if model_reservation is not None:
            stream = BudgetedModelStream(stream, run_context.budget_ledger, model_reservation)
        response = ""
        try:
            for chunk in stream:
                if run_context is not None:
                    run_context.raise_if_inactive()
                response += chunk
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                close()
        return response

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

    @staticmethod
    def _resolve_explicit_knowledge_delegate(user_query: str) -> list[dict[str, str]]:
        """无需依赖规划器输出格式，直接解析显式知识库请求。"""
        patterns = (
            r"(?:(?:请\s*)?(?:让|调用|使用|交给|委派)|请)(?:一下|下)?\s*(?:本地)?\s*(?:知识专家|knowledge[_ ]expert)",
            r"(?:根据|查询|检索|查找|搜索)(?:一下|下)?(?:本地)?知识库",
            r"^(?:知识专家|knowledge[_ ]expert)\s*[,，:：]",
        )
        for pattern in patterns:
            if not re.search(pattern, user_query, flags=re.IGNORECASE):
                continue
            task = re.sub(pattern, "", user_query, count=1, flags=re.IGNORECASE)
            task = task.lstrip(" ，,。.:：;；") or user_query
            return [{"agent_id": "knowledge_expert", "task": task}]
        return []

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
            system_prompt = f"{system_prompt}\n\n对话摘要：\n{summary_text}"
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
        run_context: RunContext | None = None,
        capability_requirements: TaskCapabilityRequirements | None = None,
        unified_invocation: bool = False,
        invocation_result_out: list[ModelInvocationResult] | None = None,
        event_emitter: StepEventEmitter | None = None,
    ) -> str:
        """执行一次非流式智能体调用。"""
        if run_context is not None:
            run_context.raise_if_inactive()
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
            run_context=run_context,
            capability_requirements=capability_requirements,
            unified_invocation=unified_invocation,
            invocation_result_out=invocation_result_out,
            event_emitter=event_emitter,
        )
        if run_context is not None:
            run_context.raise_if_inactive()
        if persist:
            self.memory_manager.add_message(
                agent_id,
                "assistant",
                final_response,
                memory_scope=persist_scope,
            )
        return final_response

    def complete_single_agent(
        self,
        agent_id: str,
        user_query: str,
        *,
        run_context: RunContext,
        capability_requirements: TaskCapabilityRequirements,
        persist: bool = True,
        invocation_result_out: list[ModelInvocationResult] | None = None,
        event_emitter: StepEventEmitter | None = None,
    ) -> str:
        """供 RunCoordinator Driver 使用的真实单 Agent 非流式业务入口。"""
        return self._run_agent_once(
            agent_id=agent_id,
            user_query=user_query,
            persist=persist,
            run_context=run_context,
            capability_requirements=capability_requirements,
            unified_invocation=True,
            invocation_result_out=invocation_result_out,
            event_emitter=event_emitter,
        )

    def _build_orchestration_event(self, event_type: str, **payload: object) -> str:
        """构建前后端约定的编排状态事件。"""
        event = {"type": event_type, **payload}
        return f"{self.ORCHESTRATION_EVENT_PREFIX}{json.dumps(event, ensure_ascii=False)}\n"

    def _plan_orchestration(
        self,
        user_query: str,
        run_context: RunContext | None = None,
    ) -> dict[str, object]:
        """执行核心 Agent 的委派规划阶段。"""
        if run_context is not None:
            run_context.raise_if_inactive()
        explicit_delegates = self._resolve_explicit_knowledge_delegate(user_query)
        if explicit_delegates:
            return {
                "planning_messages": [],
                "planning_response": "deterministic knowledge routing",
                "delegates": explicit_delegates,
            }
        planning_messages = self._build_orchestration_messages(user_query)
        if run_context is not None:
            run_context.raise_if_inactive()
        planning_response = self._collect_model_response(
            planning_messages,
            enable_thinking=False,
        )
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
            "用户问题：",
            user_query,
            "",
            "专业智能体输出：",
        ]
        for item in specialist_outputs:
            sections.extend(
                [
                    f"[{item['agent_name']}]",
                    f"任务：{item['task']}",
                    item["result"],
                    "",
                ]
            )
        sections.append(
            "请将专业智能体输出整合为面向用户的一份最终回答。"
            "不要输出 Delegate: 行，也不要再次询问专业智能体。"
            "回答应连贯且直接有用。"
            "所有关于本地知识的事实性陈述都必须由上方知识专家的输出支持。"
            "必须原样保留其中的来源引用和不确定性；不得编造、扩展，或用通用知识替代缺失的本地事实。"
            "如果知识专家报告知识库不可用或未找到相关来源，请明确说明这一限制。"
        )
        return "\n".join(sections)

    def _stream_single_agent(
        self,
        user_query: str,
        agent_id: str,
        run_context: RunContext | None = None,
    ) -> Generator[str, None, None]:
        """执行单智能体流式回复并持久化结果。"""
        final_response = yield from self._stream_final_response(
            agent_id=agent_id,
            user_query=user_query,
            history_scope=self.DIRECT_MEMORY_SCOPE,
            run_context=run_context,
        )
        if run_context is not None:
            run_context.raise_if_inactive()
        self.memory_manager.add_message(
            agent_id,
            "assistant",
            final_response,
            memory_scope=self.DIRECT_MEMORY_SCOPE,
        )

    def _stream_core_with_orchestration(
        self,
        user_query: str,
        run_context: RunContext | None = None,
    ) -> Generator[str, None, None]:
        """先执行编排，再流式输出核心 Agent 的最终汇总结论。"""
        if run_context is not None:
            run_context.raise_if_inactive()
        yield self._build_orchestration_event("planning_started")
        orchestration_result = self._plan_orchestration(user_query, run_context=run_context)
        delegates = orchestration_result["delegates"]

        if not delegates:
            yield self._build_orchestration_event("planning_skipped")
            final_response = yield from self._stream_final_response(
                agent_id="core_router",
                user_query=user_query,
                history_scope=self.DIRECT_MEMORY_SCOPE,
                run_context=run_context,
            )
            if run_context is not None:
                run_context.raise_if_inactive()
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
                run_context=run_context,
            )
            specialist_outputs.append(
                {
                    "agent_id": agent_id,
                    "agent_name": self.agents_config[agent_id]["name"],
                    "task": task,
                    "result": result,
                }
            )
            if run_context is not None:
                run_context.raise_if_inactive()
            yield self._build_orchestration_event(
                "delegate_finished",
                agent_id=agent_id,
                agent_name=self.agents_config[agent_id]["name"],
                summary=self._truncate_text(result, 120),
            )

        if (
            len(specialist_outputs) == 1
            and specialist_outputs[0]["agent_id"] == "knowledge_expert"
        ):
            final_response = specialist_outputs[0]["result"]
            if final_response:
                yield final_response
            if run_context is not None:
                run_context.raise_if_inactive()
            self.memory_manager.add_message(
                "core_router",
                "assistant",
                final_response,
                metadata={
                    "orchestration": [
                        {
                            "agent_id": "knowledge_expert",
                            "task": specialist_outputs[0]["task"],
                            "result_preview": self._truncate_text(final_response, 180),
                        }
                    ]
                },
                memory_scope=self.DIRECT_MEMORY_SCOPE,
            )
            return

        yield self._build_orchestration_event("synthesis_started")
        synthesis_query = self._build_synthesis_query(
            user_query=user_query,
            specialist_outputs=specialist_outputs,
        )
        final_response = yield from self._stream_final_response(
            agent_id="core_router",
            user_query=synthesis_query,
            history_scope=self.DIRECT_MEMORY_SCOPE,
            run_context=run_context,
        )
        if run_context is not None:
            run_context.raise_if_inactive()
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

    def chat_stream(
        self,
        user_query: str,
        agent_id: str = "core_router",
        run_context: RunContext | None = None,
    ) -> Generator[str, None, None]:
        """执行一次对话，并持久化用户与助手消息。"""
        if run_context is not None:
            run_context.raise_if_inactive()
        self.memory_manager.add_message(
            agent_id,
            "user",
            user_query,
            memory_scope=self.DIRECT_MEMORY_SCOPE,
        )
        if run_context is not None:
            run_context.raise_if_inactive()
        if self._should_orchestrate(agent_id):
            yield from self._stream_core_with_orchestration(user_query=user_query, run_context=run_context)
            return
        yield from self._stream_single_agent(user_query=user_query, agent_id=agent_id, run_context=run_context)

    def get_agent_meta(self, agent_id: str) -> tuple[str, str]:
        """返回智能体的显示名称与头像文件名。"""
        config = self.agents_config.get(agent_id, self.agents_config["core_router"])
        return str(config["name"]), str(config["avatar"])
