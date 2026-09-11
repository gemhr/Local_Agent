"""Stage6-WP3 Redis cache and admission services.

Redis is deliberately not a source of business truth: cache operations fail open,
while admission fails closed when the atomic limiter cannot be consulted.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import redis.asyncio as redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


class RedisUnavailableError(RuntimeError):
    """Safe internal signal used only to project limiter failure as HTTP 503."""


def normalize_cache_query(query: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", query).strip())


def stable_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class RedisService:
    """Application-lifecycle owner of the redis-py asyncio client/pool."""

    def __init__(self, settings: Any) -> None:
        self.client = redis.from_url(
            settings.redis_url,
            decode_responses=True,
            max_connections=settings.redis_max_connections,
            socket_connect_timeout=settings.redis_connect_timeout_seconds,
            socket_timeout=settings.redis_socket_timeout_seconds,
        )

    async def close(self) -> None:
        await self.client.aclose()


class RagQueryCache:
    """Small cache-aside helper for safe JSON retrieval projections."""

    schema_version = 2

    def __init__(self, client: redis.Redis, settings: Any, *, observability: Any = None) -> None:
        self._client = client
        self._enabled = settings.rag_cache_enabled
        self._ttl = settings.rag_cache_ttl_seconds
        self._jitter = settings.rag_cache_ttl_jitter_seconds
        self._max_value_bytes = settings.rag_cache_max_value_bytes
        self._observability = observability

    @staticmethod
    def key(*, authz_domain: str, index_generation: str, policy: object, query: str) -> str:
        # None of the raw query is present in Redis keyspace.
        return "rag:v1:" + ":".join((
            hashlib.sha256(authz_domain.encode("utf-8")).hexdigest(),
            hashlib.sha256(index_generation.encode("utf-8")).hexdigest(),
            stable_digest(policy),
            hashlib.sha256(normalize_cache_query(query).encode("utf-8")).hexdigest(),
        ))

    async def get(self, key: str) -> dict[str, Any] | None:
        started_at = time.perf_counter()
        if not self._enabled:
            self._observe("bypass", started_at)
            return None
        try:
            raw = await self._client.get(key)
            if raw is None:
                self._observe("miss", started_at)
                return None
            envelope = json.loads(raw)
            if not isinstance(envelope, dict) or envelope.get("schema_version") != self.schema_version:
                await self._delete_bad(key)
                self._observe("miss", started_at)
                return None
            payload = envelope.get("payload")
            if (
                not isinstance(payload, dict)
                or envelope.get("payload_digest") != stable_digest(payload)
            ):
                await self._delete_bad(key)
                self._observe("miss", started_at)
                return None
            self._observe("hit", started_at)
            return payload
        except (RedisError, ValueError, TypeError, json.JSONDecodeError):
            self._log_error("get")
            self._observe("error", started_at)
            return None

    def _observe(self, outcome: str, started_at: float) -> None:
        try:
            if self._observability is not None:
                self._observability.observe_cache(
                    outcome, time.perf_counter() - started_at
                )
        except Exception:
            pass

    async def set(self, key: str, payload: dict[str, Any]) -> None:
        if not self._enabled or not payload:
            return  # no negative caching
        try:
            raw = json.dumps(
                {
                    "schema_version": self.schema_version,
                    "payload_digest": stable_digest(payload),
                    "payload": payload,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
            if len(raw.encode("utf-8")) > self._max_value_bytes:
                return
            ttl = max(1, self._ttl + random.randint(-self._jitter, self._jitter))
            await self._client.set(key, raw, ex=ttl)
        except (RedisError, TypeError, ValueError):
            self._log_error("set")
            return

    async def _delete_bad(self, key: str) -> None:
        try:
            await self._client.delete(key)
        except RedisError:
            pass

    async def get_or_load(self, key: str, origin: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        cached = await self.get(key)
        if cached is not None:
            return cached
        result = await origin()
        await self.set(key, result)
        return result

    @staticmethod
    def _log_error(operation: str) -> None:
        logger.info(
            "RAG cache operation failed",
            extra={
                "component": "retrieval_cache",
                "cache_outcome": "error",
                "cache_operation": operation,
            },
        )


_TOKEN_BUCKET_LUA = """
local now = redis.call('TIME')
local now_ms = now[1] * 1000 + math.floor(now[2] / 1000)
local capacity = tonumber(ARGV[1])
local refill_per_ms = tonumber(ARGV[2])
local state = redis.call('HMGET', KEYS[1], 'tokens', 'last_ms')
local tokens = tonumber(state[1]) or capacity
local last_ms = tonumber(state[2]) or now_ms
tokens = math.min(capacity, tokens + math.max(0, now_ms - last_ms) * refill_per_ms)
local allowed = tokens >= 1
if allowed then tokens = tokens - 1 end
local ttl_ms = math.max(1000, math.ceil(capacity / refill_per_ms))
redis.call('HMSET', KEYS[1], 'tokens', tokens, 'last_ms', now_ms)
redis.call('PEXPIRE', KEYS[1], ttl_ms)
local retry_ms = 0
if not allowed then retry_ms = math.ceil((1 - tokens) / refill_per_ms) end
return {allowed and 1 or 0, retry_ms}
"""


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int


class RedisTokenBucketRateLimiter:
    def __init__(self, client: redis.Redis, settings: Any, *, observability: Any = None) -> None:
        self._client = client
        self._enabled = settings.rate_limit_enabled
        self._capacity = settings.rate_limit_capacity
        self._refill_rate = settings.rate_limit_refill_rate
        self._observability = observability

    async def check(self, principal_domain: str) -> RateLimitDecision:
        started_at = time.perf_counter()
        if not self._enabled:
            self._observe("allowed", started_at)
            return RateLimitDecision(True, 0)
        key = "ratelimit:v1:" + hashlib.sha256(principal_domain.encode("utf-8")).hexdigest()
        try:
            result = await self._client.eval(
                _TOKEN_BUCKET_LUA, 1, key, self._capacity, self._refill_rate / 1000.0
            )
            allowed, retry_ms = int(result[0]) == 1, max(0, int(result[1]))
            self._observe("allowed" if allowed else "rejected", started_at)
            return RateLimitDecision(allowed, max(1, (retry_ms + 999) // 1000) if not allowed else 0)
        except (RedisError, OSError, ValueError, TypeError) as exc:
            self._observe("unavailable", started_at)
            logger.warning(
                "Redis rate limiter unavailable",
                extra={
                    "component": "rate_limiter",
                    "limiter_outcome": "unavailable",
                    "safe_error_code": "RATE_LIMIT_UNAVAILABLE",
                },
            )
            raise RedisUnavailableError from exc

    def _observe(self, outcome: str, started_at: float) -> None:
        try:
            if self._observability is not None:
                self._observability.observe_limiter(outcome, time.perf_counter() - started_at)
        except Exception:
            pass
