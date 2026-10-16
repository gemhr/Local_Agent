"""Context 业务事实的独立安全投影；不授予接受或执行权限。"""

from core.runtime.execution_aggregate import canonical_payload_digest


def build_context_evidence(image):
    if image.root.status not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
        raise ValueError("CONTEXT_EVIDENCE_NOT_READY")
    frame = image.loop_frames[0]
    if not frame.context_policy:
        raise ValueError("CONTEXT_EVIDENCE_UNAVAILABLE")
    gaps, snapshots, models, operations = [], [], [], []
    if not image.context_snapshots:
        gaps.append("CONTEXT_SNAPSHOT_MISSING")
    known = {s.snapshot_id: s for s in image.context_snapshots}
    parent = None
    for index, row in enumerate(image.context_snapshots, 1):
        p = row.payload
        if (row.version != index or p.get("parent_id") != parent
                or canonical_payload_digest(p) != row.snapshot_digest
                or p.get("messages_digest") != canonical_payload_digest(p.get("messages"))
                or p.get("source_digest") != canonical_payload_digest(p.get("source_manifest"))
                or p.get("mandatory_digest") != canonical_payload_digest(p.get("mandatory_manifest"))):
            gaps.append("CONTEXT_SOURCE_CHAIN_MISSING")
        validation = row.validation or {}
        checks = {key: validation.get(key) is True for key in ("source", "mandatory", "roles", "pairs", "budget")}
        if not all(checks.values()) or validation.get("version") != "context-validator.v1":
            gaps.append("CONTEXT_VALIDATION_MISSING")
        snapshots.append({"snapshot_id": row.snapshot_id, "version": row.version, "digest": row.snapshot_digest,
            "parent_id": p.get("parent_id"), "parent_digest": p.get("parent_digest"), "cursor": p.get("source_cursor"),
            "source_digest": p.get("source_digest"), "mandatory_digest": p.get("mandatory_digest"),
            "mandatory_checks": [{"item_digest": i["digest"], "preserved": checks["mandatory"]} for i in p.get("mandatory_manifest", [])],
            "kind": p.get("kind") if p.get("kind") in {"RAW", "COMPACTED"} else None,
            "validator": "context-validator.v1", "checks": checks, "semantic_preservation": "NOT_PROVEN",
            "created_generation": row.created_generation, "accepted_generation": row.accepted_generation, "fence": row.accepted_fence})
        parent = row.snapshot_id
    for iteration in image.loop_iterations:
        if iteration.context_snapshot_id not in known:
            gaps.append("CONTEXT_SNAPSHOT_MISSING")
    for row in image.models:
        assessment = row.request_assessment or {}
        snapshot = known.get(row.context_snapshot_id)
        if (not assessment or assessment.get("request_digest") != row.request_digest
                or not assessment.get("admissible") or not assessment.get("reliable") or assessment.get("mode") not in {"EXACT", "BOUNDED_ESTIMATE"}):
            gaps.append("CONTEXT_REQUEST_BINDING_MISSING")
        if row.purpose == "DECISION" and (snapshot is None or snapshot.snapshot_digest != row.context_snapshot_digest):
            gaps.append("CONTEXT_SNAPSHOT_MISSING")
        budget = image.root.budget_operations.get(f"model:{row.step_id}:{row.attempt_number}:{row.model_attempt_number}", {})
        if budget.get("state") != "SETTLED" or budget.get("source") not in {"ACTUAL", "ESTIMATED", "RECOVERY_CONSERVATIVE"} or not budget.get("actual"):
            gaps.append("CONTEXT_USAGE_MISSING")
        models.append({"attempt": row.attempt_number, "model_attempt": row.model_attempt_number,
            "purpose": row.purpose, "snapshot_id": row.context_snapshot_id, "snapshot_digest": row.context_snapshot_digest,
            "operation_id": row.compaction_operation_id, "request_digest": row.request_digest,
            "profile_digest": canonical_payload_digest([row.provider_kind, row.profile_identity]),
            "mode": assessment.get("mode"), "input_bound": assessment.get("input_bound"),
            "counting_strategy_digest": canonical_payload_digest([assessment.get("mode"), assessment.get("counting_version")]),
            "assessment_digest": canonical_payload_digest(assessment),
            "output_reserve": assessment.get("output_reserve"), "framing_bound": assessment.get("framing_bound"),
            "safety_margin": assessment.get("safety_margin"), "window": assessment.get("window"),
            "required_window": assessment.get("required_window"), "admissible": assessment.get("admissible"),
            "state": row.state, "usage_source": budget.get("source"),
            "reported_usage": {key: value for key, value in (row.usage_evidence or {}).items()
                if key in {"input_tokens", "output_tokens", "total_tokens", "cost_units", "retries"} and type(value) is int},
            "usage": {key: value for key, value in (budget.get("actual") or {}).items()
                if key in {"model_calls", "input_tokens", "output_tokens", "total_tokens", "cost_units", "retries"} and type(value) is int}})
    for row in image.context_operations:
        if (row.state in {"PREPARED", "RESULT_READY"} or not row.validation
                or row.validation_digest != canonical_payload_digest(row.validation)
                or (row.candidate is not None and row.candidate_digest != canonical_payload_digest(row.candidate))
                or (row.state == "ACCEPTED" and not row.result_digest)):
            gaps.append("CONTEXT_VALIDATION_MISSING")
        associated = [m for m in image.models if m.compaction_operation_id == row.operation_id and m.purpose == "COMPACTION"]
        if len(associated) != row.attempt_count or (row.state == "ACCEPTED" and row.accepted_snapshot_id not in known):
            gaps.append("CONTEXT_OPERATION_BINDING_MISSING")
        operations.append({"operation_id": row.operation_id, "parent_id": row.parent_id,
            "source_digest": row.prepared.get("source_digest"), "mandatory_digest": row.prepared.get("mandatory_digest"),
            "prepared_request_digest": row.prepared.get("request_digest"), "created_generation": row.created_generation,
            "target_iteration": row.target_iteration, "cursor": row.source_cursor, "state": row.state,
            "attempt_count": row.attempt_count, "candidate_digest": row.candidate_digest,
            "validation_digest": row.validation_digest, "result_digest": row.result_digest,
            "reason": row.reason if row.reason in {"VALID", "CONTEXT_VALIDATION_FAILED", "CONTEXT_REJECTED_STALE", "CONTEXT_COMPACTION_FAILED", "LOOP_CONTEXT_EXHAUSTED"} else None,
            "accepted_snapshot_id": row.accepted_snapshot_id})
    if not image.terminal_sequence:
        gaps.append("CONTEXT_TERMINAL_BINDING_MISSING")
    return {"schema_version": "context-evidence.v1", "policy_version": 1, "validator_version": "context-validator.v1",
        "algorithm_version": "context-compaction.v1", "run_id": image.root.run_id, "frame_id": frame.frame_id,
        "step_id": frame.step_id, "snapshots": snapshots, "models": models, "operations": operations,
        "terminal": {"status": image.root.status, "stop_reason": image.root.stop_reason, "sequence": image.terminal_sequence},
        "coverage": "PARTIAL" if gaps else "COMPLETE", "gaps": sorted(set(gaps)), "sealed": True}


async def capture_context_evidence(repository, run_id):
    return await repository.seal_context_evidence(run_id)
