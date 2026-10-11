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
    from core.stage13.input_contract_v2 import (
        INPUT_V2,
        EXECUTION_V2,
        IDENTITIES,
        comparison_domain,
        validate_input_v2,
        validate_contract_identity,
    )

    v2 = policy["version"] == EXECUTION_V2
    expected_fields = {
        "version",
        "evaluation_run_id",
        "evaluation_attempt_id",
        "timeout_seconds",
        "execution_started_at",
        "execution_deadline_at",
        "semantic_input_digest",
    }
    if v2:
        expected_fields |= set(IDENTITIES) | {
            "source_input_digest",
            "source_semantic_input_digest",
            "projected_input_digest",
            "comparison_domain_digest",
        }
        validate_input_v2(payload)
        validate_contract_identity(policy)
        validate_contract_identity(body["expected_subject_manifest"])
        if (
            payload["schema_version"] != INPUT_V2
            or policy["source_semantic_input_digest"] != digest(semantic_input(payload))
            or policy["projected_input_digest"] != digest(semantic_input(payload))
            or policy["comparison_domain_digest"] != digest(comparison_domain())
        ):
            raise ValueError("EXECUTION_POLICY_BINDING_MISMATCH")
        from core.stage13.input_contract_v2 import receipt_contract_fields

        receipt_contract_fields(body["expected_subject_manifest"], policy)
    elif payload.get("schema_version") == INPUT_V2 or set(
        body["expected_subject_manifest"]
    ) & set(IDENTITIES):
        raise ValueError("EXECUTION_POLICY_VERSION_MISMATCH")
    if (
        set(policy) != expected_fields
        or policy["version"] != (EXECUTION_V2 if v2 else POLICY_VERSION)
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
