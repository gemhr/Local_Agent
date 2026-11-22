"""OpenCode 1.18.26 Legacy SSE 的最小安全投影。"""

from collections.abc import AsyncIterator
import json
import re

import httpx

from core.agent_platform.runtime_adapter import RuntimeUpdate


async def legacy_events(response: httpx.Response) -> AsyncIterator[dict]:
    lines: list[str] = []
    async for line in response.aiter_lines():
        if line.startswith("data:"):
            lines.append(line[5:].removeprefix(" "))
        elif not line and lines:
            payload = "\n".join(lines)
            lines.clear()
            try:
                event = json.loads(payload)
            except ValueError:
                continue
            if isinstance(event, dict):
                yield event


class OpenCodeEventProjection:
    def __init__(self, run_id: str, trace_id: str, session_id: str, user_message_id: str) -> None:
        self.run_id = run_id
        self.trace_id = trace_id
        self.session_id = session_id
        self.user_message_id = user_message_id
        self._assistant_id: str | None = None
        self._text_parts: dict[str, str] = {}
        self._seen: dict[str, None] = {}
        self._progress: str | None = None

    def project(self, event: dict) -> RuntimeUpdate | None:
        event_id = event.get("id")
        properties = event.get("properties")
        if not isinstance(event_id, str) or event_id in self._seen or not isinstance(properties, dict):
            return None
        event_type = event.get("type")
        if event_type == "message.updated":
            info = properties.get("info")
            if (
                isinstance(info, dict) and info.get("role") == "assistant"
                and info.get("sessionID") == self.session_id and info.get("parentID") == self.user_message_id
                and isinstance(info.get("id"), str) and re.fullmatch(r"msg_[A-Za-z0-9]+", info["id"])
                and info["id"] != self.user_message_id and self._assistant_id is None
            ):
                self._assistant_id = info["id"]
            return None
        if event_type == "message.part.updated":
            part = properties.get("part")
            if not self._owns_part(part):
                return None
            part_id = part.get("id")
            if not isinstance(part_id, str) or not re.fullmatch(r"prt_[A-Za-z0-9]+", part_id):
                return None
            if part.get("type") == "tool":
                state = part.get("state")
                status = state.get("status") if isinstance(state, dict) else None
                return self._control(event_id, f"tool.{status}") if status in (
                    "pending", "running", "completed", "error"
                ) else None
            if part.get("type") != "text" or part.get("ignored") or part.get("synthetic"):
                self._text_parts.pop(part_id, None)
                return None
            value = part.get("text")
            if isinstance(value, str):
                old = self._text_parts.setdefault(part_id, "")
                if value.startswith(old):
                    self._text_parts[part_id] = value
                    return self._update(event_id, "output_delta", value[len(old):])
            return None
        if event_type == "message.part.delta":
            if not self._owns_part(properties) or properties.get("field") != "text":
                return None
            part_id, delta = properties.get("partID"), properties.get("delta")
            if isinstance(part_id, str) and part_id in self._text_parts and isinstance(delta, str):
                self._text_parts[part_id] += delta
                return self._update(event_id, "output_delta", delta)
            return None
        if properties.get("sessionID") != self.session_id:
            return None
        if event_type == "session.status":
            status = properties.get("status")
            phase = status.get("type") if isinstance(status, dict) else None
            if phase in ("busy", "retry", "idle"):
                return self._control(event_id, f"session.{phase}")
        if event_type == "session.error":
            return self._control(event_id, "session.error")
        return None

    def _owns_part(self, part: object) -> bool:
        return (
            self._assistant_id is not None and isinstance(part, dict)
            and part.get("sessionID") == self.session_id and part.get("messageID") == self._assistant_id
        )

    def _control(self, event_id: str, phase: str) -> RuntimeUpdate | None:
        if self._progress == phase:
            return None
        self._progress = phase
        return self._update(event_id, "control_delta", phase)

    def _update(self, event_id: str, kind, value: str) -> RuntimeUpdate | None:
        if not value:
            return None
        self._seen[event_id] = None
        if len(self._seen) > 4096:
            self._seen.pop(next(iter(self._seen)))
        return RuntimeUpdate(kind, self.run_id, self.trace_id, value)
