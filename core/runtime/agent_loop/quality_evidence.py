"""真实持久事实的质量投影；缺少实际使用证明时保留 PARTIAL。"""
from __future__ import annotations
import re

from .quality_contract import (BUNDLE_SCHEMA, OBSERVATIONS_SCHEMA, PRODUCER, CAPTURE_POLICY,
    COMPONENT_LIMIT, OBSERVATIONS_LIMIT, BUNDLE_LIMIT, canonical_bytes, catalog_digest)
from .quality_capture import scan_facts, parameter_projection
from core.runtime.execution_aggregate import canonical_payload_digest as internal_digest
from core.runtime.tool_contract import canonical_json_digest


def _ref(value):
    return internal_digest(value) if value is not None else None


def component(kind, schema, body=None, reason=None):
    if body is not None and len(canonical_bytes(body)) > COMPONENT_LIMIT:
        body, reason = None, "EVIDENCE_LIMIT"
    return {"kind": kind, "schema_version": schema,
        "availability": "AVAILABLE" if body is not None else "NOT_APPLICABLE" if reason in {"NOT_APPLICABLE", "UNSUPPORTED_EXECUTION_POLICY"} else "UNAVAILABLE",
        "body": body, "body_digest": catalog_digest(body) if body is not None else None,
        "capture_reason": reason}


def observations(image, actions, delegations, binding, receipt):
    gaps, tools, models, facts, contexts, children = set(), [], [], [], [], []
    profile = receipt.capture_profile
    frames = {r.frame_id: r for r in image.loop_frames}
    snapshots = {r.snapshot_id: r for r in image.context_snapshots}
    invocations = {t.invocation_id: t for t in image.tool_invocations}
    actions_by_iteration = {}
    for action in actions:
        actions_by_iteration.setdefault((action.frame_id, action.iteration_no), []).append(action)
    for iteration in sorted(image.loop_iterations, key=lambda i: (i.frame_id, i.iteration_no)):
        recipes = [(a.ordinal, a.action_id, a.protocol_call_id, a.invocation_id, a.recipe, a.not_executed)
                   for a in actions_by_iteration.get((iteration.frame_id, iteration.iteration_no), [])]
        if iteration.decision_kind == "TOOL_CALL":
            call = (iteration.assistant_record or {}).get("tool_calls", [{}])[0]
            recipes = [(0, iteration.action_id, call.get("id"), iteration.tool_invocation_id, iteration.action_recipe, None)]
        for ordinal, action_id, call_id, invocation_id, recipe, skipped in recipes:
            recipe = recipe or {}
            tool = invocations.get(invocation_id)
            name = recipe.get("tool_name")
            alias = profile["tool_aliases"].get(name)
            if alias is None:
                gaps.add("TOOL_ALIAS_UNSUPPORTED")
            arguments = recipe.get("arguments")
            argument_digest, parameters = parameter_projection(arguments, profile["parameter_paths"].get(name, []))
            result = tool.committed_result if tool is not None else None
            state = "NOT_EXECUTED" if skipped else tool.state if tool is not None else "UNKNOWN"
            if state not in {"COMMITTED", "NOT_EXECUTED"}:
                gaps.add("TOOL_OUTCOME_UNCONFIRMED")
            if tool is not None and (tool.arguments_digest != recipe.get("arguments_digest")
                    or result is not None and canonical_json_digest(result) != tool.committed_result_digest):
                gaps.add("TOOL_SOURCE_INVALID")
            source_ref = "tool:" + _ref(invocation_id or action_id)
            extracted, scanned = scan_facts(result, profile, source_ref)
            facts.extend(extracted)
            proof = (result or {}).get("provider_receipt")
            external = (not recipe.get("read_only") and tool is not None and tool.state == "COMMITTED"
                and isinstance(proof, dict) and proof.get("side_effect_count") == 1
                and proof.get("business_operation_key_digest") == tool.idempotency_key_digest
                and isinstance(proof.get("provider_receipt_digest"), str)
                and re.fullmatch(r"[0-9a-f]{64}", proof["provider_receipt_digest"]) is not None
                and bool((result or {}).get("attempt_id")))
            if not recipe.get("read_only") and state == "COMMITTED" and not external:
                gaps.add("EXTERNAL_EFFECT_UNPROVEN")
            tools.append({"source_ref": source_ref, "frame_ref": _ref(iteration.frame_id),
                "iteration": iteration.iteration_no, "ordinal": ordinal, "action_ref": _ref(action_id),
                "call_ref": _ref(call_id), "invocation_ref": _ref(invocation_id), "tool_alias": alias,
                "arguments_digest": argument_digest, "parameters": parameters,
                "read_only": recipe.get("read_only") is True, "resource_key_digest": tool.resource_key_digest if tool else None,
                "state": state, "business_outcome": (result or {}).get("outcome") if (result or {}).get("outcome") in {"SUCCESS", "FAILED", "REFUSED", "TIMEOUT", "CANCELLED"} else None,
                "result_digest": tool.committed_result_digest if tool else None,
                "not_executed_digest": _ref(skipped), "external_effect_proof": "VERIFIED" if external else "NOT_APPLICABLE" if recipe.get("read_only") else "INDETERMINATE",
                "fact_scan_complete": scanned})
            tools[-1]["observation_digest"] = iteration.observation_digest
            import hashlib
            output_content = (result or {}).get("output", {}).get("content")
            tools[-1]["content_digest"] = hashlib.sha256(output_content.encode("utf-8")).hexdigest() if isinstance(output_content, str) else None
            tools[-1]["execution_status"] = (result or {}).get("status") if (result or {}).get("status") in {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"} else None
            specification = next((t.get("spec") for t in frames[iteration.frame_id].pinned_config.get("tools", [])
                if t.get("spec", {}).get("tool_name") == name or t.get("schema", {}).get("function", {}).get("name") == name), None)
            tools[-1]["spec_digest"] = _ref(specification)
    for model in image.models:
        key = f"model:{model.step_id}:{model.attempt_number}:{model.model_attempt_number}"
        capture = receipt.evaluation_capture.get(key)
        if capture is None or capture.get("request_digest") != model.request_digest:
            capture = None
            gaps.add("ACTUAL_REQUEST_CAPTURE_MISSING")
        if model.state in {"STARTED", "UNKNOWN"}:
            gaps.add("MODEL_OUTCOME_UNCONFIRMED")
        snapshot = snapshots.get(model.context_snapshot_id)
        if snapshot and capture and capture["messages_digest"] != snapshot.payload.get("messages_digest"):
            gaps.add("ACTUAL_SNAPSHOT_BINDING_INVALID")
        used = []
        if capture:
            facts.extend(capture["facts"])
            if not capture["scan_complete"]:
                gaps.add("FACT_SCAN_INCOMPLETE")
            # 只从原 Snapshot 的来源清单关联，不能按 iteration 顺序推测消费。
            if snapshot:
                used = [{"source_ref": _ref(s.get("ref")), "source_digest": s.get("observation_digest") or s.get("source_digest"),
                         "representation": snapshot.payload.get("kind")} for s in snapshot.payload.get("source_manifest", [])]
                if snapshot.payload.get("kind") == "COMPACTED" and model.purpose == "DECISION":
                    facts.extend({**f, "source_ref": "compacted-request:" + model.request_digest} for f in capture["facts"])
            elif model.purpose == "DECISION" and model.loop_frame_id in frames:
                from .context import LoopContextInputV1
                frame = frames[model.loop_frame_id]
                for observed in image.loop_iterations:
                    if observed.frame_id != frame.frame_id or observed.observation is None:
                        continue
                    pair, _ = LoopContextInputV1({"messages": [], "protocol_mode": frame.context_seed["protocol_mode"]},
                        (observed,), (observed,), 1).build()
                    group = [internal_digest(message) for message in pair]
                    actual = capture["message_digests"]
                    if group and any(actual[start:start+len(group)] == group for start in range(len(actual)-len(group)+1)):
                        used.append({"source_ref": _ref(f"source:{observed.iteration_no}"),
                            "source_digest": observed.observation_digest, "representation": "RAW"})
        accepted = next((i for i in image.loop_iterations if i.frame_id == model.loop_frame_id
            and (i.accepted_model_attempt, i.accepted_model_attempt_number) == (model.attempt_number, model.model_attempt_number)), None)
        models.append({"source_ref": "model:" + _ref([model.run_id, model.step_id, model.attempt_number, model.model_attempt_number]),
            "frame_ref": _ref(model.loop_frame_id), "iteration": model.loop_iteration_no, "purpose": model.purpose,
            "state": model.state, "request_digest": model.request_digest, "snapshot_ref": _ref(model.context_snapshot_id),
            "messages_digest": capture["messages_digest"] if capture else None,
            "fact_scan_complete": capture["scan_complete"] if capture else False,
            "request_capture_verified": capture is not None, "sources": used,
            "root_frame": model.loop_frame_id in frames and frames[model.loop_frame_id].frame_role == "ROOT",
            "accepted_kind": accepted.decision_kind if accepted else None,
            "accepted_digest": accepted.decision_digest if accepted else None,
            "input_tokens": model.usage_evidence.get("input_tokens"), "output_tokens": model.usage_evidence.get("output_tokens"),
            "total_tokens": model.usage_evidence.get("total_tokens"), "usage_quality": "ACTUAL" if model.usage_evidence else "UNAVAILABLE"})
        models[-1]["profile_digest"] = _ref([model.provider_kind, model.profile_identity])
        models[-1]["compaction_operation_ref"] = _ref(model.compaction_operation_id)
        models[-1]["budget_operation_ref"] = _ref(f"model:{model.step_id}:{model.attempt_number}:{model.model_attempt_number}")
    for snapshot in image.context_snapshots:
        source_ref = "snapshot:" + _ref(snapshot.snapshot_id)
        extracted, scanned = scan_facts(snapshot.payload.get("messages"), profile, source_ref)
        facts.extend(extracted)
        contexts.append({"source_ref": source_ref, "frame_ref": _ref(snapshot.frame_id), "version": snapshot.version,
            "snapshot_digest": snapshot.snapshot_digest, "parent_ref": _ref(snapshot.payload.get("parent_id")),
            "cursor": snapshot.payload.get("source_cursor"), "kind": snapshot.payload.get("kind"),
            "source_digest": snapshot.payload.get("source_digest"), "mandatory_digest": snapshot.payload.get("mandatory_digest"),
            "validator": {k: snapshot.validation.get(k) for k in ("version", "source", "mandatory", "roles", "pairs", "budget")}, "fact_scan_complete": scanned})
        contexts[-1]["request_budget_comparisons"] = receipt.evaluation_capture.get("context:"+snapshot.snapshot_id, [])
        contexts[-1]["compaction_operation_ref"] = next((_ref(o.operation_id) for o in image.context_operations if o.accepted_snapshot_id == snapshot.snapshot_id), None)
        if snapshot.payload.get("kind") == "COMPACTED" and not contexts[-1]["request_budget_comparisons"]:
            gaps.add("COMPACTION_COMPARISON_UNAVAILABLE")
    for delegation in delegations:
        child = frames.get(delegation.child_frame_id)
        result = delegation.result
        source_ref = "child-result:" + _ref(delegation.delegation_id)
        extracted, scanned = scan_facts(result, profile, source_ref)
        facts.extend(extracted)
        children.append({"source_ref": source_ref, "parent_ref": _ref(delegation.parent_frame_id),
            "child_ref": _ref(delegation.child_frame_id), "target_ref": _ref(child.execution_agent_id) if child else None,
            "permission_digest": _ref(delegation.frozen.get("permission_pin")), "result_digest": delegation.result_digest,
            "result_verified": result is not None and internal_digest(result) == delegation.result_digest,
            "parent_accepted": delegation.observed, "fact_scan_complete": scanned})
        import hashlib
        children[-1]["content_digest"] = hashlib.sha256(result["content"].encode("utf-8")).hexdigest() if result and isinstance(result.get("content"), str) else None
        parent_iteration = next((i for i in image.loop_iterations if i.frame_id == delegation.parent_frame_id and i.iteration_no == delegation.parent_iteration), None)
        observation_digest = parent_iteration.observation_digest if parent_iteration else None
        consumers = [m["source_ref"] for m in models if m["frame_ref"] == _ref(delegation.parent_frame_id)
            and m["accepted_kind"] is not None and any(s["source_digest"] == observation_digest for s in m["sources"]) and observation_digest]
        children[-1]["parent_observation_digest"] = observation_digest
        children[-1]["parent_model_ref"] = consumers[0] if consumers else None
    if receipt.evaluation_capture.get("limit_reached"):
        gaps.add("EVIDENCE_LIMIT")
    memory = {"mode": binding.mode if binding else None, "status": binding.status if binding else None,
        "manifest_digest": binding.manifest_digest if binding else None,
        "seed_digest": binding.seed_digest if binding else None, "selected": [], "admissions": [],
        "actual_use_proven": False, "effects": receipt.evaluation_capture.get("memory_effects")}
    if memory["effects"] is None or memory["effects"]["conversation"]["status"] in {"UNPROVEN", "UNEXPECTED_ACTIVITY"}:
        gaps.add("MEMORY_EFFECTS_UNPROVEN")
    if binding:
        verified = [c["memory"] for c in receipt.evaluation_capture.values() if isinstance(c, dict)
                    and c.get("purpose") == "DECISION" and "memory" in c]
        unique = {}
        for used in verified:
            for item in used:
                facts.extend(item["facts"])
                unique[item["record_ref"]] = {k: item.get(k) for k in ("record_ref", "kind", "version", "source_digest", "epistemic_kind")}
        memory["selected"] = sorted(unique.values(), key=lambda i: i["record_ref"])
        memory["actual_use_proven"] = bool(verified)
        memory["admissions"] = [{"ref": _ref(a.get("id")), "state": a.get("state"),
            "result_digest": a.get("result_digest")} for a in binding.admissions]
        # Manifest 只能证明选择和授权；不能自动声称正文进入了实际请求。
        if binding.mode == "SCOPED" and not verified:
            gaps.add("MEMORY_ACTUAL_USE_UNPROVEN")
    else:
        gaps.add("MEMORY_BINDING_MISSING")
    allocation_owners = {d.envelope_id: d.delegation_id for d in delegations}
    budget = {"consumed": image.root.budget_consumed, "reserved": image.root.budget_reserved,
        "operations": [{"ref": _ref(key), "kind": op["kind"], "state": op["state"],
            "reserved": op["reserved"], "actual": op.get("actual"), "unspent": op.get("unspent"),
            "released": op.get("released"), "delegation_ref": _ref(op.get("delegation_id") or allocation_owners.get(key)),
            "source": op.get("source") if op.get("source") in {"ACTUAL", "ESTIMATED", "RECOVERY_CONSERVATIVE"} else None}
            for key, op in sorted(image.root.budget_operations.items())]}
    return {"schema_version": OBSERVATIONS_SCHEMA, "extraction_version": "agent-quality-projection.v1",
        "coverage": "PARTIAL" if gaps else "COMPLETE", "gaps": sorted(gaps),
        "tools": tools, "models": models, "contexts": contexts, "delegations": children,
        "memory": memory, "facts": facts, "budget": budget}


def bundle(image, receipt, components, observed):
    gaps = set(observed["gaps"])
    if len(canonical_bytes(observed)) > OBSERVATIONS_LIMIT:
        observed = {"schema_version": OBSERVATIONS_SCHEMA, "extraction_version": "agent-quality-projection.v1",
            "coverage": "PARTIAL", "gaps": ["EVIDENCE_LIMIT"], "tools": [], "models": [], "contexts": [],
            "delegations": [], "memory": None, "facts": [], "budget": None}
        gaps.add("EVIDENCE_LIMIT")
    for c in components:
        if c["availability"] == "UNAVAILABLE" or c["body"] and c["body"].get("coverage") != "COMPLETE":
            gaps.add("COMPONENT_INCOMPLETE")
    if not image.terminal_sequence:
        gaps.add("ROOT_TERMINAL_BINDING_MISSING")
    roots = [f for f in image.loop_frames if f.frame_role == "ROOT"]
    final = roots[0].final_delivery_receipt if len(roots) == 1 else None
    result = {"schema_version": BUNDLE_SCHEMA, "producer_version": PRODUCER,
        "capture_policy_version": CAPTURE_POLICY, "algorithm_version": "catalog-json-v1",
        "evidence_id": "agent-quality://" + receipt.remote_run_id, "correlation": receipt.correlation,
        "request_fingerprint": receipt.request_fingerprint, "execution_manifest": receipt.execution_manifest,
        "execution_manifest_digest": catalog_digest(receipt.execution_manifest), "capture_profile": receipt.capture_profile,
        "capture_profile_digest": catalog_digest(receipt.capture_profile),
        "execution_policy": image.root.execution_policy,
        "terminal": {"run_id": image.root.run_id, "status": image.root.status,
            "stop_reason": image.root.stop_reason, "sequence": image.terminal_sequence},
        "root_final": {"content_digest": final.get("content_digest"), "event_id": final.get("event_id"),
            "cursor": final.get("cursor")} if final else None,
        "source_watermark": {"terminal_sequence": image.terminal_sequence,
            "frames": [{"ref": _ref(f.frame_id), "generation": f.resume_generation,
                        "observation_cursor": f.committed_observation_cursor} for f in sorted(image.loop_frames, key=lambda f: f.frame_id)]},
        "components": sorted(components, key=lambda c: c["kind"]), "observations": observed,
        "observations_digest": catalog_digest(observed), "coverage": "PARTIAL" if gaps else "COMPLETE",
        "gaps": sorted(gaps), "sealed": True}
    if len(canonical_bytes(result)) > BUNDLE_LIMIT:
        result["components"] = [component(c["kind"], c["schema_version"], reason="EVIDENCE_LIMIT") for c in components]
        result["observations"] = None
        result["observations_digest"] = None
        result["coverage"], result["gaps"] = "PARTIAL", ["EVIDENCE_LIMIT"]
    return result
