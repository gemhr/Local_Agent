#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""HTTP 请求载荷的固定边界与 application-wide ASGI Gate。"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Awaitable, Callable, Mapping


ASGIMessage = dict[str, Any]
ASGIScope = Mapping[str, Any]
ASGIReceive = Callable[[], Awaitable[ASGIMessage]]
ASGISend = Callable[[ASGIMessage], Awaitable[None]]
ASGIApp = Callable[[ASGIScope, ASGIReceive, ASGISend], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class RequestPayloadPolicy:
    """不可变、不可覆盖的进程级请求载荷数值事实源。"""

    HTTP_BODY_MAX_BYTES: int = 1_048_576
    CHAT_QUERY_MAX_CHARS: int = 32_768
    CHAT_FILE_PATH_MAX_CHARS: int = 4_096
    AGENT_ID_MAX_CHARS: int = 64
    RUN_ID_MAX_CHARS: int = 45
    SEARCH_KEYWORD_MAX_CHARS: int = 1_024
    HISTORY_LIMIT_DEFAULT: int = 10
    HISTORY_LIMIT_MIN: int = 1
    HISTORY_LIMIT_MAX: int = 100
    HISTORY_OFFSET_DEFAULT: int = 0
    HISTORY_OFFSET_MIN: int = 0
    HISTORY_OFFSET_MAX: int = 100_000
    DELETE_MESSAGE_IDS_MAX_COUNT: int = 1_000
    MESSAGE_ID_MIN: int = 1
    MESSAGE_ID_MAX: int = 9_223_372_036_854_775_807

    def __post_init__(self) -> None:
        """拒绝任何 constructor override，包括另一组看似合法的正整数。"""
        for definition in fields(self):
            value = getattr(self, definition.name)
            if type(value) is not int or value != definition.default:
                raise ValueError(
                    "RequestPayloadPolicy 使用固定整数且不允许运行时覆盖"
                )


REQUEST_PAYLOAD_POLICY = RequestPayloadPolicy()


class RequestBodyLimitMiddleware:
    """在 FastAPI/Pydantic 解析前按实际 ASGI body bytes 执行有界 Gate。"""

    def __init__(
        self,
        app: ASGIApp,
        *,
        policy: RequestPayloadPolicy = REQUEST_PAYLOAD_POLICY,
    ) -> None:
        if not isinstance(policy, RequestPayloadPolicy):
            raise TypeError("policy 必须是 RequestPayloadPolicy")
        max_body_bytes = policy.HTTP_BODY_MAX_BYTES
        if type(max_body_bytes) is not int or max_body_bytes <= 0:
            raise ValueError("HTTP body max 必须是固定正整数")
        self._app = app
        self._max_body_bytes = max_body_bytes

    async def __call__(
        self,
        scope: ASGIScope,
        receive: ASGIReceive,
        send: ASGISend,
    ) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        content_lengths = tuple(
            value
            for name, value in scope.get("headers", ())
            if bytes(name).lower() == b"content-length"
        )
        if len(content_lengths) > 1:
            await self._send_fixed_json(send, 400, b'{"detail":"Invalid Content-Length"}')
            return
        if content_lengths:
            declared = self._parse_content_length(content_lengths[0])
            if declared is None:
                await self._send_fixed_json(
                    send, 400, b'{"detail":"Invalid Content-Length"}'
                )
                return
            if declared > self._max_body_bytes:
                await self._send_fixed_json(
                    send, 413, b'{"detail":"Payload Too Large"}'
                )
                return

        buffered: list[ASGIMessage] = []
        total = 0
        while True:
            message = await receive()
            message_type = message.get("type")
            if message_type == "http.disconnect":
                buffered.clear()
                return
            if message_type != "http.request":
                buffered.clear()
                return
            body = message.get("body", b"")
            if not isinstance(body, bytes):
                buffered.clear()
                return
            total += len(body)
            if total > self._max_body_bytes:
                buffered.clear()
                await self._send_fixed_json(
                    send, 413, b'{"detail":"Payload Too Large"}'
                )
                return
            buffered.append(message)
            if not message.get("more_body", False):
                break

        index = 0

        async def replay_receive() -> ASGIMessage:
            nonlocal index
            if index < len(buffered):
                message = buffered[index]
                index += 1
                return message
            return await receive()

        await self._app(scope, replay_receive, send)

    @staticmethod
    def _parse_content_length(value: bytes) -> int | None:
        if not value or any(byte < 48 or byte > 57 for byte in value):
            return None
        return int(value)

    @staticmethod
    async def _send_fixed_json(
        send: ASGISend, status: int, body: bytes
    ) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": (
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ),
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": body,
                "more_body": False,
            }
        )


__all__ = [
    "REQUEST_PAYLOAD_POLICY",
    "RequestBodyLimitMiddleware",
    "RequestPayloadPolicy",
]
