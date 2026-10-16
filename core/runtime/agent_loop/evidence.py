"""从持久 Loop aggregate 生成有界安全证据；不拥有执行或恢复权限。"""

from __future__ import annotations

import hashlib
import json
import re

from core.runtime.agent_loop.contracts import LOOP_POLICY

SCHEMA = "loop-evidence.v1"
MAX_EVIDENCE_BYTES = 256 * 1024
BUDGET_DIMENSIONS = ("step_starts", "model_calls", "remote_model_calls", "tool_calls", "input_tokens",
    "output_tokens", "total_tokens", "cost_units", "retries", "retrieval_calls", "embedding_calls",
    "vector_queries", "keyword_queries", "bm25_queries", "rrf_fusions", "document_reads", "context_chars")


def evidence_digest(body: dict) -> str:
    # 本协议仅输出整数、字符串和布尔值，与 Go catalog-json-v1 一致。
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, allow_nan=False,
                                    sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def build_loop_evidence(image) -> dict:
    """仅投影 durable 事实；缺少证明时显式 PARTIAL，绝不推测成功。"""
    root = image.root
    if root.status not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
        raise ValueError("LOOP_EVIDENCE_NOT_READY")
    if root.execution_policy != LOOP_POLICY or len(image.loop_frames) != 1:
        raise ValueError("LOOP_EVIDENCE_UNAVAILABLE")
    frame = image.loop_frames[0]
    gaps = []
    iterations = []
    tools = {row.invocation_id: row for row in image.tool_invocations}
    models = {(row.attempt_number, row.model_attempt_number): row for row in image.models}
    for row in sorted(image.loop_iterations, key=lambda row: row.iteration_no):
        if row.frame_id != frame.frame_id:
            gaps.append("LOOP_FRAME_BINDING_MISSING")
            continue
        model = models.get((row.accepted_model_attempt, row.accepted_model_attempt_number))
        aborted = row.decision_id is None and row.state == "OPEN"
        if row.decision_id and (model is None or model.state != "COMPLETED"
                               or model.purpose != "DECISION"
                               or model.loop_frame_id != frame.frame_id
                               or model.loop_iteration_no != row.iteration_no):
            gaps.append("LOOP_MODEL_BINDING_MISSING")
        item = {"iteration": row.iteration_no, "decision_id": row.decision_id,
                "kind": row.decision_kind, "model_attempt_ref": None if model is None else
                f"model-attempt://{root.run_id}/{frame.step_id}/{model.attempt_number}/{model.model_attempt_number}",
                "accepted_generation": row.accepted_generation,
                "resume_generation": row.observed_generation if row.observed_generation is not None else row.accepted_generation,
                "action": None, "aborted": aborted}
        if row.decision_kind == "TOOL_CALL":
            recipe = row.action_recipe or {}
            tool = tools.get(row.tool_invocation_id)
            result = (tool.committed_result or {}) if tool is not None else {}
            side_effecting = not recipe.get("read_only", False)
            # 无真实外部 receipt/业务计数证明时不能声称 VERIFIED。
            proof = "INDETERMINATE" if side_effecting else "NOT_APPLICABLE"
            receipt = result.get("provider_receipt")
            if (side_effecting and tool is not None and tool.state == "COMMITTED"
                    and isinstance(receipt, dict)
                    and receipt.get("side_effect_count") == 1
                    and receipt.get("business_operation_key_digest") == tool.idempotency_key_digest
                    and isinstance(receipt.get("provider_receipt_digest"), str)
                    and re.fullmatch(r"[0-9a-f]{64}", receipt["provider_receipt_digest"])
                    and result.get("attempt_id")):
                proof = "VERIFIED"
            if tool is None or tool.state != "COMMITTED" or row.observation_digest is None:
                gaps.append("LOOP_OBSERVATION_MISSING")
            if proof == "INDETERMINATE":
                gaps.append("LOOP_SIDE_EFFECT_INDETERMINATE")
            item["action"] = {"action_id": row.action_id, "invocation_id": row.tool_invocation_id,
                "idempotency_key_digest": None if tool is None else tool.idempotency_key_digest,
                "tool_identity": None if tool is None else tool.tool_name,
                "arguments_digest": None if tool is None else tool.arguments_digest,
                "side_effecting": side_effecting,
                "tool_attempt_ref": result.get("attempt_id"),
                "result_ref": None if tool is None else f"tool-result://{tool.invocation_id}",
                "result_digest": None if tool is None else tool.committed_result_digest,
                "observation_source": row.observation_source_digest,
                "observation_digest": row.observation_digest,
                "observation_cursor": row.observation_cursor,
                "side_effect_proof": proof,
                "provider_receipt_digest": receipt.get("provider_receipt_digest") if proof == "VERIFIED" else None}
        elif not row.decision_id and not aborted:
            gaps.append("LOOP_DECISION_MISSING")
        iterations.append(item)
    if [item["iteration"] for item in iterations] != list(range(1, frame.current_iteration + 1)):
        gaps.append("LOOP_ITERATION_PREFIX_MISSING")
    receipt = frame.final_delivery_receipt
    final = None
    if receipt:
        final = {"decision_id": frame.result_decision_id,
                 "delivery_id": receipt.get("event_id"), "cursor": receipt.get("cursor"),
                 "content_digest": receipt.get("content_digest")}
        if not all(final.values()):
            gaps.append("LOOP_FINAL_RECEIPT_MISSING")
    elif root.status == "SUCCEEDED":
        gaps.append("LOOP_FINAL_RECEIPT_MISSING")
    if any(row.state in {"STARTED", "UNKNOWN"} for row in image.tool_invocations):
        gaps.append("LOOP_UNCONFIRMED_WORK")
    correlation = root.resume_input.get("agent_loop", {}).get("correlation", {})
    correlation = {key: correlation.get(key) for key in ("evaluation_run_id", "attempt_id", "request_id")}
    if not all(correlation.values()):
        gaps.append("LOOP_CORRELATION_MISSING")
    consumed = root.budget_consumed
    policy = frame.loop_policy
    details = getattr(frame, "stop_details", {}) or {}
    stopped_dimension = details.get("budget_dimension")
    requested, remaining = details.get("budget_requested"), details.get("budget_remaining")
    if stopped_dimension and (type(requested) is not int or type(remaining) is not int):
        gaps.append("LOOP_BUDGET_STOP_EVIDENCE_UNAVAILABLE")
        requested = remaining = None
    if frame.stop_code == "LOOP_BUDGET_EXHAUSTED" and not stopped_dimension:
        gaps.append("LOOP_BUDGET_STOP_EVIDENCE_UNAVAILABLE")
    profiles = getattr(frame, "pinned_config", {}).get("profiles", [])
    cost_known = bool(profiles) and all(profile.get("cost_profile") is not None for profile in profiles)
    consumed_usage = {key: consumed.get(key, 0) for key in BUDGET_DIMENSIONS}
    if not cost_known:
        consumed_usage["cost_units"] = None
    maximum = {key: getattr(root, "budget_totals", {}).get("max_" + key) for key in BUDGET_DIMENSIONS}
    terminal_sequence = getattr(image, "terminal_sequence", None)
    if terminal_sequence is None:
        gaps.append("LOOP_TERMINAL_BINDING_MISSING")
    no_progress_count = 0
    rows = sorted(image.loop_iterations, key=lambda row: row.iteration_no)
    if rows and rows[-1].observation_digest and (rows[-1].action_recipe or {}).get("read_only"):
        last = rows[-1]
        identity = (last.action_recipe.get("tool_name"), last.action_recipe.get("arguments_digest"), last.observation_digest)
        for row in reversed(rows):
            recipe = row.action_recipe or {}
            if recipe.get("read_only") and (recipe.get("tool_name"), recipe.get("arguments_digest"), row.observation_digest) == identity:
                no_progress_count += 1
            else:
                break
    body = {"schema_version": SCHEMA, "producer_version": "localagent-loop-producer.v1",
        "policy_version": LOOP_POLICY, "algorithm_version": "loop-integrity.v1",
        "evidence_id": f"loop-evidence://{root.run_id}", "frame_id": frame.frame_id,
        "run_id": root.run_id, "step_id": frame.step_id,
        "goal_digest": frame.context_seed["goal_digest"],
        "config_digest": frame.config_digest, "correlation": correlation,
        "iteration_count": frame.current_iteration, "iterations": iterations,
        "budget": {"model_calls": consumed.get("model_calls", 0), "tool_calls": consumed.get("tool_calls", 0),
                   "iterations": frame.current_iteration, "max_iterations": policy["max_iterations"],
                   "max_model_calls": policy["max_model_calls"], "max_tool_calls": policy["max_tool_calls"],
                   "stopped_dimension": stopped_dimension, "stop_requested": requested, "stop_remaining": remaining,
                   "consumed_usage": consumed_usage, "max_usage": maximum,
                   "cost_basis": "PINNED_BUDGET_UNITS" if cost_known else "UNKNOWN"},
        "stop": {"code": frame.stop_code or ("FINAL" if root.status == "SUCCEEDED" else root.stop_reason),
                 "no_progress_count": no_progress_count, "no_progress_threshold": 2},
        "final": final, "root": {"status": root.status, "stop_reason": root.stop_reason,
                                   "terminal_sequence": terminal_sequence},
        "coverage": "PARTIAL" if gaps else "COMPLETE", "sealed": True, "gaps": sorted(set(gaps))}
    def safe_projection(value):
        if isinstance(value, dict):
            return {key: safe_projection(item) for key, item in value.items()}
        if isinstance(value, list):
            return [safe_projection(item) for item in value]
        if isinstance(value, str):
            if len(value) > 512 or not re.fullmatch(r"[A-Za-z0-9._:/-]+", value):
                gaps.append("LOOP_SAFE_IDENTITY_UNAVAILABLE")
                return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()
        elif value is not None and type(value) not in {int, bool}:
            raise ValueError("LOOP_EVIDENCE_SAFE_SCHEMA_INVALID")
        return value
    body = safe_projection(body)
    body.update(coverage="PARTIAL" if gaps else "COMPLETE", gaps=sorted(set(gaps)))
    if len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_EVIDENCE_BYTES:
        body.update(iterations=[], coverage="PARTIAL", gaps=["LOOP_EVIDENCE_SIZE_LIMIT_EXCEEDED"])
    return body


async def capture_loop_evidence(repository, run_id: str) -> dict:
    image = await repository.read_loop_evidence_source(run_id)
    if image is None:
        raise ValueError("LOOP_EVIDENCE_UNAVAILABLE")
    if len(image.loop_frames) == 1 and image.loop_frames[0].sealed_evidence is not None:
        return image.loop_frames[0].sealed_evidence
    body = build_loop_evidence(image)
    return await repository.seal_loop_evidence(run_id, body, evidence_digest(body))
