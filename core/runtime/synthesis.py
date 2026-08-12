#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WP3 Synthesis Agent adapter.

Input whitelist: the synthesis Binding instruction (or original user request)
plus the explicit ``depends_on`` results only. The whitelist limits sources,
but it cannot formally guarantee that the model never hallucinates; this is
stated truthfully in the WP3 result document.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from enum import Enum

from core.runtime.agent_adapter_factory import (
    AgentAdapterError,
    AgentAdapterErrorCode,
    AgentAdapterResult,
    AgentExecutionRequest,
)
from core.runtime.budget import BudgetExceededError
from core.runtime.cancellation import RunCancelledError
from core.runtime.context import RunContext, RunDeadlineExceededError
from core.runtime.model_context import (
    ContextItem,
    ContextSourceType,
    ContextTrustLevel,
)
from core.runtime.step_result import ResultContentType, ResultDisposition
from core.runtime.step_result_store import DependencyResultView


class SynthesisInputErrorCode(str, Enum):
    MISSING_DEPENDENCIES = "MISSING_DEPENDENCIES"
    INCOMPLETE_RESULT = "INCOMPLETE_RESULT"
    UNSUPPORTED_RESULT_TYPE = "UNSUPPORTED_RESULT_TYPE"
    SYNTHESIS_MODEL_FAILED = "SYNTHESIS_MODEL_FAILED"
    SYNTHESIS_RESULT_INVALID = "SYNTHESIS_RESULT_INVALID"


class SynthesisInputError(RuntimeError):
    """Safe error that never carries dependency content or prompt text."""

    def __init__(
        self,
        error_code: SynthesisInputErrorCode,
        safe_message: str,
    ) -> None:
        self.error_code = error_code
        self.safe_message = safe_message
        super().__init__(f"{safe_message} (error_code={error_code.value})")


_SYNTHESIS_SYSTEM_RULES = (
    "You are the synthesis agent. Your answer must be based ONLY on the "
    "explicitly provided expert results below.",
    "Never claim that an agent or specialist was consulted unless it is "
    "listed below.",
    "Never describe missing content as confirmed fact; explicitly mark any "
    "gap as missing.",
    "When provided results conflict, explicitly point out the conflict.",
    "Distinguish expert facts, inferences and recommendations in your answer.",
    "Do not output internal step IDs, system contracts or runtime metadata "
    "unless the user explicitly asks for them.",
    "Do not re-plan, delegate, or call Memory, Retrieval or Tools.",
)


class SynthesisAgentAdapter:
    """Consumes the dependency-scoped result view and produces one final
    candidate as a typed result. It never writes Store, Memory or AgentState
    and never publishes user text."""

    def __init__(self, router) -> None:
        self._router = router

    def execute(
        self,
        request: AgentExecutionRequest,
        run_context: RunContext,
    ) -> AgentAdapterResult:
        if not isinstance(request, AgentExecutionRequest):
            raise SynthesisInputError(
                SynthesisInputErrorCode.MISSING_DEPENDENCIES,
                "synthesis adapter 需要 AgentExecutionRequest",
            )
        view = request.dependency_results
        if view is None:
            raise SynthesisInputError(
                SynthesisInputErrorCode.MISSING_DEPENDENCIES,
                "synthesis 缺少依赖结果视图",
            )
        self._validate_dependencies(view)
        denial = next(
            (
                entry
                for entry in view
                if entry.result_disposition is ResultDisposition.SECURITY_DENIED
            ),
            None,
        )
        if denial is not None:
            return AgentAdapterResult(
                request.content_type,
                denial.content,
                complete=True,
                result_disposition=ResultDisposition.SECURITY_DENIED,
                security_denial_code=denial.security_denial_code,
            )
        context_items = self._build_context_items(request.instruction, view)
        try:
            text = self._router.complete_context_items(
                request.agent_id,
                context_items,
                run_context=run_context,
                capability_requirements=request.capability_requirements,
                user_query=request.instruction,
                event_emitter=request.event_emitter,
                fault_controller=request.fault_controller,
            )
        except (
            asyncio.CancelledError,
            RunCancelledError,
            RunDeadlineExceededError,
            BudgetExceededError,
        ):
            raise
        except Exception:
            raise SynthesisInputError(
                SynthesisInputErrorCode.SYNTHESIS_MODEL_FAILED,
                "synthesis 模型调用失败",
            ) from None
        if not isinstance(text, str) or not text.strip():
            raise SynthesisInputError(
                SynthesisInputErrorCode.SYNTHESIS_RESULT_INVALID,
                "synthesis 返回了非法结果",
            )
        return AgentAdapterResult(request.content_type, text, complete=True)

    @staticmethod
    def _build_context_items(
        instruction: str,
        view: DependencyResultView,
    ) -> tuple[ContextItem, ...]:
        now = datetime.now(UTC)
        items = [
            ContextItem(
                "synthesis-system-instruction",
                ContextSourceType.SYSTEM_INSTRUCTION,
                ContextTrustLevel.TRUSTED_INSTRUCTION,
                "\n".join(_SYNTHESIS_SYSTEM_RULES),
                1000,
                now,
            ),
            ContextItem(
                "synthesis-current-step",
                ContextSourceType.CURRENT_STEP,
                ContextTrustLevel.USER_CONTENT,
                instruction,
                900,
                now,
            ),
        ]
        for index, entry in enumerate(view, start=1):
            items.append(
                ContextItem(
                    f"synthesis-step-result-{index}",
                    ContextSourceType.STEP_RESULT,
                    ContextTrustLevel.USER_CONTENT,
                    entry.content,
                    max(1, 800 - index),
                    now,
                    source_ref=entry.producer_agent_id,
                    mandatory=True,
                )
            )
        return tuple(items)

    def _validate_dependencies(self, view: DependencyResultView) -> None:
        if len(view) == 0:
            raise SynthesisInputError(
                SynthesisInputErrorCode.MISSING_DEPENDENCIES,
                "synthesis 没有任何 required dependency result",
            )
        for entry in view:
            if entry.complete is not True:
                raise SynthesisInputError(
                    SynthesisInputErrorCode.INCOMPLETE_RESULT,
                    "required dependency result 未完成",
                )
            if entry.content_type not in {
                ResultContentType.TEXT,
                ResultContentType.MARKDOWN,
            }:
                raise SynthesisInputError(
                    SynthesisInputErrorCode.UNSUPPORTED_RESULT_TYPE,
                    "dependency result type 不被 synthesis 接受",
                )

    @staticmethod
    def _build_prompt(
        instruction: str,
        view: DependencyResultView,
    ) -> str:
        lines = [
            "You are the synthesis agent. Your answer must be based ONLY on "
            "the explicitly provided expert results below.",
            "Rules:",
        ]
        lines.extend(f"- {rule}" for rule in _SYNTHESIS_SYSTEM_RULES)
        lines.append("")
        lines.append(f"Task instruction:\n{instruction}")
        lines.append("")
        lines.append(
            "Provided expert results (stable dependency order, raw content):"
        )
        for index, entry in enumerate(view, start=1):
            lines.append(
                f"{index}. producer_agent_id: {entry.producer_agent_id}\n"
                f"   content_type: {entry.content_type.value}\n"
                f"   complete: {str(entry.complete).lower()}\n"
                f"   content: {entry.content}"
            )
        return "\n".join(lines)


__all__ = [
    "SynthesisAgentAdapter",
    "SynthesisInputError",
    "SynthesisInputErrorCode",
]
