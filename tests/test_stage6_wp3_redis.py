"""Stage6-WP3 real Redis tests; never flush a Redis database."""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest
import pytest_asyncio
import redis.asyncio as redis

from core.redis_service import RagQueryCache, RedisTokenBucketRateLimiter, RedisUnavailableError


class _Settings:
    rag_cache_enabled = True
    rag_cache_ttl_seconds = 2
    rag_cache_ttl_jitter_seconds = 0
    rag_cache_max_value_bytes = 1024
    rate_limit_enabled = True
    rate_limit_capacity = 3
    rate_limit_refill_rate = 10.0


@pytest_asyncio.fixture
async def real_redis():
    client = redis.from_url(os.getenv("LOCAL_AGENT_TEST_REDIS_URL", "redis://127.0.0.1:6380/0"), decode_responses=True)
    namespace = f"test:stage6wp3:{uuid.uuid4().hex}:"
    try:
        await client.ping()
    except Exception as exc:
        await client.aclose()
        pytest.fail(f"Redis test instance unavailable: {type(exc).__name__}")
    try:
        yield client, namespace
    finally:
        keys = [key async for key in client.scan_iter(match=f"{namespace}*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()


@pytest.mark.asyncio
async def test_real_redis_cache_aside_isolated_ttl_and_corruption(real_redis) -> None:
    client, namespace = real_redis
    cache = RagQueryCache(client, _Settings())
    policy = {"strategy": "HYBRID_RRF", "top_k": 3, "rrf_k": 60}
    key_a = namespace + cache.key(authz_domain="user-a", index_generation="g1", policy=policy, query="  Redis\t缓存 ")
    key_b = namespace + cache.key(authz_domain="user-b", index_generation="g1", policy=policy, query="Redis 缓存")
    calls = 0

    async def origin():
        nonlocal calls
        calls += 1
        return {"chunks": [{"text": "safe"}]}

    assert await cache.get_or_load(key_a, origin) == {"chunks": [{"text": "safe"}]}
    assert await cache.get_or_load(key_a, origin) == {"chunks": [{"text": "safe"}]}
    assert calls == 1
    assert await cache.get(key_b) is None
    valid_json_corruption = json.loads(await client.get(key_a))
    valid_json_corruption["payload"]["chunks"][0]["text"] = "tampered"
    await client.set(key_a, json.dumps(valid_json_corruption), ex=2)
    assert await cache.get(key_a) is None
    assert await cache.get_or_load(key_a, origin) == {"chunks": [{"text": "safe"}]}
    assert calls == 2
    await client.set(key_a, "not-json", ex=2)
    assert await cache.get(key_a) is None
    assert await cache.get_or_load(key_a, origin) == {"chunks": [{"text": "safe"}]}
    assert calls == 3
    await asyncio.sleep(2.1)
    assert await cache.get_or_load(key_a, origin) == {"chunks": [{"text": "safe"}]}
    assert calls == 4


@pytest.mark.asyncio
async def test_real_redis_token_bucket_is_atomic_across_instances(real_redis) -> None:
    client, namespace = real_redis
    settings = _Settings()
    settings.rate_limit_capacity = 5
    settings.rate_limit_refill_rate = 0.1
    one = RedisTokenBucketRateLimiter(client, settings)
    two = RedisTokenBucketRateLimiter(client, settings)
    # The test namespace cannot be injected into the production key, so use a
    # unique principal identity; only the derived ratelimit key is removed here.
    principal = namespace + "principal"
    decisions = await asyncio.gather(*[ (one if index % 2 else two).check(principal) for index in range(20) ])
    assert sum(item.allowed for item in decisions) == 5
    rejected = [item for item in decisions if not item.allowed]
    assert rejected and all(item.retry_after_seconds >= 1 for item in rejected)
    rate_key = "ratelimit:v1:" + __import__("hashlib").sha256(principal.encode()).hexdigest()
    await client.delete(rate_key)


@pytest.mark.asyncio
async def test_cache_fails_open_and_limiter_fails_closed_when_redis_is_unavailable() -> None:
    unavailable = redis.from_url("redis://127.0.0.1:6399/0", decode_responses=True, socket_connect_timeout=0.05, socket_timeout=0.05)
    try:
        cache = RagQueryCache(unavailable, _Settings())
        calls = 0

        async def origin():
            nonlocal calls
            calls += 1
            return {"chunks": [{"text": "origin"}]}

        assert await cache.get_or_load("test:stage6wp3:outage", origin) == {"chunks": [{"text": "origin"}]}
        assert calls == 1
        with pytest.raises(RedisUnavailableError):
            await RedisTokenBucketRateLimiter(unavailable, _Settings()).check("outage-principal")
    finally:
        await unavailable.aclose()
