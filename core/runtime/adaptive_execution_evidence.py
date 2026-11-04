"""双模式评测安全投影；原执行聚合和评测 Receipt 是事实来源。"""
from core.runtime.agent_loop.quality_contract import catalog_digest
from core.runtime.execution_aggregate import canonical_payload_digest

SCHEMA = "adaptive-execution-evidence.v1"
POLICY_VERSION = "ci-adaptive-evaluation.v1"
GROUPS = ("BASELINE", "CANDIDATE_A", "CANDIDATE_B")


def pin_environment(router, context, receipt):
    """复用本 Run 已解析的工具快照和实际模型配置，禁止仅凭 profile 名字比较。"""
    from core.runtime.agent_loop.context import _pin_value
    model = dict(profiles=_pin_value(router.model_profiles), max_tokens=router.max_tokens,
        adapters=[type(router.model_adapter_resolver.resolve(p.profile_id)).__module__ + "." +
            type(router.model_adapter_resolver.resolve(p.profile_id)).__qualname__ for p in router.model_profiles],
        routing=dict(allow_escalation=router.model_routing_policy.allow_escalation,
            allow_downgrade=router.model_routing_policy.allow_downgrade,
            require_confirmation=router.model_routing_policy.require_confirmation_for_downgrade))
    tools = [dict(schema=r.native_function_definition(), spec=_pin_value(r.adapter.spec),
        adapter=type(r.adapter).__module__ + "." + type(r.adapter).__qualname__) for r in context.tool_resolution_snapshot.registrations()]
    return dict(model_configuration_digest=canonical_payload_digest(model), tool_configuration_digest=canonical_payload_digest(tools),
        authorization_digest=catalog_digest(dict(principal_id=receipt.principal_id, tenant_id=receipt.tenant_id)),
        timeout_milliseconds=receipt.execution_manifest["timeout_milliseconds"],
        business_input_digest=catalog_digest(receipt.execution_manifest["business_input"]))


def strategy(group):
    if group not in GROUPS:
        raise ValueError("ADAPTIVE_GROUP_INVALID")
    value = dict(version=POLICY_VERSION, group=group, query_mode="AGENT_LOOP" if group == "BASELINE" else "FAST_PATH",
        diagnosis_stop=group == "CANDIDATE_B")
    return {**value, "digest": catalog_digest(value)}


def validate_admission(root, receipt):
    """原 Run 初始化事务中核对新协议的受信准入，不套用旧 Loop manifest。"""
    manifest = receipt.execution_manifest
    policy = manifest["strategy"]
    loop = root.resume_input.get("agent_loop") or {}
    status = manifest["slice"] == "STATUS"
    expected_agent = "ci_execution_status" if status else "ci_incident_investigator"
    expected_engine = "fixed-plan.v1" if status and policy["query_mode"] == "FAST_PATH" else "durable-agent-loop.v2"
    route = root.resume_input.get("adaptive_route")
    if (policy != strategy(policy["group"]) or root.run_id != receipt.attempt_id
            or root.resume_input.get("resolved_agent_id") != expected_agent or root.execution_policy != expected_engine
            or status and (route is None or route["mode"] != policy["query_mode"]
                or any(route["query"][k] != manifest["business_input"][k] for k in ("incident_id", "execution_id")))
            or not status and loop.get("pinned_config", {}).get("adaptive_stop_enabled") != policy["diagnosis_stop"]):
        raise ValueError("ADAPTIVE_RUNTIME_PIN_INVALID")


def build_evidence(image, correlation, policy, business, actions=()):
    root = image.root
    route = root.resume_input.get("adaptive_route")
    fast = root.execution_policy == "fixed-plan.v1"
    if root.status not in {"SUCCEEDED", "FAILED", "CANCELLED"} or fast != (policy["query_mode"] == "FAST_PATH" and business["slice"] == "STATUS"):
        raise ValueError("ADAPTIVE_EXECUTION_BINDING_INVALID")
    if fast and (image.loop_frames or image.models):
        raise ValueError("ADAPTIVE_FAST_EVIDENCE_INVALID")
    if business["slice"] == "STATUS" and (route is None or route["mode"] != policy["query_mode"]):
        raise ValueError("ADAPTIVE_ROUTE_BINDING_INVALID")
    frames = []
    for frame in image.loop_frames:
        if frame.frame_role == "ROOT" and business["slice"] == "DIAGNOSIS":
            if (frame.pinned_config.get("adaptive_stop") is not None) != policy["diagnosis_stop"]:
                raise ValueError("ADAPTIVE_STOP_BINDING_INVALID")
        iterations = sorted((i for i in image.loop_iterations if i.frame_id == frame.frame_id), key=lambda i: i.iteration_no)
        frames.append(dict(ref=catalog_digest(frame.frame_id), role=frame.frame_role, config_digest=frame.config_digest, seed_digest=frame.seed_digest,
            iteration_count=sum(i.frame_id == frame.frame_id for i in image.loop_iterations),
            observation_count=frame.committed_observation_cursor, stop_decision=frame.adaptive_decision,
            iterations=[dict(number=i.iteration_no, state=i.state, kind=i.decision_kind,
                decision_ref=catalog_digest(i.decision_id) if i.decision_id else None,
                observation_cursor=i.observation_cursor, observation_digest=i.observation_digest,
                source_digest=i.observation_source_digest) for i in iterations]))
    started = [m for m in image.models if m.started_at is not None]
    token_rows = [m.usage_evidence for m in started if all(type(m.usage_evidence.get(k)) is int for k in ("input_tokens", "output_tokens", "total_tokens"))]
    usage = {k: sum(v[k] for v in token_rows) for k in ("input_tokens", "output_tokens", "total_tokens")} if started and len(token_rows) == len(started) else None
    model_ms = sum(max(0, int((m.completed_at - m.started_at).total_seconds() * 1000)) for m in started) if all(m.completed_at is not None for m in started) else None
    tools = [t for t in image.tool_invocations if t.started_at is not None]
    tool_ms = sum(t.committed_result["duration_ms"] for t in tools) if all(t.state == "COMMITTED" and t.committed_result is not None for t in tools) else None
    identity = {k: v for k, v in correlation.items() if k != "evaluation_project_id"}
    identity["project_id"] = correlation["evaluation_project_id"]
    comparability = {**root.resume_input["adaptive_evaluation_environment"],
        "investigation_seed_digest": next((f.seed_digest for f in image.loop_frames if f.frame_role == "ROOT"), None) if business["slice"] == "DIAGNOSIS" else None}
    delivered = business.pop("runtime_delivered", None)
    return dict(schema_version=SCHEMA, identity=identity, run_id=root.run_id,
        comparability=comparability,
        strategy=dict(group=policy["group"], version=policy["version"], digest=policy["digest"],
            mode="FAST_PATH" if fast else "AGENT_LOOP", engine_policy=root.execution_policy,
            route_policy_version=route["policy_version"] if route else None, route_policy_digest=route["policy_digest"] if route else None,
            route_reason=route["reason"] if route else None),
        runtime=dict(status=root.status, stop_reason=root.stop_reason, error_code=business.pop("runtime_error", None),
            delivered=(delivered is True if fast else any(f.final_delivery_receipt is not None and f.final_delivery_receipt.get("status") == "DELIVERED" for f in image.loop_frames))),
        business=business,
        metrics=dict(model_attempts_started=len(started), tool_invocations_started=len(tools),
            model_states={s.lower(): sum(m.state == s for m in image.models) for s in ("NOT_STARTED", "STARTED", "COMPLETED", "UNKNOWN", "REJECTED")},
            tool_states={s.lower(): sum(t.state == s for t in image.tool_invocations) for s in ("PREPARED", "STARTED", "COMMITTED", "UNKNOWN", "NOT_COMMITTED")},
            provider_usage=usage, usage_available_attempts=len(token_rows), actual_cost=None,
            model_duration_ms=model_ms, tool_duration_ms=tool_ms, router_duration_ms=None, admission_duration_ms=None,
            delivery_duration_ms=None, first_meaningful_response_ms=None),
        mode_evidence=dict(fast=dict(step_count=len(image.steps), tool_refs=[catalog_digest(t.invocation_id) for t in image.tool_invocations]) if fast else None,
            loop=dict(frames=frames, tool_actions=[dict(frame_ref=catalog_digest(a.frame_id), iteration=a.iteration_no,
                invocation_ref=catalog_digest(a.invocation_id) if a.invocation_id else None,
                started=any(t.invocation_id == a.invocation_id and t.started_at is not None for t in image.tool_invocations)) for a in actions]) if not fast else None))
