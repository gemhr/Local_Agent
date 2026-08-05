#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WP2 PlanningModel adapter backed by the existing unified model contract."""

from __future__ import annotations

import asyncio

from core.runtime.blocking_executor import BlockingTaskKind
from core.runtime.multi_agent_planning import PlanningRequest


class UnifiedPlanningModelAdapter:
    """Run-scoped adapter; retry, budget, circuit and provider fallback stay unified."""

    def __init__(
        self,
        router,
        *,
        blocking_executor,
        event_emitter=None,
        fault_controller=None,
    ) -> None:
        self._router = router
        self._blocking_executor = blocking_executor
        self._event_emitter = event_emitter
        self._fault_controller = fault_controller

    async def generate_plan(self, request: PlanningRequest, run_context) -> str:
        run_context.raise_if_inactive()

        def admission_remaining() -> float:
            remaining = run_context.remaining_seconds()
            return 30.0 if remaining is None else max(0.0, remaining)

        handle = self._blocking_executor.submit(
            lambda: self._router.complete_planning_decision(
                request.user_request,
                run_context=run_context,
                event_emitter=self._event_emitter,
                fault_controller=self._fault_controller,
            ),
            kind=BlockingTaskKind.PLANNING_MODEL,
            run_id=run_context.run_id,
            operation_id="planning-model",
            cancellation_check=run_context.raise_if_inactive,
            remaining_seconds=admission_remaining,
        )
        try:
            result = await handle.result_async()
        except asyncio.CancelledError:
            handle.cancel_or_detach()
            raise
        run_context.raise_if_inactive()
        return result


__all__ = ["UnifiedPlanningModelAdapter"]
