"""状态驱动的模拟执行；场景只选择初态，不直接决定结果。"""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5
from core.runtime.snapshot_serialization import sha256_digest

SCENARIOS = [
    dict(scenario_key="report_pipeline_failure", title="上报链路失败", description="调查工具兼容与执行证据", template_version="2"),
    dict(scenario_key="environment_configuration_failure", title="环境配置冲突", description="检查共享环境开关", template_version="2"),
    dict(scenario_key="stale_case_input", title="TLV 输入落后", description="检查用例输入与软件要求", template_version="2"),
    dict(scenario_key="buffer_behavior_change", title="产品 Buffer 回归", description="模拟研发交付与关联 Feature 回归", template_version="2"),
    dict(scenario_key="limited_diagnostics", title="诊断记录不完整", description="保留实际证据缺口", template_version="2"),
]
PRODUCT_RELEASES = {
    "product-2.0": dict(required_tlv_revision=2, buffer_behavior="compatible"),
    "product-2.1": dict(required_tlv_revision=2, buffer_behavior="compact"),
    "product-2.1.1": dict(required_tlv_revision=2, buffer_behavior="compatible"),
}


def initial_state(key):
    if key not in {s["scenario_key"] for s in SCENARIOS}:
        raise ValueError("CI_SCENARIO_UNKNOWN")
    state = dict(
        environment=dict(environment_id="env-report", ip="192.0.2.15", software_version="product-2.0",
            parser_version="parser-1.0", decoder_table_version="decoder-1.0",
            parser_supported_field_revision=1, decoder_supported_field_revision=1,
            feature_flags={"compressed_transport": True}, buffer_behavior="compatible"),
        test_case=dict(case_id="case-report-compare", feature_id="feature-report-fields",
            required_field_revision=2, input_revision=2, golden_id="golden-report",
            golden_version="2", comparison_rule_id="semantic-fields-equal/1"),
        report=dict(report_artifact_id="report-input", field_revision=2, payload_matches_golden=True),
        diagnostics_complete=key != "limited_diagnostics")
    env, case = state["environment"], state["test_case"]
    if key in {"environment_configuration_failure", "stale_case_input", "buffer_behavior_change"}:
        env.update(parser_version="parser-2.0", decoder_table_version="decoder-2.0",
            parser_supported_field_revision=2, decoder_supported_field_revision=2)
    if key == "environment_configuration_failure":
        case.update(case_id="case-legacy-report", feature_id="feature-legacy-report", compressed_transport=False)
    elif key == "stale_case_input":
        case.update(case_id="case-tlv-report", feature_id="feature-tlv-report", input_revision=1, input_contract="tlv")
        state["report"]["field_revision"] = 1
    elif key == "buffer_behavior_change":
        env.update(software_version="product-2.1", buffer_behavior="compact")
        case.update(case_id="case-legacy-buffer", feature_id="feature-legacy-buffer", buffer_requirement="legacy")
    return state


def case_catalog(state):
    """旧实例按实际主用例识别关联检查，不回填过去执行。"""
    primary = deepcopy(state["test_case"])
    if primary["case_id"] == "case-legacy-buffer":
        related_id, feature, revision = "case-modern-buffer", "feature-modern-buffer", 2
    elif primary["case_id"] == "case-report-compare":
        related_id, feature, revision = "case-legacy-field-health", "feature-legacy-fields", 1
    else:
        related_id, feature, revision = "case-report-health", "feature-report-health", 2
    related = dict(case_id=related_id, feature_id=feature, required_field_revision=revision,
        input_revision=revision, golden_id="golden-" + related_id, golden_version="1",
        comparison_rule_id="semantic-fields-equal/1", payload_matches_golden=True)
    if related_id == "case-modern-buffer":
        related["buffer_requirement"] = "modern"
    return [primary, related]


def execute(state, execution_id, case_id=None):
    snapshot = deepcopy(state)
    cases = case_catalog(state)
    selected = next((c for c in cases if c["case_id"] == (case_id or cases[0]["case_id"])), None)
    if selected is None:
        raise ValueError("CI_CASE_UNKNOWN")
    snapshot["test_case"] = deepcopy(selected)
    if selected["case_id"] != state["test_case"]["case_id"]:
        snapshot["report"] = dict(report_artifact_id="report-input", field_revision=selected["required_field_revision"],
            payload_matches_golden=selected["payload_matches_golden"])
    env, case, artifact = snapshot["environment"], snapshot["test_case"], snapshot["report"]
    release = PRODUCT_RELEASES[env["software_version"]]
    now = datetime.now(timezone.utc)
    captured = now.isoformat()
    artifact["report_artifact_id"] = execution_id + ":report"
    revision = artifact["field_revision"]
    flag_error = ("compressed_transport" in case and
        env.get("feature_flags", {}).get("compressed_transport", True) != case["compressed_transport"])
    input_error = case.get("input_contract") == "tlv" and (
        case["input_revision"] < release["required_tlv_revision"] or revision != case["input_revision"])
    pre_error = "FEATURE_FLAG_CONFLICT" if flag_error else "TLV_INPUT_REVISION_STALE" if input_error else None
    buffer = env.get("buffer_behavior", release["buffer_behavior"])
    allowed = {"compact": {"modern"}, "legacy": {"legacy"}, "compatible": {"legacy", "modern"}}[buffer]
    product_error = "BUFFER_CONTRACT_MISMATCH" if case.get("buffer_requirement") and case["buffer_requirement"] not in allowed else None
    parser_ran = pre_error is None and product_error is None
    compatible = revision <= env["parser_supported_field_revision"] and revision <= env["decoder_supported_field_revision"]
    parser_error = "UNSUPPORTED_FIELD_REVISION" if parser_ran and not compatible else None
    compared = parser_ran and compatible
    comparison_error = "PARSE_RESULT_EMPTY" if parser_error else "GOLDEN_MISMATCH" if compared and not artifact["payload_matches_golden"] else None
    outcome = "PASS" if compared and artifact["payload_matches_golden"] else "FAIL"
    stages = [dict(stage="PRECHECK", status="EXECUTED", error=pre_error),
        dict(stage="PRODUCT", status="SKIPPED" if pre_error else "EXECUTED", error=product_error if not pre_error else None),
        dict(stage="PARSER", status="EXECUTED" if parser_ran else "SKIPPED", error=parser_error),
        dict(stage="COMPARISON", status="EXECUTED" if parser_ran else "SKIPPED", error=comparison_error)]
    summary = dict(execution_id=execution_id, environment_id=env["environment_id"], case_id=case["case_id"],
        outcome=outcome, completed_at=captured, stages=stages)
    common = dict(execution_id=execution_id, artifact_id=artifact["report_artifact_id"], call_id=execution_id + ":compare")
    parser = dict(**common, field_revision=revision,
        parser_supported_field_revision=env["parser_supported_field_revision"], decoder_supported_field_revision=env["decoder_supported_field_revision"],
        parser_version=env["parser_version"], decoder_table_version=env["decoder_table_version"],
        status="EXECUTED" if parser_ran else "SKIPPED", skipped_reason=pre_error or product_error,
        error=parser_error, parsed_fields={"report": "decoded"} if compared else None)
    complete = snapshot["diagnostics_complete"]
    comparison = dict(**common, caller="COMPARE", callee="PARSER", golden_comparison_performed=compared,
        outcome=outcome, error=comparison_error, skipped_reason=pre_error or product_error) if complete else dict(**common,
        caller="COMPARE", outcome=outcome, detail="比较阶段未获得可用结果；调用详细记录未采集")
    visible_env = deepcopy(env)
    if not complete:
        for field in ("parser_version", "decoder_table_version", "parser_supported_field_revision", "decoder_supported_field_revision"):
            visible_env[field] = None
        visible_env["missing_reason"] = "工具安装与能力记录未采集"
    contents = dict(CI_SUMMARY=summary,
        AUTOMATION_LOG=dict(**common, caller="AUTOMATION", callee="COMPARE", outcome=outcome, stages=stages,
            container_type="simulated-compressed-report", product_release=release),
        COMPARISON_LOG=comparison, PARSER_LOG=parser if complete else None,
        ENVIRONMENT_RECORD=dict(**common, **visible_env), CASE_REQUIREMENTS=dict(**common, **case))
    evidence = [dict(evidence_id=str(uuid5(NAMESPACE_URL, execution_id + ":" + kind)), execution_id=execution_id,
        kind=kind, captured_at=captured, availability="AVAILABLE" if content is not None else "MISSING",
        media_type="application/json", digest=sha256_digest(content) if content is not None else None,
        content=content, missing_reason=None if content is not None else "解析日志未采集") for kind, content in contents.items()]
    return dict(started_at=now, completed_at=now, outcome=outcome, execution_snapshot=snapshot,
        summary=summary, evidence_manifest=evidence,
        evidence_digest=sha256_digest(sorted(evidence, key=lambda e: e["evidence_id"])))
