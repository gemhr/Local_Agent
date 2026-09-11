"""WP6 real Redis observability evidence；仅操作测试生成的 key。"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import os
import uuid

import pytest
import redis.asyncio as redis

from core.observability import ObservabilityService
from core.redis_service import RagQueryCache, RedisTokenBucketRateLimiter
from core.settings import Settings


@pytest.mark.asyncio
async def test_real_redis_cache_and_limiter_emit_metrics() -> None:
    client = redis.from_url(
        os.getenv("LOCAL_AGENT_TEST_REDIS_URL", "redis://127.0.0.1:6380/0"),
        decode_responses=True,
    )
    cache_key = f"test:stage6wp6:cache:{uuid.uuid4().hex}"
    principal = f"test-stage6wp6-{uuid.uuid4().hex}"
    settings = replace(
        Settings.load(),
        rate_limit_capacity=1,
        rate_limit_refill_rate=0.0001,
    )
    observability = ObservabilityService(settings, process_role="api")
    cache = RagQueryCache(client, settings, observability=observability)
    limiter = RedisTokenBucketRateLimiter(client, settings, observability=observability)
    try:
        assert await cache.get(cache_key) is None
        await cache.set(cache_key, {"safe": "projection"})
        assert await cache.get(cache_key) == {"safe": "projection"}
        assert (await limiter.check(principal)).allowed
        assert not (await limiter.check(principal)).allowed
        rendered = observability.render_metrics().body.decode()
        assert 'localagent_rag_cache_requests_total{outcome="miss"} 1.0' in rendered
        assert 'localagent_rag_cache_requests_total{outcome="hit"} 1.0' in rendered
        assert 'localagent_rate_limit_decisions_total{outcome="allowed"} 1.0' in rendered
        assert 'localagent_rate_limit_decisions_total{outcome="rejected"} 1.0' in rendered
        assert principal not in rendered
        assert cache_key not in rendered
    finally:
        await client.delete(cache_key)
        # limiter key 由固定 digest 构成，只删除本测试 principal 对应 key。
        await client.delete(
            "ratelimit:v1:" + hashlib.sha256(principal.encode("utf-8")).hexdigest()
        )
        await client.aclose()
