"""可复现的 PLATFORM_ONLY HTTP capacity harness。"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import httpx

from core.capacity import percentile


def _git_identity() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:
        return "UNKNOWN"


async def _one(client: httpx.AsyncClient, url: str, method: str, payload: dict | None, timeout: float) -> tuple[float, bool, int | None, str | None]:
    started = time.perf_counter()
    try:
        response = await client.request(method, url, json=payload, timeout=timeout)
        elapsed = (time.perf_counter() - started) * 1000
        return elapsed, 200 <= response.status_code < 300, response.status_code, None
    except httpx.TimeoutException:
        return (time.perf_counter() - started) * 1000, False, None, "timeout"
    except httpx.HTTPError as exc:
        return (time.perf_counter() - started) * 1000, False, None, type(exc).__name__


async def run(args: argparse.Namespace) -> dict:
    method, path, payload = {
        "api_baseline": ("GET", "/health", None),
        "synthetic_start": ("POST", "/api/v1/chat", {"agent_id": "capacity-test", "query": "deterministic capacity probe"}),
    }[args.scenario]
    url = args.base_url.rstrip("/") + path
    started_at = datetime.now(UTC).isoformat()
    started = time.perf_counter()
    results = []
    # Local benchmark must not inherit a corporate proxy for loopback traffic.
    async with httpx.AsyncClient(trust_env=False) as client:
        semaphore = asyncio.Semaphore(args.concurrency)
        async def worker() -> None:
            async with semaphore:
                results.append(await _one(client, url, method, payload, args.timeout))
        await asyncio.gather(*(worker() for _ in range(args.requests)))
    elapsed = time.perf_counter() - started
    latencies = [item[0] for item in results]
    succeeded = sum(1 for item in results if item[1])
    status = {}
    errors = {}
    for _, ok, code, error in results:
        key = str(code) if code is not None else "transport"
        status[key] = status.get(key, 0) + 1
        if error:
            errors[error] = errors.get(error, 0) + 1
    return {
        "scenario": args.scenario, "evidence_profile": "SYNTHETIC",
        "started_at": started_at, "duration_seconds": elapsed,
        "concurrency": args.concurrency, "attempted": len(results),
        "succeeded": succeeded, "failed": len(results) - succeeded,
        "throughput_rps": len(results) / elapsed if elapsed else 0.0,
        "latency_ms": {"p50": percentile(latencies, 50), "p95": percentile(latencies, 95), "p99": percentile(latencies, 99), "max": max(latencies)},
        "http_status_distribution": status, "error_type_counts": errors,
        "environment": {"os": platform.platform(), "python": sys.version.split()[0], "process_count": 1, "worker_mode": "single harness client", "benchmark_mode": "deterministic/local platform endpoint", "git": _git_identity()},
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--scenario", choices=("api_baseline", "synthetic_start"), required=True)
    parser.add_argument("--concurrency", type=int, required=True)
    parser.add_argument("--requests", type=int, required=True)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--output-path", type=Path, required=True)
    args = parser.parse_args()
    if args.concurrency <= 0 or args.requests <= 0 or args.timeout <= 0:
        parser.error("concurrency, requests and timeout must be positive")
    result = asyncio.run(run(args))
    args.output_path.parent.mkdir(parents=True, exist_ok=True)
    args.output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
