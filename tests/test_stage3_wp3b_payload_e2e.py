from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass, field

import httpx
import pytest
import pytest_asyncio

import server
from core.request_payload import (
    REQUEST_PAYLOAD_POLICY,
    RequestBodyLimitMiddleware,
)
from core.runtime import ChatRuntimeMode


POLICY = REQUEST_PAYLOAD_POLICY


async def _invoke_middleware(*, headers=(), messages, scope_type="http"):
    calls = {"downstream": 0, "body": b""}
    sent = []
    queue = deque(messages)

    async def receive():
        if queue:
            return queue.popleft()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    async def app(scope, receive, send):
        calls["downstream"] += 1
        chunks = []
        while True:
            message = await receive()
            assert message["type"] == "http.request"
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        calls["body"] = b"".join(chunks)
        await send({"type": "http.response.start", "status": 204, "headers": ()})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    middleware = RequestBodyLimitMiddleware(app)
    await middleware(
        {"type": scope_type, "method": "POST", "path": "/", "headers": headers},
        receive,
        send,
    )
    return calls, sent


def _request_messages(*chunks: bytes):
    return [
        {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
        for index, chunk in enumerate(chunks)
    ]


def _response(sent):
    start = next(message for message in sent if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    return start, body


@pytest.mark.asyncio
async def test_non_http_scope_is_not_intercepted() -> None:
    calls, _ = await _invoke_middleware(
        scope_type="websocket",
        messages=_request_messages(b""),
    )
    assert calls["downstream"] == 1


@pytest.mark.asyncio
async def test_declared_over_limit_is_early_fixed_413() -> None:
    calls, sent = await _invoke_middleware(
        headers=((b"content-length", str(POLICY.HTTP_BODY_MAX_BYTES + 1).encode()),),
        messages=_request_messages(b"not-read"),
    )
    start, body = _response(sent)
    assert calls["downstream"] == 0
    assert start["status"] == 413
    assert dict(start["headers"])[b"content-type"] == b"application/json"
    assert body == b'{"detail":"Payload Too Large"}'


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [b"", b"-1", b"+1", b"1.0", b"1,2", b"abc", b" 1"])
async def test_invalid_content_length_is_fixed_400(declared: bytes) -> None:
    calls, sent = await _invoke_middleware(
        headers=((b"content-length", declared),),
        messages=_request_messages(b"x"),
    )
    start, body = _response(sent)
    assert calls["downstream"] == 0
    assert start["status"] == 400
    assert body == b'{"detail":"Invalid Content-Length"}'
    if declared:
        assert declared not in body


@pytest.mark.asyncio
async def test_multiple_content_lengths_are_fixed_400() -> None:
    calls, sent = await _invoke_middleware(
        headers=((b"content-length", b"1"), (b"content-length", b"1")),
        messages=_request_messages(b"x"),
    )
    start, body = _response(sent)
    assert calls["downstream"] == 0
    assert start["status"] == 400
    assert body == b'{"detail":"Invalid Content-Length"}'


@pytest.mark.asyncio
async def test_leading_zero_content_length_and_exact_actual_body_are_allowed() -> None:
    body = b"x" * POLICY.HTTP_BODY_MAX_BYTES
    calls, sent = await _invoke_middleware(
        headers=((b"content-length", f"00{len(body)}".encode()),),
        messages=_request_messages(body),
    )
    assert calls == {"downstream": 1, "body": body}
    assert _response(sent)[0]["status"] == 204


@pytest.mark.asyncio
async def test_exact_limit_across_multiple_chunks_is_replayed_in_order() -> None:
    first = b"a" * (POLICY.HTTP_BODY_MAX_BYTES // 2)
    second = b"b" * (POLICY.HTTP_BODY_MAX_BYTES - len(first))
    calls, sent = await _invoke_middleware(
        headers=((b"content-length", str(POLICY.HTTP_BODY_MAX_BYTES).encode()),),
        messages=_request_messages(first, second),
    )
    assert calls == {"downstream": 1, "body": first + second}
    assert _response(sent)[0]["status"] == 204


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [(), ((b"content-length", b"10"),), ((b"content-length", b"1048576"),)])
async def test_actual_bytes_are_authority_for_missing_lying_or_equal_header(headers) -> None:
    chunks = (b"a" * POLICY.HTTP_BODY_MAX_BYTES, b"b")
    calls, sent = await _invoke_middleware(headers=headers, messages=_request_messages(*chunks))
    start, body = _response(sent)
    assert calls["downstream"] == 0
    assert start["status"] == 413
    assert body == b'{"detail":"Payload Too Large"}'


@pytest.mark.asyncio
async def test_disconnect_or_unknown_message_never_calls_downstream() -> None:
    for message in ({"type": "http.disconnect"}, {"type": "unexpected", "body": b"secret"}):
        calls, sent = await _invoke_middleware(messages=[message])
        assert calls["downstream"] == 0
        assert sent == []


class _Registry:
    def __init__(self, owner):
        self.owner = owner

    def cancel(self, *args, **kwargs):
        self.owner.registry_calls += 1
        return None


@dataclass
class _ServiceSpy:
    chat_calls: int = 0
    history_calls: int = 0
    search_calls: int = 0
    delete_calls: int = 0
    registry_calls: int = 0
    seen: list[dict] = field(default_factory=list)
    admission_gate = None

    def __post_init__(self):
        self.run_registry = _Registry(self)

    def selected_runtime_mode(self):
        return ChatRuntimeMode.LEGACY

    def stream_chat(self, *, agent_id, query, file_path, run_id):
        self.chat_calls += 1
        self.seen.append({"agent_id": agent_id, "query": query, "file_path": file_path, "run_id": run_id})
        yield "ok"

    def get_history(self, *, agent_id, limit, offset):
        self.history_calls += 1
        self.seen.append({"agent_id": agent_id, "limit": limit, "offset": offset})
        return []

    def search_memory(self, keyword):
        self.search_calls += 1
        self.seen.append({"keyword": keyword})
        return []

    def get_all_memory(self):
        return {"messages": [], "summaries": []}

    def delete_memory(self, *, message_ids, delete_all):
        self.delete_calls += 1
        self.seen.append({"message_ids": message_ids, "delete_all": delete_all})
        return {"deleted": 0}


@pytest.fixture
def service_spy(monkeypatch):
    spy = _ServiceSpy()
    monkeypatch.setattr(server, "chat_service", spy)
    return spy


@pytest_asyncio.fixture
async def client():
    transport = httpx.ASGITransport(app=server.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as value:
        yield value


@pytest.mark.asyncio
async def test_real_app_oversized_chat_body_and_ignored_json_are_pre_service_413(client, service_spy) -> None:
    payload = {"agent_id": "general", "query": "x", "ignored": "z" * POLICY.HTTP_BODY_MAX_BYTES}
    response = await client.post("/api/chat", content=json.dumps(payload), headers={"content-type": "application/json"})
    assert response.status_code == 413
    assert response.json() == {"detail": "Payload Too Large"}
    assert service_spy.chat_calls == 0
    assert service_spy.registry_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"query": "q" * (POLICY.CHAT_QUERY_MAX_CHARS + 1)},
        {"file_path": "f" * (POLICY.CHAT_FILE_PATH_MAX_CHARS + 1)},
        {"agent_id": "a" * (POLICY.AGENT_ID_MAX_CHARS + 1)},
        {"run_id": "r" * (POLICY.RUN_ID_MAX_CHARS + 1)},
    ],
)
async def test_real_app_oversized_chat_fields_are_422_before_service(client, service_spy, changes) -> None:
    payload = {"agent_id": "general", "query": "x", **changes}
    response = await client.post("/api/chat", json=payload)
    assert response.status_code == 422
    assert service_spy.chat_calls == 0
    assert service_spy.registry_calls == 0


@pytest.mark.asyncio
async def test_real_app_exact_query_and_small_unknown_reach_service(client, service_spy) -> None:
    query = "😀" * POLICY.CHAT_QUERY_MAX_CHARS
    response = await client.post(
        "/api/chat",
        json={"agent_id": "general", "query": query, "unexpected": "ignored"},
    )
    assert response.status_code == 200
    assert response.text == "ok"
    assert service_spy.chat_calls == 1
    assert service_spy.seen[-1]["query"] == query


@pytest.mark.asyncio
async def test_empty_query_with_file_path_remains_compatible(client, service_spy) -> None:
    response = await client.post(
        "/api/chat",
        json={"agent_id": "general", "query": "", "file_path": "C:/data/example.txt"},
    )
    assert response.status_code == 200
    assert service_spy.chat_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "run_id",
    [
        "12345678123456781234567812345678",
        "12345678-1234-5678-1234-567812345678",
        "{12345678-1234-5678-1234-567812345678}",
        "urn:uuid:12345678-1234-5678-1234-567812345678",
    ],
)
async def test_run_id_forms_preserve_original_header(client, service_spy, run_id: str) -> None:
    response = await client.post("/api/chat", json={"agent_id": "general", "query": "x", "run_id": run_id})
    assert response.status_code == 200
    assert response.headers["x-run-id"] == run_id


@pytest.mark.asyncio
async def test_malformed_run_id_within_limit_keeps_fixed_route_422(client, service_spy) -> None:
    response = await client.post("/api/chat", json={"agent_id": "general", "query": "x", "run_id": "malformed"})
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid run_id"}
    assert service_spy.chat_calls == 0


@pytest.mark.asyncio
async def test_cancel_run_id_path_limit_and_uuid_compatibility(client, service_spy) -> None:
    oversized = await client.post("/api/runtime/runs/" + "r" * (POLICY.RUN_ID_MAX_CHARS + 1) + "/cancel")
    assert oversized.status_code == 422
    assert service_spy.registry_calls == 0
    run_id = "urn:uuid:12345678-1234-5678-1234-567812345678"
    valid = await client.post(f"/api/runtime/runs/{run_id}/cancel")
    assert valid.status_code == 200
    assert valid.json()["run_id"] == run_id


@pytest.mark.asyncio
async def test_search_keyword_bounds_and_empty_compatibility(client, service_spy) -> None:
    for keyword in ("", "k" * POLICY.SEARCH_KEYWORD_MAX_CHARS):
        response = await client.get("/api/search", params={"keyword": keyword})
        assert response.status_code == 200
    response = await client.get("/api/search", params={"keyword": "k" * (POLICY.SEARCH_KEYWORD_MAX_CHARS + 1)})
    assert response.status_code == 422
    assert service_spy.search_calls == 2


@pytest.mark.asyncio
async def test_history_bounds_reject_before_service(client, service_spy) -> None:
    valid = await client.get("/api/history/" + "a" * POLICY.AGENT_ID_MAX_CHARS, params={"limit": 1, "offset": 100_000})
    assert valid.status_code == 200
    for path, params in [
        ("/api/history/" + "a" * (POLICY.AGENT_ID_MAX_CHARS + 1), {}),
        ("/api/history/general", {"limit": 0}),
        ("/api/history/general", {"limit": -1}),
        ("/api/history/general", {"limit": 101}),
        ("/api/history/general", {"offset": -1}),
        ("/api/history/general", {"offset": 100_001}),
    ]:
        assert (await client.get(path, params=params)).status_code == 422
    assert service_spy.history_calls == 1


@pytest.mark.asyncio
async def test_history_omitted_query_uses_policy_owned_defaults(client, service_spy) -> None:
    response = await client.get("/api/history/general")
    assert response.status_code == 200
    assert service_spy.history_calls == 1
    assert service_spy.seen[-1] == {
        "agent_id": "general",
        "limit": POLICY.HISTORY_LIMIT_DEFAULT,
        "offset": POLICY.HISTORY_OFFSET_DEFAULT,
    }


@pytest.mark.asyncio
async def test_memory_delete_body_count_and_id_reject_before_mutation(client, service_spy) -> None:
    oversized_body = await client.request(
        "DELETE",
        "/api/memory",
        content=json.dumps({"message_ids": [], "ignored": "z" * POLICY.HTTP_BODY_MAX_BYTES}),
        headers={"content-type": "application/json"},
    )
    assert oversized_body.status_code == 413
    too_many = await client.request("DELETE", "/api/memory", json={"message_ids": list(range(1, 1002))})
    assert too_many.status_code == 422
    for invalid in (0, -1, POLICY.MESSAGE_ID_MAX + 1):
        response = await client.request("DELETE", "/api/memory", json={"message_ids": [invalid]})
        assert response.status_code == 422
    assert service_spy.delete_calls == 0


@pytest.mark.asyncio
async def test_memory_delete_exact_count_reaches_service_without_partial_mutation(client, service_spy) -> None:
    ids = list(range(1, POLICY.DELETE_MESSAGE_IDS_MAX_COUNT + 1))
    response = await client.request("DELETE", "/api/memory", json={"message_ids": ids, "delete_all": True})
    assert response.status_code == 200
    assert service_spy.delete_calls == 1
    assert service_spy.seen[-1] == {"message_ids": ids, "delete_all": True}
