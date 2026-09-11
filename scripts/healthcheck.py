#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""API / publisher / worker 的 bounded read-only readiness CLI。"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from core.health_checks import ComponentReadinessService
from core.persistence import Database, DatabaseConfig
from core.settings import Settings


async def _probe_api_readiness(settings: Settings) -> tuple[bool, dict[str, object]]:
    """读取真实 API `/readyz`，以同一 Runtime admission fact 判定 readiness。"""

    started_at = time.perf_counter()

    def request_status() -> int | None:
        request = Request(
            f"{settings.api_base_url.rstrip('/')}/readyz",
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=settings.health_check_timeout_seconds) as response:
                return int(response.status)
        except HTTPError as exc:
            return int(exc.code)
        except Exception:
            return None

    try:
        status_code = await asyncio.wait_for(
            asyncio.to_thread(request_status),
            timeout=settings.health_check_timeout_seconds,
        )
    except TimeoutError:
        status_code = None
    ready = status_code == 200
    reason_code = "ok" if ready else "not_ready" if status_code == 503 else "unavailable"
    return ready, {
        "status": "ready" if ready else "not_ready",
        "components": {
            "api": {
                "status": "healthy" if ready else "unavailable",
                "reason_code": reason_code,
                "latency_ms": int((time.perf_counter() - started_at) * 1000),
            }
        },
    }


async def _run(component: str) -> int:
    database = None
    payload: dict[str, object] = {
        "status": "not_ready",
        "components": {
            "healthcheck": {
                "status": "unavailable",
                "reason_code": "initialization_failed",
                "latency_ms": 0,
            }
        },
    }
    exit_code = 1
    try:
        settings = Settings.load()
        if component == "api":
            ready, payload = await _probe_api_readiness(settings)
            exit_code = 0 if ready else 1
        else:
            database = Database(DatabaseConfig.from_settings(settings))
            readiness = ComponentReadinessService(database, settings)
            result = (
                await readiness.check_publisher()
                if component == "publisher"
                else await readiness.check_worker()
            )
            payload = result.to_safe_dict()
            exit_code = 0 if result.ready else 1
    except Exception:
        # CLI 只能输出固定安全投影，禁止 traceback、DSN 或 credential 泄漏。
        exit_code = 1
    finally:
        if database is not None:
            await database.dispose()
    print(json.dumps(payload, separators=(",", ":")))
    return exit_code


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--component", choices=("api", "publisher", "worker"), required=True)
    raise SystemExit(asyncio.run(_run(parser.parse_args().component)))


if __name__ == "__main__":
    main()
