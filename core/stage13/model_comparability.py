"""Stage13 v2 模型身份投影；不修改不可变执行回执。"""

import hashlib

from core.stage13.triage_subject import digest, check_manifest, strict_json

POLICY_VERSION = "stage13.model-comparability.v2"
AGENT_REGRESSION = "AGENT_REGRESSION"
MODEL_UPGRADE = "MODEL_UPGRADE"


def project_receipt(
    expected, receipt, evidence=(), *, evaluation_mode=AGENT_REGRESSION
):
    """从实际 call、配置和可选的历史响应取证独立计算身份等级。"""
    result = {
        "policy_version": POLICY_VERSION,
        "evaluated_under_policy_version": POLICY_VERSION,
        "evaluation_mode": evaluation_mode,
        "identity_level": "MODEL_IDENTITY_UNKNOWN",
        "comparable": False,
        "expected_provider": expected.get("requested_provider"),
        "actual_provider": None,
        "expected_model": expected.get("requested_model"),
        "actual_model": None,
        "model_profile_match": False,
        "revision": None,
        "revision_status": "UNAVAILABLE",
        "revision_required": False,
        "system_fingerprint": None,
        "fingerprint_status": "UNAVAILABLE",
        "warnings": [],
        "reasons": [],
    }

    def reject(reason, *, mismatch=False):
        result["identity_level"] = (
            "MODEL_IDENTITY_MISMATCH" if mismatch else "MODEL_IDENTITY_UNKNOWN"
        )
        result["reasons"].append(reason)
        return result

    if evaluation_mode not in (AGENT_REGRESSION, MODEL_UPGRADE):
        return reject("EVALUATION_MODE_UNSUPPORTED", mismatch=True)
    try:
        check_manifest(expected, expected)
        if (
            receipt["actual_subject_manifest"] != expected
            or receipt["receipt_digest"]
            != digest({k: v for k, v in receipt.items() if k != "receipt_digest"})
            or receipt["resolved_toolset_identity"] != expected["tool_profile_digest"]
        ):
            return reject("SUBJECT_RECEIPT_MISMATCH", mismatch=True)
        calls = receipt["model_call_receipts"]
        if not calls:
            return reject("MODEL_CALL_MISSING")
        if len(calls) != 1:
            return reject("MODEL_CALL_BINDING_INVALID", mismatch=True)
        call = calls[0]
        if call["run_id"] != receipt["run_id"] or call["role"] != receipt["role"]:
            return reject("MODEL_CALL_BINDING_INVALID", mismatch=True)
        result["model_profile_match"] = (
            call["resolved_profile_id"] == expected["model_profile_id"]
            and call["resolved_profile_digest"]
            == call["model_config_digest"]
            == expected["model_profile_digest"]
        )
        if not result["model_profile_match"]:
            return reject("MODEL_PROFILE_MISMATCH", mismatch=True)
        if any(
            call[k] != expected[k]
            for k in ("requested_provider", "requested_model", "requested_revision")
        ):
            return reject("REQUESTED_IDENTITY_MISMATCH", mismatch=True)
        actual = call
        if evidence:
            if len(evidence) != 1:
                return reject("MODEL_EVIDENCE_BINDING_INVALID", mismatch=True)
            actual = dict(evidence[0])
            if any(
                actual[k] != v
                for k, v in {
                    "call_id": call["call_id"],
                    "run_id": call["run_id"],
                    "receipt_digest": receipt["receipt_digest"],
                    "effective_messages_digest": call["effective_messages_digest"],
                }.items()
            ):
                return reject("MODEL_EVIDENCE_BINDING_INVALID", mismatch=True)
            raw = actual["response_body"]
            if (
                hashlib.sha256(raw.encode()).hexdigest()
                != actual["response_body_sha256"]
            ):
                return reject("PROVIDER_RESPONSE_DIGEST_INVALID", mismatch=True)
            packets = [
                strict_json(line[6:])
                for line in raw.splitlines()
                if line.startswith("data: ") and line[6:] != "[DONE]"
            ]
            models = {p["model"] for p in packets if p.get("model")}
            revisions = {
                p["model_revision"] for p in packets if p.get("model_revision")
            }
            fingerprints = {
                p["system_fingerprint"] for p in packets if p.get("system_fingerprint")
            }
            if len(models) != 1 or len(revisions) > 1 or len(fingerprints) > 1:
                return reject("PROVIDER_RESPONSE_IDENTITY_UNCONFIRMED")
            actual["reported_model"] = next(iter(models))
            actual["reported_revision"] = next(iter(revisions), None)
            actual["system_fingerprint"] = next(iter(fingerprints), None)
            for source, target in (
                ("model_artifact_sha256", "reported_artifact_digest"),
                ("deployment_id", "reported_deployment_id"),
            ):
                values = {p[source] for p in packets if p.get(source)}
                if len(values) > 1:
                    return reject("PROVIDER_RESPONSE_IDENTITY_UNCONFIRMED")
                actual[target] = next(iter(values), None)
            if (
                actual["reported_model"] != call["reported_model"]
                or actual["reported_revision"] != call["reported_revision"]
            ):
                return reject("PROVIDER_RESPONSE_RECEIPT_MISMATCH", mismatch=True)
        if (
            call["state"] != "COMPLETED"
            or call["dispatch_certainty"] != "PROVIDER_RESPONDED"
        ):
            return reject("PROVIDER_RESPONSE_UNCONFIRMED")
        config = actual.get("resolved_model_config")
        provider = actual.get("resolved_provider")
        result["actual_provider"] = provider
        result["actual_model"] = actual.get("reported_model")
        if (
            not provider
            or actual.get("resolved_provider_source") != "CONFIGURED_TRANSPORT"
            or not config
        ):
            return reject("RESOLVED_TRANSPORT_UNAVAILABLE")
        if digest(config) != expected["model_profile_digest"]:
            return reject("RESOLVED_CONFIG_MISMATCH", mismatch=True)
        endpoint = actual.get("resolved_endpoint")
        if not endpoint or hashlib.sha256(endpoint.encode()).hexdigest() != config.get(
            "endpoint_digest"
        ):
            return reject("ENDPOINT_BINDING_INVALID", mismatch=True)
        if (
            provider != config.get("provider")
            or provider != expected["requested_provider"]
        ):
            return reject("PROVIDER_MISMATCH", mismatch=True)
        if config.get("model") != expected["requested_model"]:
            return reject("MODEL_CONFIG_MISMATCH", mismatch=True)
        if call.get("reported_provider") not in (None, provider):
            return reject("REPORTED_PROVIDER_MISMATCH", mismatch=True)
        if not result["actual_model"]:
            return reject("REPORTED_MODEL_UNAVAILABLE")
        if result["actual_model"] != result["expected_model"]:
            return reject("CANONICAL_MODEL_MISMATCH", mismatch=True)
        revision = actual.get("reported_revision")
        result["revision"] = revision or None
        if revision and revision.strip():
            result["revision_status"] = "PROVIDER_REPORTED"
        if (
            call.get("actual_revision") is not None
            and call["actual_revision"] != revision
        ):
            return reject("ACTUAL_REVISION_NOT_REPORTED", mismatch=True)
        if (
            call.get("actual_revision") is not None
            and not call["actual_revision"].strip()
        ):
            return reject("ACTUAL_REVISION_NOT_REPORTED", mismatch=True)
        if (
            expected.get("requested_revision") is not None
            and revision
            and expected["requested_revision"] != revision
        ):
            return reject("EXPECTED_REVISION_MISMATCH", mismatch=True)
        result["system_fingerprint"] = actual.get("system_fingerprint") or None
        if result["system_fingerprint"]:
            result["fingerprint_status"] = "RECORDED"
        result["identity_level"] = "PROVIDER_MODEL_MATCH"
        artifact = actual.get("reported_artifact_digest")
        deployment = actual.get("reported_deployment_id")
        if (
            artifact
            and len(artifact) == 64
            and all(c in "0123456789abcdef" for c in artifact)
            and deployment
            and deployment.strip()
            and revision
            and revision.strip()
        ):
            result["identity_level"] = "EXACT_DEPLOYMENT_VERIFIED"
            result["revision_status"] = "VERIFIED_BY_PROVIDER_RESPONSE"
        result["comparable"] = True
        result["reasons"] = ["PROVIDER_CANONICAL_MODEL_PROFILE_MATCH"]
        return result
    except (KeyError, TypeError, ValueError, UnicodeError):
        return reject("MODEL_EVIDENCE_INVALID", mismatch=True)


def compare_receipts(
    baseline, candidate, *, evaluation_mode=AGENT_REGRESSION, intended_variables=()
):
    """比较同输入的两侧身份；模型升级差异须由评测计划明确声明。"""
    left, right = baseline["projection"], candidate["projection"]
    result = {
        "policy_version": POLICY_VERSION,
        "evaluated_under_policy_version": POLICY_VERSION,
        "evaluation_mode": evaluation_mode,
        "baseline": left,
        "candidate": right,
        "intended_variables": list(intended_variables),
        "comparable": False,
        "comparability": "BLOCKED",
        "fingerprint_status": "UNAVAILABLE",
        "warnings": [],
        "reasons": [],
    }
    if evaluation_mode not in (AGENT_REGRESSION, MODEL_UPGRADE) or any(
        p["policy_version"] != POLICY_VERSION
        or p["evaluation_mode"] != evaluation_mode
        or not p["comparable"]
        for p in (left, right)
    ):
        result["reasons"].append("SUBJECT_IDENTITY_BLOCKED")
        return result
    lm, rm = baseline["manifest"], candidate["manifest"]
    for key in ("tool_profile_digest", "output_schema_digest"):
        if lm[key] != rm[key]:
            equivalent_tools = False
            if key == "tool_profile_digest":
                payloads = [
                    baseline.get("tool_identity"),
                    candidate.get("tool_identity"),
                ]
                if all(
                    isinstance(p, dict)
                    and digest(p) == m[key]
                    and p.get("agent_id") == m["agent_id"]
                    and p.get("model_profile") == m["model_profile_id"]
                    for p, m in zip(payloads, (lm, rm))
                ):
                    capabilities = [
                        {
                            k: v
                            for k, v in p.items()
                            if k not in ("agent_id", "model_profile")
                        }
                        for p in payloads
                    ]
                    equivalent_tools = capabilities[0] == capabilities[1]
            if not equivalent_tools:
                result["reasons"].append(key.upper() + "_MISMATCH")
    if baseline["input_digest"] != candidate["input_digest"]:
        result["reasons"].append("INPUT_BINDING_MISMATCH")
    differences = [
        key
        for key, a, b in (
            ("provider", left["actual_provider"], right["actual_provider"]),
            ("canonical_model", left["actual_model"], right["actual_model"]),
            ("model_profile", lm["model_profile_digest"], rm["model_profile_digest"]),
        )
        if a != b
    ]
    if evaluation_mode == AGENT_REGRESSION:
        if differences or intended_variables:
            result["reasons"].append("AGENT_REGRESSION_MODEL_IDENTITY_MISMATCH")
    elif (
        set(intended_variables) - {"provider", "canonical_model", "model_profile"}
        or not set(differences) <= set(intended_variables)
        or lm["prompt_digest"] != rm["prompt_digest"]
    ):
        result["reasons"].append("MODEL_UPGRADE_VARIABLES_NOT_FROZEN")
    lf, rf = left["system_fingerprint"], right["system_fingerprint"]
    if lf and rf:
        result["fingerprint_status"] = (
            "BACKEND_FINGERPRINT_MATCH" if lf == rf else "BACKEND_CONFIGURATION_CHANGED"
        )
        if lf != rf:
            result["warnings"].append("BACKEND_CONFIGURATION_CHANGED")
    if not result["reasons"]:
        result["comparable"] = True
        result["comparability"] = (
            "COMPARABLE_WITH_ENVIRONMENT_WARNING"
            if result["warnings"]
            else "COMPARABLE"
        )
    return result
