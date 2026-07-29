#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Strict chat runtime mode parsing and immutable request selection."""

from __future__ import annotations

from enum import Enum


class ChatRuntimeMode(str, Enum):
    """The only supported application chat runtime modes."""

    LEGACY = "LEGACY"
    COORDINATED = "COORDINATED"

    @classmethod
    def parse(
        cls,
        value: object,
        *,
        default: "ChatRuntimeMode | None" = None,
    ) -> "ChatRuntimeMode":
        """Normalize case and whitespace, while rejecting every unknown value."""
        active_default = default or cls.LEGACY
        if not isinstance(active_default, cls):
            raise TypeError("default must be a ChatRuntimeMode")
        if value is None:
            return active_default
        if not isinstance(value, str):
            raise TypeError("CHAT_RUNTIME_MODE must be a string")
        normalized = value.strip().upper()
        if not normalized:
            return active_default
        try:
            return cls(normalized)
        except ValueError:
            raise ValueError("CHAT_RUNTIME_MODE is unsupported") from None


class ChatRuntimeSelector:
    """Application-scoped selector returning one immutable enum snapshot."""

    __slots__ = ("_mode",)

    def __init__(self, mode: ChatRuntimeMode) -> None:
        if not isinstance(mode, ChatRuntimeMode):
            raise TypeError("mode must be a ChatRuntimeMode")
        self._mode = mode

    def selected_runtime_mode(self) -> ChatRuntimeMode:
        """Capture the configured mode once at request entry."""
        return self._mode

    def capture(self) -> ChatRuntimeMode:
        """Equivalent request-entry spelling used by assembly callers."""
        return self.selected_runtime_mode()

    def __repr__(self) -> str:
        return f"ChatRuntimeSelector(mode={self._mode.value!r})"


__all__ = ["ChatRuntimeMode", "ChatRuntimeSelector"]
