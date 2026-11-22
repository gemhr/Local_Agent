"""根 CI 调查的冻结停止合同；不拥有 Loop 或执行状态。"""
import json

from core.runtime.budget import BudgetPolicy, SHARED_TOKEN_DIMENSIONS
from core.runtime.execution_aggregate import canonical_payload_digest as digest
from .contracts import LoopStopped

AGENT_ID = "ci_incident_investigator"
POLICY = dict(version="ci-investigation-stop.v1", coverage_rule="ci-observed-minimum.v1", reserve_seconds=15)
POLICY_DIGEST = digest(POLICY)


def stop_policy(frame):
    policy = frame.pinned_config.get("adaptive_stop")
    if policy is None:
        return None
    if (frame.schema_version != 2 or frame.frame_role != "ROOT" or frame.execution_agent_id != AGENT_ID
            or policy != {**POLICY, "digest": POLICY_DIGEST}):
        raise RuntimeError("LOOP_ADAPTIVE_POLICY_INVALID")
    return policy


def evidence_identity(frame, iterations, sources):
    observed = [i for i in iterations if i.frame_id == frame.frame_id and i.state == "OBSERVED"]
    if [i.observation_cursor for i in observed] != list(range(1, frame.committed_observation_cursor + 1)):
        raise RuntimeError("LOOP_ADAPTIVE_SOURCE_INVALID")
    for item in observed:
        if digest(item.observation) != item.observation_digest:
            raise RuntimeError("LOOP_ADAPTIVE_SOURCE_INVALID")
    return digest(dict(cursor=frame.committed_observation_cursor,
        observations=[dict(iteration=i.iteration_no, observation=i.observation_digest, source=i.observation_source_digest) for i in observed],
        sources={key: value["digest"] for key, value in sorted(sources.items())}))


def validate_restriction(frame):
    value = frame.adaptive_decision
    if value is None:
        return
    policy = stop_policy(frame)
    payload = {k: v for k, v in value.items() if k != "decision_id"}
    cap = value.get("output_reserve")
    extra = {"output_reserve"} if "output_reserve" in value else set()
    if extra and (frame.loop_policy.get("budget_strategy") != "shared-root.v1"
            or type(cap) is not int or not 1 <= cap <= frame.pinned_config["max_tokens"]):
        raise RuntimeError("LOOP_ADAPTIVE_DECISION_INVALID")
    if (policy is None or set(value) != extra | {"version", "policy_digest", "observation_cursor", "evidence_digest", "effective_iteration",
            "allowed_actions", "reason", "insufficient", "decision_id"}
            or value["version"] != "final-only.v1" or value["policy_digest"] != policy["digest"]
            or value["allowed_actions"] != "FINAL_ONLY" or value["reason"] not in {"COVERAGE_SUFFICIENT", "BUDGET_RESERVE"}
            or type(value["insufficient"]) is not bool or value["insufficient"] != (value["reason"] == "BUDGET_RESERVE")
            or value["observation_cursor"] != frame.committed_observation_cursor
            or not 1 <= frame.current_iteration <= value["effective_iteration"] <= frame.loop_policy["max_iterations"]
            or value["decision_id"] != digest(payload)):
        raise RuntimeError("LOOP_ADAPTIVE_DECISION_INVALID")


def enforce_final_only(frame, decision, iteration_no):
    value = frame.adaptive_decision
    if value is None:
        return
    validate_restriction(frame)
    if iteration_no != value["effective_iteration"] or decision.kind not in {"FINAL", "REFUSAL"}:
        raise LoopStopped("LOOP_DECISION_INVALID")
    if value["insufficient"] and decision.kind == "FINAL":
        try:
            report = json.loads(decision.content)
            if report.get("diagnosis_status") != "INSUFFICIENT_EVIDENCE" or not report.get("uncertainties"):
                raise ValueError("insufficient report required")
        except (ValueError, TypeError, AttributeError):
            raise LoopStopped("LOOP_DECISION_INVALID") from None


def consider_stop(repo, submit, frame, iterations, context, router, capabilities, definition):
    profiles = router.model_profiles
    policy = stop_policy(frame)
    if policy is None or frame.adaptive_decision is not None:
        return
    observed = bool(iterations and iterations[-1].state == "OBSERVED" and frame.committed_observation_cursor)
    if not observed and (iterations or frame.loop_policy.get("budget_strategy") != "shared-root.v1"):
        return
    budget = context.budget_ledger.snapshot()
    remaining = context.remaining_seconds()
    latency = max((p.cost_profile.estimated_latency_ms / 1000 for p in profiles if p.cost_profile), default=0)
    # 固定余时阈值是停止继续调查的保守规则，不是模型完成时间保证。
    reserve = (remaining is not None and remaining <= max(policy["reserve_seconds"], latency * 2)
        or budget.run_budget.max_model_calls is not None and budget.remaining.model_calls <= 1
        or budget.run_budget.max_tool_calls is not None and budget.remaining.tool_calls <= 0)
    final_output = None
    limits = {dimension: available for dimension in SHARED_TOKEN_DIMENSIONS
              if (available := budget.remaining_limit(dimension)) is not None}
    if frame.loop_policy.get("budget_strategy") == "shared-root.v1" and limits:
        from types import SimpleNamespace
        from core.runtime.model_routing import ModelRoutingError
        from core.runtime.model_selection import ModelSelectionError, model_request_usage
        from .compaction import eligible_context_assessments, request_assessments, route_context_request
        from .context import context_sources, snapshot_candidate
        from .handoff import loop_options

        # 根据已观察证据估算下一次调查与同证据下一次较短报告的请求需求。
        final_frame = SimpleNamespace(**{**vars(frame), "adaptive_decision": {"allowed_actions": "FINAL_ONLY", "insufficient": True}})
        groups = context_sources(frame, iterations)
        options = loop_options(final_frame)
        cap = frame.pinned_config["max_tokens"]
        short_cap = max(1, cap // 2)
        final_messages = snapshot_candidate(final_frame, groups, options, short_cap).payload["messages"]
        final, _ = request_assessments(router, final_messages, options, short_cap, capabilities, definition)
        investigation_opts = loop_options(frame)
        prior_summary = None
        if frame.current_context_snapshot_id is not None:
            image = submit(repo.load(context.run_id))
            previous = next((s for s in image.context_snapshots
                             if s.snapshot_id == frame.current_context_snapshot_id), None)
            if previous is not None:
                prior_summary = previous.payload["summary"]
        investigation_messages = snapshot_candidate(
            frame, groups, investigation_opts, cap, summary=prior_summary, kind="RAW").payload["messages"]
        investigation, _ = request_assessments(router, investigation_messages, investigation_opts, cap, capabilities, definition)
        final = eligible_context_assessments(router, final, context, capabilities, frame=frame, purpose="DECISION")
        investigation = eligible_context_assessments(router, investigation, context, capabilities, frame=frame, purpose="DECISION")
        if observed or investigation:
            by_id = {p.profile_id.value: p for p in profiles}
            final_usage = [model_request_usage(by_id[a.profile_id], a.input_bound, short_cap) for a in final]
            feasible_final = [usage for usage in final_usage if BudgetPolicy.feasible(budget, usage)]
            if not feasible_final:
                if not observed:
                    context.raise_if_inactive()
                raise LoopStopped("LOOP_EVIDENCE_INSUFFICIENT")

            first = None
            if investigation and (not observed or not reserve):
                # 首轮和后续调查都按实际路由的首选预算预测。
                try:
                    routing = route_context_request(router, frame.execution_agent_id, investigation_messages,
                        context, capabilities, cap, definition, investigation, budget)
                except (ModelSelectionError, ModelRoutingError):
                    pass
                else:
                    if routing.candidates:
                        selected = routing.candidates[0].profile
                        assessment = next(a for a in investigation if a.profile_id == selected.profile_id.value)
                        first = model_request_usage(selected, assessment.input_bound, cap)
            if not observed:
                # 首轮两笔请求各自通过预算检查。输入和输出分别在取证后
                # 用实耗检查合计，总量、成本和模型调用次数现在就要留出空间。
                initial_limits = {d: available for d in ("total_tokens", "cost_units", "model_calls", "remote_model_calls")
                                  if (available := budget.remaining_limit(d)) is not None}
                if first is None or not any(
                    all(getattr(first, dimension) + getattr(report, dimension) <= available
                        for dimension, available in initial_limits.items())
                    for report in feasible_final
                ):
                    context.raise_if_inactive()
                    raise LoopStopped("LOOP_EVIDENCE_INSUFFICIENT")
            else:
                if not reserve:
                    reserve = first is None or not any(
                        BudgetPolicy.feasible(budget, first.plus(report)) for report in feasible_final)
                if reserve:
                    final_output = short_cap
    if not observed:
        return
    submit(repo.consider_loop_final_only(context.durable_lease, frame.frame_id,
        resume_generation=frame.resume_generation, row_version=frame.row_version, reserve=reserve,
        **({"output_reserve": final_output} if final_output is not None else {})))
