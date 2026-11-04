"""根 CI 调查的冻结停止合同；不拥有 Loop 或执行状态。"""
import json

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
    if (policy is None or set(value) != {"version", "policy_digest", "observation_cursor", "evidence_digest", "effective_iteration",
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


def consider_stop(repo, submit, frame, iterations, context, profiles):
    policy = stop_policy(frame)
    if (policy is None or frame.adaptive_decision is not None or not iterations
            or iterations[-1].state != "OBSERVED" or not frame.committed_observation_cursor):
        return
    budget = context.budget_ledger.snapshot()
    remaining = context.remaining_seconds()
    latency = max((p.cost_profile.estimated_latency_ms / 1000 for p in profiles if p.cost_profile), default=0)
    # 固定余时阈值是停止继续调查的保守规则，不是模型完成时间保证。
    reserve = (remaining is not None and remaining <= max(policy["reserve_seconds"], latency * 2)
        or budget.run_budget.max_model_calls is not None and budget.remaining.model_calls <= 1
        or budget.run_budget.max_tool_calls is not None and budget.remaining.tool_calls <= 0)
    submit(repo.consider_loop_final_only(context.durable_lease, frame.frame_id,
        resume_generation=frame.resume_generation, row_version=frame.row_version, reserve=reserve))
