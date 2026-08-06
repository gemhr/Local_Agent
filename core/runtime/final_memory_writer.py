#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Run-level delivered-only final Memory owner (WP4 minimum contract).

The final consensus requires Memory to receive only the original user message
and the unique, confirmed-DELIVERED final assistant message. INTERNAL,
FAILED and OUTCOME_UNKNOWN finals never reach Memory; specialist/Synthesis
raw results are never written. The completion pipeline calls this writer only
after OutputGate reports DELIVERED.
"""

from __future__ import annotations

import threading

from core.runtime.step_result_store import StepResultStore


class RunFinalMemoryWriter:
    """One write per Run; write-once guarded; uses the existing direct scope.

    Scope contract (source-derived, not invented):
    - the entry agent's existing ``direct`` conversation scope receives the
      original user message and the delivered final assistant message, which
      preserves the single-Agent direct history persistence behavior;
    - the writer never stores specialist/Synthesis raw results.
    """

    def __init__(
        self,
        router,
        *,
        entry_agent_id: str,
        user_request: str,
        persist: bool,
    ) -> None:
        if not isinstance(entry_agent_id, str) or not entry_agent_id.strip():
            raise ValueError("entry_agent_id 不能为空")
        if not isinstance(user_request, str) or not user_request.strip():
            raise ValueError("user_request 不能为空")
        if type(persist) is not bool:
            raise TypeError("persist 必须是 bool")
        self._router = router
        self._entry_agent_id = entry_agent_id.strip()
        self._user_request = user_request
        self._persist = persist
        self._write_lock = threading.Lock()
        self._written = False

    @property
    def written(self) -> bool:
        with self._write_lock:
            return self._written

    def write_delivered(
        self,
        *,
        final_step_id: str,
        store: StepResultStore,
    ) -> None:
        """Write the original user message and the delivered final once."""
        if not self._persist:
            return
        if not isinstance(store, StepResultStore):
            raise TypeError("write_delivered 需要 StepResultStore")
        with self._write_lock:
            if self._written:
                raise RuntimeError(
                    "Run 级 final Memory 只能写入一次，拒绝重复写入"
                )
            self._written = True
        content = store.read_final_content(final_step_id)
        memory = getattr(self._router, "memory_manager", None)
        if memory is None:
            raise RuntimeError("router 没有可用的 memory_manager")
        scope = getattr(self._router, "DIRECT_MEMORY_SCOPE", "direct")
        try:
            memory.add_message(
                self._entry_agent_id,
                "user",
                self._user_request,
                memory_scope=scope,
            )
            memory.add_message(
                self._entry_agent_id,
                "assistant",
                content,
                memory_scope=scope,
            )
        except BaseException:
            self._written = False
            raise

    def __repr__(self) -> str:
        return (
            "RunFinalMemoryWriter("
            f"entry_agent_id={self._entry_agent_id!r}, "
            f"persist={self._persist!r}, written={self.written!r})"
        )


__all__ = ["RunFinalMemoryWriter"]
