"""Stage13 offline 执行 envelope；180 秒起点由 EvalOps 唯一拥有。"""

from copy import deepcopy
from datetime import datetime

from core.stage13.triage_subject import digest, strict_json

POLICY_VERSION = "stage13.attempt-execution.v1"


def semantic_input(payload):
    """仅剥离历史绝对执行期限，保留完整业务和授权语义。"""
    result = deepcopy(payload)
    result["evidence_policy"].pop("deadline_at", None)
    return result


def bind_execution_policy(body):
    """核对实际请求，冻结可由两仓独立重算的摘要。"""
    policy = body["execution_policy"]
    payload = strict_json(body["query"])
    start = datetime.fromisoformat(policy["execution_started_at"])
    deadline = datetime.fromisoformat(policy["execution_deadline_at"])
    if (
        set(policy)
        != {
            "version",
            "evaluation_run_id",
            "evaluation_attempt_id",
            "timeout_seconds",
            "execution_started_at",
            "execution_deadline_at",
            "semantic_input_digest",
        }
        or policy["version"] != POLICY_VERSION
        or policy["evaluation_attempt_id"] != body["run_id"]
        or policy["timeout_seconds"] != 180
        or body["timeout_seconds"] != 180
        or start.tzinfo is None
        or deadline.tzinfo is None
        or (deadline - start).total_seconds() != 180
        or payload["evidence_policy"]["deadline_at"] != policy["execution_deadline_at"]
        or policy["semantic_input_digest"] != digest(semantic_input(payload))
    ):
        raise ValueError("EXECUTION_POLICY_BINDING_MISMATCH")
    return {"policy": policy, "execution_request_digest": digest(body)}
