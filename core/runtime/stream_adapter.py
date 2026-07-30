#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Adapt Runtime Events to the desktop client's custom text-chunk protocol."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json

from core.runtime.events import (
    OutputDeltaPayload,
    RuntimeEvent,
    RuntimeEventType,
)


class ChatStreamChunkKind(str, Enum):
    """The three transport-level chunk categories."""

    TEXT = "TEXT"
    CONTROL = "CONTROL"
    SAFE_ERROR = "SAFE_ERROR"


@dataclass(frozen=True, slots=True)
class ChatStreamChunk:
    """One complete client-consumable chunk."""

    kind: ChatStreamChunkKind
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ChatStreamChunkKind):
            raise TypeError("kind must be ChatStreamChunkKind")
        if not isinstance(self.text, str):
            raise TypeError("text must be a string")


class ChatStreamProtocolError(RuntimeError):
    """A path-free transport protocol failure."""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


_CONTROL_EVENT_TYPES = frozenset(
    {
        RuntimeEventType.RUN_STARTED,
        RuntimeEventType.STEP_STARTED,
        RuntimeEventType.STEP_COMPLETED,
        RuntimeEventType.MODEL_STARTED,
        RuntimeEventType.MODEL_COMPLETED,
        RuntimeEventType.TOOL_STARTED,
        RuntimeEventType.TOOL_COMPLETED,
        RuntimeEventType.RETRIEVAL_STARTED,
        RuntimeEventType.RETRIEVAL_STAGE_COMPLETED,
        RuntimeEventType.RETRIEVAL_COMPLETED,
        RuntimeEventType.CANCELLATION,
        RuntimeEventType.TIMEOUT,
        RuntimeEventType.BUDGET_EXHAUSTED,
        RuntimeEventType.RUN_COMPLETED,
    }
)

_PAYLOAD_FIELD_ALLOWLIST: dict[RuntimeEventType, tuple[str, ...]] = {
    RuntimeEventType.RUN_STARTED: ("status",),
    RuntimeEventType.STEP_STARTED: ("status",),
    RuntimeEventType.STEP_COMPLETED: (
        "status",
        "safe_error_code",
        "duration_ms",
    ),
    RuntimeEventType.MODEL_STARTED: (
        "profile_id",
        "candidate_index",
        "retry_index",
        "routing_adjustment",
    ),
    RuntimeEventType.MODEL_COMPLETED: (
        "profile_id",
        "candidate_index",
        "retry_index",
        "succeeded",
        "safe_error_code",
        "duration_ms",
    ),
    RuntimeEventType.TOOL_STARTED: ("tool_name", "retry_index"),
    RuntimeEventType.TOOL_COMPLETED: (
        "tool_name",
        "succeeded",
        "safe_error_code",
        "retry_index",
        "duration_ms",
        "status",
    ),
    RuntimeEventType.RETRIEVAL_STARTED: ("collection_count", "top_k"),
    RuntimeEventType.RETRIEVAL_STAGE_COMPLETED: (
        "stage",
        "status",
        "duration_ms",
        "input_count",
        "output_count",
        "degraded",
        "safe_error_code",
    ),
    RuntimeEventType.RETRIEVAL_COMPLETED: (
        "status",
        "duration_ms",
        "chunk_count",
        "citation_count",
        "degraded",
        "safe_error_code",
    ),
    RuntimeEventType.CANCELLATION: ("reason", "component"),
    RuntimeEventType.TIMEOUT: ("component", "safe_error_code"),
    RuntimeEventType.BUDGET_EXHAUSTED: (
        "component",
        "dimension",
        "safe_error_code",
    ),
    RuntimeEventType.RUN_COMPLETED: ("status", "stop_reason", "duration_ms"),
}

_SAFE_TRANSPORT_ERROR_CODES = frozenset(
    {
        "RUNTIME_CONFIGURATION_ERROR",
        "RUNTIME_SCOPE_CREATION_FAILED",
        "RUNTIME_EXECUTION_FAILED",
        "RUNTIME_STREAM_ENCODING_FAILED",
        "RUNTIME_TERMINAL_MISSING",
    }
)


def safe_transport_error_chunk(error_code: str) -> ChatStreamChunk:
    """Build one fixed client-visible error without exception details."""
    active_code = (
        error_code
        if error_code in _SAFE_TRANSPORT_ERROR_CODES
        else "RUNTIME_EXECUTION_FAILED"
    )
    return ChatStreamChunk(
        ChatStreamChunkKind.SAFE_ERROR,
        f"[runtime-error] {active_code}\n",
    )


class ChatStreamCompatibilityAdapter:
    """Stateful RuntimeEvent-to-wire adapter with terminal enforcement.

    This is a custom text chunk protocol. It does not emit standard SSE frames.
    """

    ORCHESTRATION_EVENT_PREFIX = "[[ORCH]]"

    def __init__(self) -> None:
        self._terminal_seen = False
        self._finished = False

    @property
    def terminal_seen(self) -> bool:
        return self._terminal_seen

    def adapt(self, event: RuntimeEvent) -> ChatStreamChunk | None:
        if not isinstance(event, RuntimeEvent):
            raise ChatStreamProtocolError("RUNTIME_STREAM_ENCODING_FAILED")
        if self._finished or self._terminal_seen:
            raise ChatStreamProtocolError("RUNTIME_STREAM_ENCODING_FAILED")

        if event.event_type is RuntimeEventType.OUTPUT_DELTA:
            if not isinstance(event.payload, OutputDeltaPayload):
                raise ChatStreamProtocolError("RUNTIME_STREAM_ENCODING_FAILED")
            if not event.payload.text:
                return None
            return ChatStreamChunk(ChatStreamChunkKind.TEXT, event.payload.text)

        if event.event_type is RuntimeEventType.ERROR:
            return safe_transport_error_chunk("RUNTIME_EXECUTION_FAILED")

        if event.event_type not in _CONTROL_EVENT_TYPES:
            return None

        if event.event_type is RuntimeEventType.RUN_COMPLETED:
            self._terminal_seen = True

        try:
            payload = {
                name: getattr(event.payload, name)
                for name in _PAYLOAD_FIELD_ALLOWLIST[event.event_type]
                if getattr(event.payload, name) is not None
            }
            projection = {
                "run_id": event.run_id,
                "sequence": event.sequence,
                "event_type": event.event_type.value,
                "step_id": event.step_id,
                "payload": payload,
            }
            encoded = json.dumps(
                projection,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        except Exception:
            raise ChatStreamProtocolError(
                "RUNTIME_STREAM_ENCODING_FAILED"
            ) from None

        return ChatStreamChunk(
            ChatStreamChunkKind.CONTROL,
            self.ORCHESTRATION_EVENT_PREFIX + encoded + "\n",
        )

    def finish(self) -> ChatStreamChunk | None:
        """Close transport adaptation without fabricating a Runtime terminal."""
        if self._finished:
            return None
        self._finished = True
        if not self._terminal_seen:
            return safe_transport_error_chunk("RUNTIME_TERMINAL_MISSING")
        return None


class RuntimeEventTextAdapter(ChatStreamCompatibilityAdapter):
    """Backward-compatible spelling for callers that expect ``encode``."""

    def encode(self, event: RuntimeEvent) -> str:
        chunk = self.adapt(event)
        return "" if chunk is None else chunk.text


__all__ = [
    "ChatStreamChunk",
    "ChatStreamChunkKind",
    "ChatStreamCompatibilityAdapter",
    "ChatStreamProtocolError",
    "RuntimeEventTextAdapter",
    "safe_transport_error_chunk",
]
