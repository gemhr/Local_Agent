"""Stage9-WP5 capacity evidence primitives.

本模块只读取既有 Settings/Runtime owner 的值，不提供第二套运行时配置。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from math import ceil
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class CapacityBound:
    resource: str
    capacity: str
    owner: str
    saturation_symptom: str
    evidence: str = "INFERRED"


def percentile(values: list[float] | tuple[float, ...], percentile_rank: float) -> float:
    """Nearest-rank percentile，适用于无插值的可审计小样本结果。"""
    if not values:
        raise ValueError("percentile requires at least one value")
    if not 0 < percentile_rank <= 100:
        raise ValueError("percentile rank must be in (0, 100]")
    ordered = sorted(float(value) for value in values)
    index = max(0, ceil(len(ordered) * percentile_rank / 100) - 1)
    return ordered[index]


def capacity_model(settings: Any) -> tuple[CapacityBound, ...]:
    """从当前 Settings 和冻结的 Runtime 常量汇总容量边界。"""
    return (
        CapacityBound("API requests", "uvicorn worker/process mode (not set by Settings)", "FastAPI/uvicorn", "latency or process-level rejection"),
        CapacityBound("Active Runs", f"{settings.blocking_max_workers} supervised producers", "RunExecutionSupervisor", "API admission wait before Run binding"),
        CapacityBound("Model calls", str(settings.model_threads), "Model Runtime", "wait latency at model executor/thread capacity"),
        CapacityBound("Tool execution", "16 global; per-tool max declared by Tool", "ToolConcurrencyController", "semaphore queue or timeout"),
        CapacityBound("DB connections", f"{settings.db_pool_size} pool + {settings.db_max_overflow} overflow", "SQLAlchemy PostgreSQL engine", "checkout wait or pool timeout"),
        CapacityBound("HTTP connections", "100 max connections / 20 keep-alive", "httpx.AsyncClient", "HTTP pool wait"),
        CapacityBound("SSE subscriptions", "UNBOUNDED subscriptions; 250ms DB polling each", "PostgresClientEventFeed/API transport", "memory and polling DB pressure"),
        CapacityBound("Continuations", "DB claim is bounded per transaction; worker concurrency UNBOUNDED/NOT_INSTRUMENTED", "GenericContinuationService", "READY backlog or DB contention"),
        CapacityBound("Blocking work", f"{settings.blocking_max_workers} workers + {settings.blocking_max_pending_tasks} pending admission", "BlockingExecutor", "queue delay or admission rejection"),
        CapacityBound("Redis connections", str(settings.redis_max_connections), "Redis client/rate limiter", "connection wait or rate-limit unavailable"),
    )


def capacity_payload(settings: Any) -> list[dict[str, str]]:
    return [asdict(item) for item in capacity_model(settings)]


def evaluate_slo(spec: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate one benchmark JSON against one candidate SLO.

    缺字段或样本不足返回 NOT_ENOUGH_EVIDENCE；只有实际违反目标才 FAIL。
    """
    required_profile = spec.get("required_evidence_profile")
    observed_profile = result.get("evidence_profile")
    if required_profile is not None and observed_profile != required_profile:
        return {
            "status": "NOT_ENOUGH_EVIDENCE",
            "reason": f"evidence profile {observed_profile!r} != required {required_profile!r}",
        }
    required_scenario = spec.get("required_scenario", spec.get("scenario"))
    observed_scenario = result.get("scenario")
    if required_scenario is not None and observed_scenario != required_scenario:
        return {
            "status": "NOT_ENOUGH_EVIDENCE",
            "reason": f"scenario {observed_scenario!r} != required {required_scenario!r}",
        }
    required = int(spec.get("minimum_samples", 1))
    samples = int(result.get("attempted", 0))
    if samples < required:
        return {"status": "NOT_ENOUGH_EVIDENCE", "reason": f"samples {samples} < minimum {required}"}
    metric = str(spec["metric"])
    objective = str(spec.get("objective", "at_most"))
    threshold = float(spec["threshold"])
    if metric == "success_rate":
        if "succeeded" not in result:
            return {"status": "NOT_ENOUGH_EVIDENCE", "reason": "missing metric success_rate"}
        observed = float(result["succeeded"]) / samples if samples else 0.0
    elif metric.startswith("latency_ms."):
        key = metric.split(".", 1)[1]
        latency = result.get("latency_ms")
        if not isinstance(latency, Mapping) or key not in latency:
            return {"status": "NOT_ENOUGH_EVIDENCE", "reason": f"missing metric {metric}"}
        observed = float(latency[key])
    elif metric == "error_rate":
        if "failed" not in result:
            return {"status": "NOT_ENOUGH_EVIDENCE", "reason": "missing metric error_rate"}
        observed = float(result["failed"]) / samples if samples else 1.0
    elif metric.startswith("correctness."):
        key = metric.split(".", 1)[1]
        correctness = result.get("correctness", {})
        if not isinstance(correctness, Mapping) or key not in correctness:
            return {"status": "NOT_ENOUGH_EVIDENCE", "reason": f"missing metric {metric}"}
        observed = float(correctness[key])
    else:
        return {"status": "NOT_ENOUGH_EVIDENCE", "reason": f"unsupported metric {metric}"}
    passed = observed >= threshold if objective == "at_least" else observed <= threshold
    return {"status": "PASS" if passed else "FAIL", "observed": observed, "threshold": threshold, "metric": metric}


__all__ = ["CapacityBound", "capacity_model", "capacity_payload", "evaluate_slo", "percentile"]
