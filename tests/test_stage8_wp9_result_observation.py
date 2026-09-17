"""Stage8-WP9：平台观察、确定性日志归一化与既有结果入口复用。"""

import asyncio
import hashlib
import json

import pytest

from core.stage8 import (
    BusinessReviewService, ExternalExecutionStatus,
    FailureTriageResult, MissionService, MissionStatus, Stage8ExecutionService,
    Stage8ValidationError, TestPlanRepository as Stage8TestPlanRepository,
)
from core.stage8 import repositories as stage8_repo
from core.stage8.execution import FailureTriageService
from core.stage8.observation import ExecutionResultParser


async def _prepare_job(database, mission_id="m-wp9"):
    missions = MissionService(database)
    await missions.create_mission("FEATURE-001", mission_id=mission_id)
    await missions.transition_mission(mission_id, MissionStatus.CONTEXT_READY, 1)
    await missions.transition_mission(mission_id, MissionStatus.AWAITING_REVIEW, 2)
    payload = {
        "subject_id": "plan-wp9", "version": 1, "summary": "wp9",
        "scenarios": [{"scenario_id": "case-1", "title": "case", "category": "NEGATIVE"}],
        "environment_requirements": {"version": "1", "network_type": "isolated", "hardware_type": "linux", "required_capabilities": ["CASE_EXECUTION"]},
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    plan = await Stage8TestPlanRepository(database).save(mission_id, "plan-wp9", 1, digest, payload)
    reviews = BusinessReviewService(database)
    review = await reviews.create_review(mission_id, "TEST_PLAN", subject_id=plan.subject_id, subject_version=1, subject_digest=digest)
    await reviews.approve_review(review.review_id, mission_id, subject_id=plan.subject_id, subject_version=1, subject_digest=digest)
    async with database.transaction() as session:
        artifact = await stage8_repo.add_generated_case_artifact(session, {
            "artifact_id": f"artifact-{mission_id}", "mission_id": mission_id,
            "test_plan_subject_id": plan.subject_id, "test_plan_version": 1,
            "test_plan_digest": digest, "scenario_id": "case-1",
            "provider_case_id": "CASE-WP9", "case_path": "/cases/CASE-WP9", "status": "GENERATED",
        })
    await missions.transition_mission(mission_id, MissionStatus.READY_FOR_EXECUTION, 3)
    return missions, artifact


def _invoker(status, result, calls):
    async def invoke(name, payload):
        calls.append(name)
        if name == "stage8_search_environments":
            return [{"environment_id": "ENV-001", "version": "1", "network_type": "isolated", "board": "linux", "status": "FREE", "ip": "10.0.0.1", "capabilities": ["CASE_EXECUTION"], "feature_flags": []}]
        if name == "stage8_get_environment":
            return {"environment_id": "ENV-001", "version": "1", "network_type": "isolated", "board": "linux", "status": "FREE", "ip": "10.0.0.1", "capabilities": ["CASE_EXECUTION"], "feature_flags": []}
        if name == "stage8_start_execution":
            return {"execution_id": "EXEC-WP9", "status": "RUNNING"}
        if name == "stage8_get_execution_status":
            return {"execution_id": "EXEC-WP9", "status": status}
        if name == "stage8_get_execution_result":
            return result
        raise AssertionError(name)
    return invoke


@pytest.mark.asyncio
async def test_running_observation_does_not_mutate_job_or_mission(clean_database):
    missions, artifact = await _prepare_job(clean_database)
    calls = []
    service = Stage8ExecutionService(clean_database, tool_invoker=_invoker("RUNNING", {}, calls))
    job = await service.start_execution("m-wp9", generated_case_artifact_id=artifact.artifact_id)
    observed = await service.observe_once(job.job_id)
    assert observed.status is ExternalExecutionStatus.RUNNING
    assert (await missions.get_mission("m-wp9")).status is MissionStatus.EXECUTING
    assert "stage8_get_execution_result" not in calls


@pytest.mark.asyncio
async def test_success_observation_reuses_canonical_ingestion(clean_database):
    missions, artifact = await _prepare_job(clean_database, "m-wp9-success")
    result = {"execution_id": "EXEC-WP9", "result_location": "provider://result/1", "ready": True, "lines": ["STATUS=SUCCESS"]}
    service = Stage8ExecutionService(clean_database, tool_invoker=_invoker("COMPLETED", result, []))
    job = await service.start_execution("m-wp9-success", generated_case_artifact_id=artifact.artifact_id)
    observed = await service.observe_once(job.job_id)
    assert observed.status is ExternalExecutionStatus.SUCCEEDED
    assert (await missions.get_mission("m-wp9-success")).status is MissionStatus.COMPLETED


@pytest.mark.asyncio
async def test_failed_observation_is_idempotent_and_triages_once(clean_database):
    missions, artifact = await _prepare_job(clean_database, "m-wp9-failed")
    result = {"execution_id": "EXEC-WP9", "result_location": "provider://result/2", "ready": True, "lines": ["STATUS=FAILED", "ERROR_CODE=ASSERTION", "ERROR_MESSAGE=bad result", "FAILED_STEP=assert"]}
    class Triage:
        calls = 0
        async def failure_triage(self, request):
            self.calls += 1
            return FailureTriageResult(classification="ENVIRONMENT", confidence=0.9, evidence_ids=["EXEC_RESULT"], root_cause_hypothesis="bad result", recommended_action="RETRY", severity="HIGH")
    triage = Triage()
    service = Stage8ExecutionService(clean_database, tool_invoker=_invoker("FAILED", result, []), triage_service=FailureTriageService(triage))
    job = await service.start_execution("m-wp9-failed", generated_case_artifact_id=artifact.artifact_id)
    first = await service.observe_once(job.job_id)
    second = await service.observe_once(job.job_id)
    assert first.status is second.status is ExternalExecutionStatus.FAILED
    assert first.result["error_code"] == "ASSERTION"
    assert triage.calls == 1
    assert (await missions.get_mission("m-wp9-failed")).status is MissionStatus.TRIAGING


@pytest.mark.asyncio
async def test_terminal_provider_without_result_keeps_job_running(clean_database):
    missions, artifact = await _prepare_job(clean_database, "m-wp9-not-ready")
    service = Stage8ExecutionService(
        clean_database,
        tool_invoker=_invoker("COMPLETED", {"execution_id": "EXEC-WP9", "ready": False}, []),
    )
    job = await service.start_execution("m-wp9-not-ready", generated_case_artifact_id=artifact.artifact_id)
    observed = await service.observe_once(job.job_id)
    assert observed.status is ExternalExecutionStatus.RUNNING
    assert (await missions.get_mission("m-wp9-not-ready")).status is MissionStatus.EXECUTING


@pytest.mark.asyncio
async def test_malformed_terminal_log_never_defaults_to_success(clean_database):
    missions, artifact = await _prepare_job(clean_database, "m-wp9-malformed")
    service = Stage8ExecutionService(
        clean_database,
        tool_invoker=_invoker("COMPLETED", {"execution_id": "EXEC-WP9", "result_location": "provider://result/3", "ready": True, "lines": ["error happened"]}, []),
    )
    job = await service.start_execution("m-wp9-malformed", generated_case_artifact_id=artifact.artifact_id)
    observed = await service.observe_once(job.job_id)
    assert observed.status is ExternalExecutionStatus.RUNNING
    assert (await missions.get_mission("m-wp9-malformed")).status is MissionStatus.EXECUTING


def test_parser_fail_closed_and_bounds_log_excerpt():
    parser = ExecutionResultParser(max_bytes=32)
    assert parser.parse("EXEC", ["ERROR=not a result"]) is None
    result = parser.parse("EXEC", ["STATUS=SUCCESS", "X=" + "a" * 100], result_location="provider://x")
    assert result is not None
    assert len(result.log_excerpt.encode()) <= 32


@pytest.mark.asyncio
async def test_provider_terminal_state_does_not_override_parser_or_first_result(clean_database):
    _, artifact = await _prepare_job(clean_database, "m-wp9-conflict")
    calls = []
    result = {
        "execution_id": "EXEC-WP9",
        "result_location": "provider://result/conflict",
        "ready": True,
        "lines": ["STATUS=SUCCESS"],
    }
    service = Stage8ExecutionService(
        clean_database, tool_invoker=_invoker("FAILED", result, calls)
    )
    job = await service.start_execution(
        "m-wp9-conflict", generated_case_artifact_id=artifact.artifact_id
    )

    first = await service.observe_once(job.job_id)
    second = await service.observe_once(job.job_id)

    assert first.status is second.status is ExternalExecutionStatus.SUCCEEDED
    assert calls.count("stage8_get_execution_status") == 1
    assert calls.count("stage8_get_execution_result") == 1


@pytest.mark.asyncio
async def test_provider_response_identity_mismatch_fails_closed(clean_database):
    _, artifact = await _prepare_job(clean_database, "m-wp9-id-mismatch")

    async def invoke(name, payload):
        if name == "stage8_search_environments":
            return [{"environment_id": "ENV-001", "version": "1", "network_type": "isolated", "board": "linux", "status": "FREE", "ip": "10.0.0.1", "capabilities": ["CASE_EXECUTION"], "feature_flags": []}]
        if name == "stage8_get_environment":
            return {"environment_id": "ENV-001", "version": "1", "network_type": "isolated", "board": "linux", "status": "FREE", "ip": "10.0.0.1", "capabilities": ["CASE_EXECUTION"], "feature_flags": []}
        if name == "stage8_start_execution":
            return {"execution_id": "EXEC-WP9", "status": "RUNNING"}
        if name == "stage8_get_execution_status":
            return {"execution_id": "EXEC-OTHER", "status": "COMPLETED"}
        raise AssertionError(name)

    service = Stage8ExecutionService(clean_database, tool_invoker=invoke)
    job = await service.start_execution(
        "m-wp9-id-mismatch", generated_case_artifact_id=artifact.artifact_id
    )
    with pytest.raises(Stage8ValidationError, match="identity mismatch"):
        await service.observe_once(job.job_id)


@pytest.mark.asyncio
async def test_concurrent_failed_observation_triages_once(clean_database):
    _, artifact = await _prepare_job(clean_database, "m-wp9-concurrent")
    result_reads = 0
    both_reading = asyncio.Event()

    async def invoke(name, payload):
        nonlocal result_reads
        if name == "stage8_search_environments":
            return [{"environment_id": "ENV-001", "version": "1", "network_type": "isolated", "board": "linux", "status": "FREE", "ip": "10.0.0.1", "capabilities": ["CASE_EXECUTION"], "feature_flags": []}]
        if name == "stage8_get_environment":
            return {"environment_id": "ENV-001", "version": "1", "network_type": "isolated", "board": "linux", "status": "FREE", "ip": "10.0.0.1", "capabilities": ["CASE_EXECUTION"], "feature_flags": []}
        if name == "stage8_start_execution":
            return {"execution_id": "EXEC-WP9", "status": "RUNNING"}
        if name == "stage8_get_execution_status":
            return {"execution_id": "EXEC-WP9", "status": "COMPLETED"}
        if name == "stage8_get_execution_result":
            result_reads += 1
            if result_reads == 2:
                both_reading.set()
            await asyncio.wait_for(both_reading.wait(), timeout=2)
            return {
                "execution_id": "EXEC-WP9",
                "result_location": "provider://result/concurrent",
                "ready": True,
                "lines": ["STATUS=FAILED", "ERROR_CODE=ASSERTION"],
            }
        raise AssertionError(name)

    class Triage:
        calls = 0

        async def failure_triage(self, request):
            self.calls += 1
            return FailureTriageResult(
                classification="ENVIRONMENT", confidence=0.9,
                evidence_ids=["EXEC_RESULT"], root_cause_hypothesis="bad result",
                recommended_action="RETRY", severity="HIGH",
            )

    triage = Triage()
    service = Stage8ExecutionService(
        clean_database,
        tool_invoker=invoke,
        triage_service=FailureTriageService(triage),
    )
    job = await service.start_execution(
        "m-wp9-concurrent", generated_case_artifact_id=artifact.artifact_id
    )

    first, second = await asyncio.gather(
        service.observe_once(job.job_id), service.observe_once(job.job_id)
    )

    assert first.status is second.status is ExternalExecutionStatus.FAILED
    assert triage.calls == 1
