"""固定小计划的纯业务规则；模拟时间不影响 Runtime 时钟。"""
from copy import deepcopy
from datetime import datetime, time, timedelta, timezone
from core.ci_simulation.execution import initial_state, case_catalog, PRODUCT_RELEASES

BUSINESS_TZ = timezone(timedelta(hours=8))
PLAN_KEY = "small-nightly"
PROFILES = {
    "parser": "report_pipeline_failure", "configuration": "environment_configuration_failure",
    "input": "stale_case_input", "buffer": "buffer_behavior_change", "missing": "limited_diagnostics",
}
ENVIRONMENTS = [dict(environment_id=name, ip=f"192.0.2.{21 + i}", profile_key=profile)
    for i, (name, profile) in enumerate([
        ("env-parser-a", "parser"), ("env-parser-b", "parser"), ("env-config", "configuration"),
        ("env-input", "input"), ("env-buffer", "buffer"), ("env-evidence", "missing")])]
VERSIONS = list(PRODUCT_RELEASES)
DURATIONS = {"normal": 60, "slow": 80}


def scheduled_start(business_date):
    return datetime.combine(business_date, time(), BUSINESS_TZ)


def expand_environment(selection):
    env = next(e for e in ENVIRONMENTS if e["environment_id"] == selection.environment_id)
    state = initial_state(PROFILES[selection.profile_key])
    state["environment"].update(environment_id=env["environment_id"], ip=env["ip"])
    primary, companion = case_catalog(state)
    primary.update(payload_matches_golden=state["report"]["payload_matches_golden"],
        report_field_revision=state["report"]["field_revision"])
    companion["report_field_revision"] = companion["required_field_revision"]
    versions = []
    for software in VERSIONS:
        shared = deepcopy(state["environment"])
        shared.update(software_version=software, buffer_behavior=PRODUCT_RELEASES[software]["buffer_behavior"])
        slots = []
        for case, other in ((primary, companion), (companion, primary)):
            snapshot = dict(environment=deepcopy(shared), test_case=deepcopy(case), regression_companion=deepcopy(other),
                report=dict(report_artifact_id="report-input", field_revision=case["report_field_revision"],
                    payload_matches_golden=case["payload_matches_golden"]), diagnostics_complete=state["diagnostics_complete"])
            slots.append(dict(case_id=case["case_id"], initial_state=snapshot, duration_minutes=DURATIONS[selection.duration_profile]))
        versions.append(dict(software_version=software, frozen_cases=slots))
    return dict(environment_id=env["environment_id"], ip=env["ip"], owner_user_id=str(selection.owner_user_id), profile_key=selection.profile_key,
        duration_profile=selection.duration_profile, versions=versions)


def case_due(version, index):
    return version.started_business_at + timedelta(minutes=sum(s["duration_minutes"] for s in version.frozen_cases[:index + 1]))
