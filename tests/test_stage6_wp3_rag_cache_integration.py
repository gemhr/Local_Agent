from __future__ import annotations

import asyncio
import json
import os

import pytest
import pytest_asyncio
import redis.asyncio as redis

from core.redis_service import RagQueryCache, stable_digest
from core.runtime import (
    BudgetLedger,
    RetrievalExecutionService,
    RetrievalExecutionStatus,
    RetrievalInvocation,
    RunBudget,
    create_run_context,
)
from core.runtime.retrieval_cache import CachedRetrievalExecutionService, SyncRedisBridge
from tests.test_retrieval_execution import FakeRetrievalAdapter


class _CacheSettings:
    rag_cache_enabled = True
    rag_cache_ttl_seconds = 30
    rag_cache_ttl_jitter_seconds = 0
    rag_cache_max_value_bytes = 262144


class _CaptureEmitter:
    def __init__(self) -> None:
        self.events = []

    def emit_from_worker(self, event_type, payload, *, component):
        self.events.append((event_type.value, payload, component))


@pytest_asyncio.fixture
async def real_redis_client():
    client = redis.from_url(
        os.getenv("LOCAL_AGENT_TEST_REDIS_URL", "redis://127.0.0.1:6380/0"),
        decode_responses=True,
    )
    try:
        await client.ping()
    except Exception as exc:
        await client.aclose()
        pytest.fail(f"Redis test instance unavailable: {type(exc).__name__}")
    keys: list[str] = []
    try:
        yield client, keys
    finally:
        if keys:
            await client.delete(*keys)
        await client.aclose()


def _context(authz_domain: str | None, *, timeout_seconds: float | None = None):
    context, _source = create_run_context(
        entry_agent_id="knowledge_expert", timeout_seconds=timeout_seconds
    )
    context.attach_retrieval_cache_access(authz_domain)
    context.attach_budget_ledger(
        BudgetLedger(RunBudget(), deadline_remaining=context.remaining_seconds)
    )
    return context


def _invocation(retrieval_id: str, *, top_k: int = 4) -> RetrievalInvocation:
    return RetrievalInvocation.create(
        "same production query",
        collection_names=("kb",),
        top_k=top_k,
        rerank_top_k=2,
        requested_timeout_seconds=2.0,
        retrieval_id=retrieval_id,
    )


def _business_projection(result):
    return [
        (
            chunk.text,
            chunk.score,
            chunk.source,
            chunk.provenance,
            chunk.citation.display_label,
        )
        for chunk in result.final_chunks
    ]


@pytest.mark.asyncio
async def test_production_cache_miss_then_hit_restores_business_contract_and_new_runtime_evidence(
    real_redis_client,
) -> None:
    client, cleanup_keys = real_redis_client
    adapter = FakeRetrievalAdapter()
    origin = RetrievalExecutionService(adapter)
    cache = RagQueryCache(client, _CacheSettings())
    bridge = SyncRedisBridge(asyncio.get_running_loop(), timeout_seconds=2.0)
    service = CachedRetrievalExecutionService(
        origin, cache, bridge, index_generation_provider=lambda: "generation-1"
    )
    first_invocation = _invocation("origin-request")
    key = cache.key(
        authz_domain="user-a-domain",
        index_generation="generation-1",
        policy=service._policy_identity(first_invocation),
        query=first_invocation.original_query,
    )
    cleanup_keys.append(key)

    first = await asyncio.to_thread(
        service.execute, first_invocation, run_context=_context("user-a-domain")
    )
    origin_calls = tuple(adapter.calls)
    emitter = _CaptureEmitter()
    second = await asyncio.to_thread(
        service.execute,
        _invocation("cached-request"),
        run_context=_context("user-a-domain"),
        event_emitter=emitter,
    )

    assert tuple(adapter.calls) == origin_calls
    assert _business_projection(second) == _business_projection(first)
    assert second.retrieval_id == "cached-request"
    assert second.citations[0].citation_id.startswith("Rcached-r")
    assert second.started_at >= first.completed_at
    assert second.budget_usage.retrieval_calls == 1
    assert second.budget_usage.embedding_calls == 0
    assert [record.stage.value for record in second.stage_records] == [
        "RETRIEVE",
        "CONTEXT_BUILD",
    ]
    assert [item[0] for item in emitter.events] == [
        "RETRIEVAL_STARTED",
        "RETRIEVAL_STAGE_COMPLETED",
        "RETRIEVAL_STAGE_COMPLETED",
        "RETRIEVAL_COMPLETED",
    ]
    assert emitter.events[0][1].retrieval_id == "cached-request"
    assert emitter.events[-1][1].retrieval_id == "cached-request"


@pytest.mark.asyncio
async def test_production_cache_isolates_user_generation_policy_and_rejects_bad_projection(
    real_redis_client,
) -> None:
    client, cleanup_keys = real_redis_client
    adapter = FakeRetrievalAdapter()
    cache = RagQueryCache(client, _CacheSettings())
    generation = ["generation-1"]
    service = CachedRetrievalExecutionService(
        RetrievalExecutionService(adapter),
        cache,
        SyncRedisBridge(asyncio.get_running_loop(), timeout_seconds=2.0),
        index_generation_provider=lambda: generation[0],
    )

    async def execute(request_id: str, domain: str, *, top_k: int = 4):
        invocation = _invocation(request_id, top_k=top_k)
        cleanup_keys.append(
            cache.key(
                authz_domain=domain,
                index_generation=generation[0],
                policy=service._policy_identity(invocation),
                query=invocation.original_query,
            )
        )
        return await asyncio.to_thread(
            service.execute, invocation, run_context=_context(domain)
        )

    await execute("a-origin", "user-a")
    calls_after_a = len(adapter.calls)
    await execute("a-hit", "user-a")
    assert len(adapter.calls) == calls_after_a

    await execute("b-origin", "user-b")
    assert len(adapter.calls) > calls_after_a
    calls_after_b = len(adapter.calls)

    generation[0] = "generation-2"
    await execute("g2-origin", "user-a")
    assert len(adapter.calls) > calls_after_b
    calls_after_g2 = len(adapter.calls)

    await execute("policy-origin", "user-a", top_k=5)
    assert len(adapter.calls) > calls_after_g2

    bad_invocation = _invocation("bad-version")
    bad_key = cache.key(
        authz_domain="bad-user",
        index_generation=generation[0],
        policy=service._policy_identity(bad_invocation),
        query=bad_invocation.original_query,
    )
    cleanup_keys.append(bad_key)
    unsupported_payload = {"schema_version": "cached-retrieval-result.v999"}
    await client.set(
        bad_key,
        json.dumps(
            {
                "schema_version": cache.schema_version,
                "payload_digest": stable_digest(unsupported_payload),
                "payload": unsupported_payload,
            }
        ),
        ex=30,
    )
    calls_before_bad = len(adapter.calls)
    await asyncio.to_thread(
        service.execute,
        bad_invocation,
        run_context=_context("bad-user"),
    )
    assert len(adapter.calls) > calls_before_bad


@pytest.mark.asyncio
async def test_production_cache_bypasses_internal_calls_and_redis_outage() -> None:
    unavailable = redis.from_url(
        "redis://127.0.0.1:6399/0",
        decode_responses=True,
        socket_connect_timeout=0.05,
        socket_timeout=0.05,
    )
    try:
        adapter = FakeRetrievalAdapter()
        service = CachedRetrievalExecutionService(
            RetrievalExecutionService(adapter),
            RagQueryCache(unavailable, _CacheSettings()),
            SyncRedisBridge(asyncio.get_running_loop(), timeout_seconds=0.5),
            index_generation_provider=lambda: "generation-1",
        )
        outage = await asyncio.to_thread(
            service.execute,
            _invocation("outage"),
            run_context=_context("user-a"),
        )
        calls_after_outage = len(adapter.calls)
        internal = await asyncio.to_thread(
            service.execute,
            _invocation("internal"),
            run_context=_context(None),
        )
        assert outage.final_chunks and internal.final_chunks
        assert len(adapter.calls) > calls_after_outage
    finally:
        await unavailable.aclose()


@pytest.mark.asyncio
async def test_cache_lookup_does_not_extend_run_deadline() -> None:
    class _SlowRedis:
        async def get(self, _key):
            await asyncio.sleep(0.1)
            return None

        async def set(self, *_args, **_kwargs):
            raise AssertionError("timed-out retrieval must not populate cache")

    adapter = FakeRetrievalAdapter()
    service = CachedRetrievalExecutionService(
        RetrievalExecutionService(adapter),
        RagQueryCache(_SlowRedis(), _CacheSettings()),
        SyncRedisBridge(asyncio.get_running_loop(), timeout_seconds=1.0),
        index_generation_provider=lambda: "generation-1",
    )
    invocation = RetrievalInvocation.create(
        "deadline query",
        collection_names=("kb",),
        top_k=4,
        rerank_top_k=2,
        requested_timeout_seconds=0.01,
        retrieval_id="deadline-request",
    )

    result = await asyncio.to_thread(
        service.execute,
        invocation,
        run_context=_context("user-a", timeout_seconds=0.01),
    )

    assert result.status is RetrievalExecutionStatus.TIMED_OUT
    assert adapter.calls == []
