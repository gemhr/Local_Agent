"""WP04C 的最小身份/模式矩阵，同时提供双语言一致性输入。"""

from copy import deepcopy
from uuid import uuid4

import pytest

from core.stage13.contracts import sha256
from core.stage13.model_comparability import project_receipt, compare_receipts
from core.stage13.triage_subject import digest


def policy_input(model="deepseek-flash", provider="deepseek"):
    endpoint = "https://api.example.test"
    config = {
        "provider": provider,
        "model": model,
        "revision": None,
        "context_window": 1000000,
        "max_tokens": 4096,
        "temperature": 0,
        "thinking": False,
        "retry_attempts": 1,
        "endpoint_digest": sha256(endpoint.encode()),
    }
    manifest = {
        "subject_id": "ci_triage_candidate",
        "subject_version": "policy-test-1",
        "agent_id": "ci_triage_candidate",
        "agent_definition_version": "policy-test-1",
        "agent_definition_digest": "a" * 64,
        "prompt_version": "stage13.triage-prompt.v1",
        "prompt_digest": "b" * 64,
        "model_profile_id": "remote_advanced",
        "model_profile_digest": digest(config),
        "requested_provider": provider,
        "requested_model": model,
        "requested_revision": None,
        "tool_profile_id": "stage13.read-only-evidence.v1",
        "tool_profile_digest": "d" * 64,
        "output_schema_version": "stage13.triage-output.v1",
        "output_schema_digest": "9fb6df7454482257b9358a887e0b5c41871a6e66db9b462f6e7f3c1810f0dca4",
        "execution_enabled": True,
    }
    manifest["subject_manifest_digest"] = digest(manifest)
    run = str(uuid4())
    call = {
        "call_id": str(uuid4()),
        "run_id": run,
        "role": "INITIAL",
        "effective_messages_digest": "e" * 64,
        "effective_system_prompt_digest": "f" * 64,
        "resolved_profile_id": "remote_advanced",
        "resolved_profile_digest": digest(config),
        "model_config_digest": digest(config),
        "requested_provider": provider,
        "requested_model": model,
        "requested_revision": None,
        "reported_provider": None,
        "reported_model": model,
        "reported_revision": None,
        "actual_revision": None,
        "dispatch_certainty": "PROVIDER_RESPONDED",
        "verification_status": "PROVIDER_MODEL_MATCH",
        "state": "COMPLETED",
        "input_tokens": None,
        "output_tokens": None,
        "cost": None,
        "resolved_provider": provider,
        "resolved_provider_source": "CONFIGURED_TRANSPORT",
        "resolved_endpoint": endpoint,
        "resolved_model_config": config,
        "system_fingerprint": "fingerprint-A",
    }
    receipt = {
        "receipt_version": "stage13.actual-subject-receipt.v1",
        "run_id": run,
        "anchor_run_id": run,
        "analysis_job_id": None,
        "evaluation_attempt_id": run,
        "role": "INITIAL",
        "actual_subject_manifest": manifest,
        "actual_input_digest": "1" * 64,
        "effective_payload_digest": "2" * 64,
        "prompt_template_digest": sha256(
            "仅分析下面原始授权输入；输出一个严格七字段 JSON 对象。".encode()
        ),
        "resolved_toolset_identity": "d" * 64,
        "final_answer_digest": "3" * 64,
        "model_call_receipts": [call],
        "model_identity_verification": "PROVIDER_MODEL_MATCH",
    }
    seal(receipt)
    return {"manifest": manifest, "receipt": receipt, "evidence": []}


def seal(receipt):
    receipt.pop("receipt_digest", None)
    receipt["receipt_digest"] = digest(receipt)


def policy_cases():
    cases = []
    for name in (
        "normal_no_revision",
        "model_mismatch",
        "version_mismatch",
        "provider_mismatch",
        "missing_model",
        "fingerprint_same",
        "fingerprint_changed",
        "profile_mismatch",
        "model_upgrade",
        "upgrade_undeclared",
        "endpoint_mismatch",
        "transport_missing",
        "call_binding",
        "exact_revision",
        "revision_only",
        "claimed_exact_without_proof",
        "requested_revision_echo",
        "input_mismatch",
        "tool_mismatch",
    ):
        left, right = policy_input(), policy_input()
        mode, intended = "AGENT_REGRESSION", []
        level, comparable = "PROVIDER_MODEL_MATCH", True
        call = right["receipt"]["model_call_receipts"][0]
        if name == "model_mismatch":
            call["reported_model"] = "glm-5.3"
            level, comparable = "MODEL_IDENTITY_MISMATCH", False
        elif name in ("version_mismatch", "model_upgrade", "upgrade_undeclared"):
            left, right = policy_input("glm-5.2", "glm"), policy_input("glm-5.3", "glm")
            comparable = name == "model_upgrade"
            if name != "version_mismatch":
                mode = "MODEL_UPGRADE"
            if name == "model_upgrade":
                intended = ["canonical_model", "model_profile"]
        elif name == "provider_mismatch":
            right = policy_input(provider="minimax")
            comparable = False
        elif name == "missing_model":
            call["reported_model"] = None
            level, comparable = "MODEL_IDENTITY_UNKNOWN", False
        elif name == "fingerprint_changed":
            call["system_fingerprint"] = "fingerprint-B"
        elif name == "profile_mismatch":
            call["resolved_model_config"]["temperature"] = 0.7
            level, comparable = "MODEL_IDENTITY_MISMATCH", False
        elif name == "endpoint_mismatch":
            call["resolved_endpoint"] = "https://unexpected.test"
            level, comparable = "MODEL_IDENTITY_MISMATCH", False
        elif name == "transport_missing":
            call["resolved_provider"] = None
            level, comparable = "MODEL_IDENTITY_UNKNOWN", False
        elif name == "call_binding":
            call["run_id"] = str(uuid4())
            level, comparable = "MODEL_IDENTITY_MISMATCH", False
        elif name == "exact_revision":
            call["reported_revision"] = call["actual_revision"] = (
                "provider-immutable-revision-1"
            )
            call["verification_status"] = "EXACT_DEPLOYMENT_VERIFIED"
            call["reported_artifact_digest"] = "6" * 64
            call["reported_deployment_id"] = "provider-deployment-1"
            level = "EXACT_DEPLOYMENT_VERIFIED"
        elif name in ("revision_only", "claimed_exact_without_proof"):
            call["reported_revision"] = call["actual_revision"] = "provider-revision-1"
            if name == "claimed_exact_without_proof":
                call["verification_status"] = "EXACT_DEPLOYMENT_VERIFIED"
        elif name == "requested_revision_echo":
            call["actual_revision"] = "request-only"
            level, comparable = "MODEL_IDENTITY_MISMATCH", False
        elif name == "input_mismatch":
            right["receipt"]["actual_input_digest"] = "4" * 64
            comparable = False
        elif name == "tool_mismatch":
            right["manifest"]["tool_profile_digest"] = "5" * 64
            right["manifest"].pop("subject_manifest_digest")
            right["manifest"]["subject_manifest_digest"] = digest(right["manifest"])
            right["receipt"]["resolved_toolset_identity"] = "5" * 64
            comparable = False
        seal(right["receipt"])
        cases.append(
            {
                "name": name,
                "document": {
                    "evaluation_mode": mode,
                    "intended_variables": intended,
                    "baseline": left,
                    "candidate": right,
                },
                "candidate_level": level,
                "comparable": comparable,
            }
        )
    return cases


def project_document(document):
    mode = document["evaluation_mode"]
    sides = []
    for key in ("baseline", "candidate"):
        side = document[key]
        sides.append(
            {
                "manifest": side["manifest"],
                "input_digest": side["receipt"]["actual_input_digest"],
                "tool_identity": side.get("tool_identity"),
                "projection": project_receipt(
                    side["manifest"],
                    side["receipt"],
                    side["evidence"],
                    evaluation_mode=mode,
                ),
            }
        )
    return compare_receipts(
        *sides, evaluation_mode=mode, intended_variables=document["intended_variables"]
    )


@pytest.mark.parametrize("case", policy_cases(), ids=lambda c: c["name"])
def test_model_comparability_policy(case):
    before = deepcopy(case["document"])
    result = project_document(case["document"])
    assert result["candidate"]["identity_level"] == case["candidate_level"]
    assert result["comparable"] is case["comparable"]
    if case["name"] == "fingerprint_changed":
        assert result["comparability"] == "COMPARABLE_WITH_ENVIRONMENT_WARNING"
        assert result["warnings"] == ["BACKEND_CONFIGURATION_CHANGED"]
    if case["name"] == "fingerprint_same":
        assert result["fingerprint_status"] == "BACKEND_FINGERPRINT_MATCH"
    assert case["document"] == before
