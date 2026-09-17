"""Stage8-WP8 环境感知执行桥接的定向回归。"""

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from core.stage8 import (
    BusinessReviewService,
    ExecutionListBuilder,
    MissionService,
    MissionStatus,
    ResourceUnavailable,
    Stage8ExecutionService,
    Stage8ValidationError,
    TestPlanRepository as _TestPlanRepository,
)
from core.stage8 import repositories as stage8_repo
from core.stage8.specialists import TestPlanResult as _TestPlanResult, TestScenario as _TestScenario
from server import Stage8ExecutionStartRequest


def _requirements_payload(**overrides):
    result = {
        "version": "1",
        "network_type": "isolated",
        "hardware_type": "linux",
        "required_capabilities": ["CASE_EXECUTION"],
        "feature_flags": [],
    }
    result.update(overrides)
    return result


def _plan_payload(subject_id: str, version: int, requirements=None):
    return {
        "subject_id": subject_id,
        "version": version,
        "summary": "WP8 plan",
        "scenarios": [{
            "scenario_id": "scenario-1",
            "title": "execution",
            "category": "FUNCTIONAL",
            "covered_risk_ids": [],
            "test_focus": "execution is accepted",
            "priority": "HIGH",
        }],
        "environment_requirements": (
            requirements if requirements is not None else _requirements_payload()
        ),
        "regression_scope": [],
        "risk_coverage": [],
    }


async def _ready_mission_with_artifact(
    database,
    *,
    mission_id: str = "mission-wp8",
    subject_id: str = "plan-wp8",
    version: int = 1,
    requirements=None,
    artifact_id: str = "artifact-wp8",
):
    mission = await MissionService(database).create_mission("FEATURE-001", mission_id=mission_id)
    await MissionService(database).transition_mission(mission_id, MissionStatus.CONTEXT_READY, 1)
    await MissionService(database).transition_mission(mission_id, MissionStatus.AWAITING_REVIEW, 2)
    payload = _plan_payload(subject_id, version, requirements)
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    plan = await _TestPlanRepository(database).save(mission_id, subject_id, version, digest, payload)
    reviews = BusinessReviewService(database)
    review = await reviews.create_review(
        mission_id,
        "TEST_PLAN",
        subject_id=subject_id,
        subject_version=version,
        subject_digest=digest,
    )
    await reviews.approve_review(
        review.review_id,
        mission_id,
        subject_id=subject_id,
        subject_version=version,
        subject_digest=digest,
    )
    await MissionService(database).transition_mission(mission_id, MissionStatus.READY_FOR_EXECUTION, 3)
    async with database.transaction() as session:
        artifact = await stage8_repo.add_generated_case_artifact(session, {
            "artifact_id": artifact_id,
            "mission_id": mission_id,
            "test_plan_subject_id": subject_id,
            "test_plan_version": version,
            "test_plan_digest": digest,
            "scenario_id": "scenario-1",
            "provider_case_id": "CASE-GEN-001",
            "case_path": "/generated/cases/CASE-GEN-001",
            "status": "GENERATED",
        })
    return mission, plan, artifact


def _environment(
    environment_id: str,
    *,
    status: str = "FREE",
    ip: str = "10.0.0.1",
    capabilities=None,
):
    return {
        "environment_id": environment_id,
        "version": "1",
        "network_type": "isolated",
        "board": "linux",
        "status": status,
        "ip": ip,
        "capabilities": capabilities or ["CASE_EXECUTION"],
        "feature_flags": [],
    }


@pytest.mark.asyncio
async def test_start_request_only_accepts_generated_artifact_id():
    body = Stage8ExecutionStartRequest.model_validate({"generated_case_artifact_id": "artifact-1"})
    assert body.generated_case_artifact_id == "artifact-1"
    with pytest.raises(ValidationError):
        Stage8ExecutionStartRequest.model_validate({
            "generated_case_artifact_id": "artifact-1",
            "case_path": "/caller/override",
        })
    with pytest.raises(ValidationError):
        Stage8ExecutionStartRequest.model_validate({
            "generated_case_artifact_id": "artifact-1",
            "parameters": {"command": "caller-controlled"},
        })


@pytest.mark.asyncio
async def test_stale_artifact_is_rejected_before_environment_query(clean_database):
    _, _, artifact = await _ready_mission_with_artifact(clean_database)
    payload = _plan_payload("plan-current", 2)
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    await _TestPlanRepository(clean_database).save("mission-wp8", "plan-current", 2, digest, payload)
    calls = []

    async def invoke(name, payload):
        calls.append(name)
        raise AssertionError("stale artifact must fail before Tool Runtime")

    service = Stage8ExecutionService(clean_database, tool_invoker=invoke)
    with pytest.raises(Stage8ValidationError, match="current approved TestPlan"):
        await service.start_execution(
            "mission-wp8", generated_case_artifact_id=artifact.artifact_id
        )
    assert calls == []


@pytest.mark.asyncio
async def test_running_job_replay_still_rejects_stale_artifact(clean_database, tmp_path):
    _, _, artifact = await _ready_mission_with_artifact(clean_database)
    calls = []

    async def invoke(name, payload):
        calls.append(name)
        if name == "stage8_search_environments":
            return [_environment("ENV-01")]
        if name == "stage8_get_environment":
            return _environment("ENV-01")
        return {"execution_id": "EXEC-WP8-STALE", "status": "RUNNING"}

    service = Stage8ExecutionService(
        clean_database,
        tool_invoker=invoke,
        execution_list_builder=ExecutionListBuilder(tmp_path),
    )
    await service.start_execution(
        "mission-wp8", generated_case_artifact_id=artifact.artifact_id
    )
    payload = _plan_payload("plan-current", 2)
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    await _TestPlanRepository(clean_database).save(
        "mission-wp8", "plan-current", 2, digest, payload
    )

    with pytest.raises(Stage8ValidationError, match="current approved TestPlan"):
        await service.start_execution(
            "mission-wp8", generated_case_artifact_id=artifact.artifact_id
        )
    assert calls == [
        "stage8_search_environments",
        "stage8_get_environment",
        "stage8_start_execution",
    ]


def test_environment_requirements_are_part_of_test_plan_digest():
    common = {
        "subject_id": "plan",
        "version": 1,
        "summary": "plan",
        "scenarios": [_TestScenario(
            scenario_id="s1", title="case", category="FUNCTIONAL", test_focus="run", priority="HIGH"
        )],
    }
    plan_a = _TestPlanResult.model_validate({**common, "environment_requirements": _requirements_payload()})
    plan_b = _TestPlanResult.model_validate({**common, "environment_requirements": _requirements_payload(version="2")})
    digest_a = hashlib.sha256(json.dumps(plan_a.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    digest_b = hashlib.sha256(json.dumps(plan_b.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert digest_a != digest_b


@pytest.mark.asyncio
async def test_recheck_busy_candidate_falls_back_deterministically(clean_database, tmp_path):
    _, _, artifact = await _ready_mission_with_artifact(clean_database)
    calls = []

    async def invoke(name, payload):
        calls.append((name, payload))
        if name == "stage8_search_environments":
            return [
                _environment("ENV-00", capabilities=["OTHER"]),
                _environment("ENV-01"),
                _environment("ENV-02", ip="10.0.0.2"),
            ]
        if name == "stage8_get_environment":
            return _environment(payload["environment_id"], status="BUSY" if payload["environment_id"] == "ENV-01" else "FREE", ip="10.0.0.2")
        return {"execution_id": "EXEC-WP8-1", "status": "RUNNING"}

    service = Stage8ExecutionService(
        clean_database,
        tool_invoker=invoke,
        execution_list_builder=ExecutionListBuilder(tmp_path),
    )
    job = await service.start_execution("mission-wp8", generated_case_artifact_id=artifact.artifact_id)
    assert job.environment_id == "ENV-02"
    assert [payload["environment_id"] for name, payload in calls if name == "stage8_get_environment"] == ["ENV-01", "ENV-02"]
    start = next(payload for name, payload in calls if name == "stage8_start_execution")
    assert start["case_path"] == "/generated/cases/CASE-GEN-001"
    assert start["environment_id"] == "ENV-02"


@pytest.mark.asyncio
async def test_no_free_environment_never_starts_external_execution(clean_database, tmp_path):
    _, _, artifact = await _ready_mission_with_artifact(clean_database)
    calls = []

    async def invoke(name, payload):
        calls.append(name)
        if name == "stage8_search_environments":
            return [_environment("ENV-01", status="BUSY")]
        raise AssertionError("no-resource path must not call get/start")

    service = Stage8ExecutionService(
        clean_database,
        tool_invoker=invoke,
        execution_list_builder=ExecutionListBuilder(tmp_path),
    )
    result = await service.start_execution("mission-wp8", generated_case_artifact_id=artifact.artifact_id)
    assert isinstance(result, ResourceUnavailable)
    assert "stage8_start_execution" not in calls
    assert (await MissionService(clean_database).get_mission("mission-wp8")).status is MissionStatus.WAITING_FOR_RESOURCE


@pytest.mark.asyncio
async def test_unspecified_requirements_fail_closed_before_environment_query(clean_database):
    _, _, artifact = await _ready_mission_with_artifact(
        clean_database, requirements={}
    )
    calls = []

    async def invoke(name, payload):
        calls.append(name)
        raise AssertionError("unspecified requirements must fail before Tool Runtime")

    result = await Stage8ExecutionService(
        clean_database, tool_invoker=invoke
    ).start_execution(
        "mission-wp8", generated_case_artifact_id=artifact.artifact_id
    )
    assert isinstance(result, ResourceUnavailable)
    assert result.no_match_reason == "RESOURCE_REQUIREMENTS_UNSPECIFIED"
    assert calls == []


@pytest.mark.asyncio
async def test_pending_job_is_durable_before_provider_side_effect(clean_database, tmp_path):
    _, _, artifact = await _ready_mission_with_artifact(clean_database)
    observed = []

    async def invoke(name, payload):
        if name == "stage8_search_environments":
            return [_environment("ENV-01")]
        if name == "stage8_get_environment":
            return _environment("ENV-01")
        async with clean_database.session() as session:
            jobs = await stage8_repo.list_execution_jobs(session, "mission-wp8")
            mission = await stage8_repo.get_mission(session, "mission-wp8")
            observed.append((
                [(job.status, job.environment_id, job.plan_payload["execution_list_ref"]) for job in jobs],
                mission.status,
            ))
        return {"execution_id": "EXEC-WP8-PENDING", "status": "RUNNING"}

    service = Stage8ExecutionService(
        clean_database,
        tool_invoker=invoke,
        execution_list_builder=ExecutionListBuilder(tmp_path),
    )
    await service.start_execution("mission-wp8", generated_case_artifact_id=artifact.artifact_id)
    assert observed == [([("PENDING", "ENV-01", observed[0][0][0][2])], "READY_FOR_EXECUTION")]
    assert observed[0][0][0][2].endswith(".xls")


@pytest.mark.asyncio
async def test_same_request_replays_but_different_parameters_do_not_false_replay(clean_database, tmp_path):
    _, _, artifact = await _ready_mission_with_artifact(clean_database)
    starts = []

    async def invoke(name, payload):
        if name == "stage8_search_environments":
            return [_environment("ENV-01")]
        if name == "stage8_get_environment":
            return _environment("ENV-01")
        starts.append(payload)
        return {"execution_id": f"EXEC-WP8-{len(starts)}", "status": "RUNNING"}

    service = Stage8ExecutionService(
        clean_database,
        tool_invoker=invoke,
        execution_list_builder=ExecutionListBuilder(tmp_path),
    )
    first = await service.start_execution(
        "mission-wp8", generated_case_artifact_id=artifact.artifact_id, parameters={"mode": "A"}
    )
    replay = await service.start_execution(
        "mission-wp8", generated_case_artifact_id=artifact.artifact_id, parameters={"mode": "A"}
    )
    assert replay.job_id == first.job_id
    assert len(starts) == 1
    with pytest.raises(Stage8ValidationError, match="not ready for execution"):
        await service.start_execution(
            "mission-wp8", generated_case_artifact_id=artifact.artifact_id, parameters={"mode": "B"}
        )
    assert len(starts) == 1


def test_execution_list_is_spreadsheetml_and_repeatable(tmp_path):
    artifact = type("Artifact", (), {
        "artifact_id": "artifact-1",
        "case_path": "/generated/cases/CASE-1",
    })()
    builder = ExecutionListBuilder(tmp_path)
    first = builder.build("mission-1", artifact, "digest-a")
    second = builder.build("mission-1", artifact, "digest-a")
    third = builder.build("mission-1", artifact, "digest-b")
    assert first == second
    assert first != third
    content = open(first, encoding="utf-8").read()
    assert "urn:schemas-microsoft-com:office:spreadsheet" in content
    assert "/generated/cases/CASE-1" in content
    assert ">TRUE</Data>" in content
