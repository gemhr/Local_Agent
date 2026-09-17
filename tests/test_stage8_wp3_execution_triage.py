"""Stage8-WP3 最小异步执行与失败分流闭环。"""

import hashlib
import json

import pytest

from core.stage8 import (
    BusinessReviewService,
    ExecutionResult,
    ExternalExecutionStatus,
    FailureTriageResult,
    MissionService,
    MissionStatus,
    ReviewStatus,
    Stage8ExecutionService,
    GovernedToolInvoker,
    FailureTriageRequest,
    SpecialistAgentApplicationService,
    TestPlanRepository as Stage8TestPlanRepository,
    Stage8ValidationError,
)
from core.stage8 import repositories as stage8_repo
from core.stage8.execution import FailureTriageService
from core.runtime import ToolExecutionService
from core.runtime.agent_registry import DEFAULT_AGENT_REGISTRY
from core.runtime.durable_approval import DurableApprovalService
from core.runtime.run_control import DurableRunControlService
from core.runtime.tool_governance import (
    ToolGovernanceService, ToolPolicyCatalog, register_default_tool_policies,
)
from core.runtime.tool_idempotency import DurableToolInvocationService
from core.runtime.tool_registry import ToolRegistry
from core.stage8.platforms import DeterministicMockPlatform
from tools.registry import build_builtin_tool_registrations


class _Triage:
    def __init__(self):
        self.request = None

    async def failure_triage(self, request):
        self.request = request
        return FailureTriageResult(
            classification="ENVIRONMENT", confidence=0.9, evidence_ids=["EXEC_RESULT"],
            root_cause_hypothesis="product assertion failed",
            recommended_action="CREATE_TICKET", severity="HIGH",
        )


async def _approve_current_test_plan(database, mission_id: str, *, subject_id: str = "plan-wp3", version: int = 1):
    payload = {
        "subject_id": subject_id,
        "version": version,
        "summary": "wp3 plan",
        "scenarios": [{"scenario_id": "case-1", "title": "case", "category": "NEGATIVE"}],
        "environment_requirements": {
            "version": "1",
            "network_type": "isolated",
            "hardware_type": "linux",
            "required_capabilities": ["CASE_EXECUTION"],
        },
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(canonical).hexdigest()
    stored = await Stage8TestPlanRepository(database).save(mission_id, subject_id, version, digest, payload)
    reviews = BusinessReviewService(database)
    review = await reviews.create_review(
        mission_id, "TEST_PLAN", subject_id=stored.subject_id,
        subject_version=stored.version, subject_digest=stored.subject_digest
    )
    await reviews.approve_review(
        review.review_id, mission_id,
        subject_id=stored.subject_id,
        subject_version=stored.version, subject_digest=stored.subject_digest,
    )
    return stored


async def _add_artifact(database, mission_id: str, plan, *, artifact_id: str):
    async with database.transaction() as session:
        return await stage8_repo.add_generated_case_artifact(session, {
            "artifact_id": artifact_id,
            "mission_id": mission_id,
            "test_plan_subject_id": plan.subject_id,
            "test_plan_version": plan.version,
            "test_plan_digest": plan.subject_digest,
            "scenario_id": "case-1",
            "provider_case_id": "CASE-GEN-001",
            "case_path": "/generated/cases/CASE-GEN-001",
            "status": "GENERATED",
        })


def _execution_invoker(execution_id, calls=None):
    async def invoke(name, payload):
        if calls is not None:
            calls.append((name, payload))
        if name == "stage8_search_environments":
            return [{"environment_id": "ENV-001", "version": "1", "network_type": "isolated", "board": "linux", "status": "FREE", "ip": "10.0.0.1", "capabilities": ["CASE_EXECUTION"], "feature_flags": []}]
        if name == "stage8_get_environment":
            return {"environment_id": "ENV-001", "version": "1", "network_type": "isolated", "board": "linux", "status": "FREE", "ip": "10.0.0.1", "capabilities": ["CASE_EXECUTION"], "feature_flags": []}
        return {"execution_id": execution_id, "status": "RUNNING"}
    return invoke


@pytest.mark.asyncio
async def test_start_returns_without_waiting_and_success_completes(clean_database):
    missions = MissionService(clean_database)
    reviews = BusinessReviewService(clean_database)
    await missions.create_mission("FEATURE-001", mission_id="m-wp3")
    await missions.transition_mission("m-wp3", MissionStatus.CONTEXT_READY, 1)
    await missions.transition_mission("m-wp3", MissionStatus.AWAITING_REVIEW, 2)
    plan_row = await _approve_current_test_plan(clean_database, "m-wp3")
    artifact = await _add_artifact(clean_database, "m-wp3", plan_row, artifact_id="artifact-wp3")
    await missions.transition_mission("m-wp3", MissionStatus.READY_FOR_EXECUTION, 3)

    calls = []
    async def invoke(name, payload):
        calls.append((name, payload))
        return await _execution_invoker("EXEC-WP3-1")(name, payload)

    service = Stage8ExecutionService(clean_database, tool_invoker=invoke)
    job = await service.start_execution("m-wp3", generated_case_artifact_id=artifact.artifact_id)
    assert job.execution_id == "EXEC-WP3-1"
    assert [name for name, _ in calls] == [
        "stage8_search_environments", "stage8_get_environment", "stage8_start_execution"
    ]
    assert (await missions.get_mission("m-wp3")).status is MissionStatus.EXECUTING
    replay = await service.start_execution("m-wp3", generated_case_artifact_id=artifact.artifact_id)
    assert replay.job_id == job.job_id
    assert len(calls) == 3

    completed = await service.ingest_result(ExecutionResult("EXEC-WP3-1", ExternalExecutionStatus.SUCCEEDED, "ok"))
    duplicate = await service.ingest_result(ExecutionResult("EXEC-WP3-1", ExternalExecutionStatus.FAILED, "late"))
    assert completed.status is ExternalExecutionStatus.SUCCEEDED
    assert duplicate.status is ExternalExecutionStatus.SUCCEEDED
    assert (await missions.get_mission("m-wp3")).status is MissionStatus.COMPLETED


@pytest.mark.asyncio
async def test_failed_result_builds_authoritative_evidence_and_triages(clean_database):
    missions = MissionService(clean_database)
    reviews = BusinessReviewService(clean_database)
    await missions.create_mission("FEATURE-001", mission_id="m-wp3-f")
    await missions.transition_mission("m-wp3-f", MissionStatus.CONTEXT_READY, 1)
    await missions.transition_mission("m-wp3-f", MissionStatus.AWAITING_REVIEW, 2)
    plan_row = await _approve_current_test_plan(clean_database, "m-wp3-f")
    artifact = await _add_artifact(clean_database, "m-wp3-f", plan_row, artifact_id="artifact-wp3-f")
    await missions.transition_mission("m-wp3-f", MissionStatus.READY_FOR_EXECUTION, 3)
    triage = _Triage()
    service = Stage8ExecutionService(
        clean_database,
        tool_invoker=_execution_invoker("EXEC-WP3-F"),
        triage_service=FailureTriageService(triage),
    )
    await service.start_execution("m-wp3-f", generated_case_artifact_id=artifact.artifact_id)
    job = await service.ingest_result(ExecutionResult("EXEC-WP3-F", ExternalExecutionStatus.FAILED, "assertion failed", expected_result="ok", logs=["process exited 1"]))
    assert job.status is ExternalExecutionStatus.FAILED
    assert job.triage["state"] == "COMPLETED"
    assert job.triage["result"]["classification"] == "ENVIRONMENT"
    assert (await missions.get_mission("m-wp3-f")).status is MissionStatus.TRIAGING
    assert triage.request.evidence["FEATURE_BINDING"]["feature_id"] == "FEATURE-001"
    assert triage.request.evidence["TEST_PLAN_BINDING"]["subject_id"] == "plan-wp3"


@pytest.mark.asyncio
async def test_stale_approved_test_plan_review_cannot_start_current_plan(clean_database):
    missions = MissionService(clean_database)
    reviews = BusinessReviewService(clean_database)
    await missions.create_mission("FEATURE-001", mission_id="m-wp3-binding")
    await missions.transition_mission("m-wp3-binding", MissionStatus.CONTEXT_READY, 1)
    await missions.transition_mission("m-wp3-binding", MissionStatus.AWAITING_REVIEW, 2)
    old = await _approve_current_test_plan(clean_database, "m-wp3-binding", subject_id="plan-old")
    artifact = await _add_artifact(clean_database, "m-wp3-binding", old, artifact_id="artifact-wp3-binding")
    await Stage8TestPlanRepository(clean_database).save(
        "m-wp3-binding", "plan-current", 2, "b" * 64,
        {"subject_id": "plan-current", "version": 2},
    )
    # The old approval remains APPROVED, but it must not authorize the newer subject.
    assert old.subject_digest != "b" * 64
    await missions.transition_mission("m-wp3-binding", MissionStatus.READY_FOR_EXECUTION, 3)
    service = Stage8ExecutionService(
        clean_database,
        tool_invoker=lambda *_: _async_result({"execution_id": "must-not-start"}),
    )
    with pytest.raises(Stage8ValidationError, match="current approved TestPlan"):
        await service.start_execution(
            "m-wp3-binding", generated_case_artifact_id=artifact.artifact_id
        )


class _CountingProductTriage:
    def __init__(self):
        self.calls = 0

    async def failure_triage(self, request):
        self.calls += 1
        return FailureTriageResult(
            classification="PRODUCT", confidence=0.9, evidence_ids=["EXEC_RESULT"],
            root_cause_hypothesis="product assertion failed",
            recommended_action="CREATE_TICKET", severity="HIGH",
            ticket_draft={"title": "wp3 failure", "severity": "HIGH", "description": "assertion failed"},
        )


@pytest.mark.asyncio
async def test_duplicate_failed_callback_does_not_repeat_triage_or_ticket_request(clean_database):
    missions = MissionService(clean_database)
    await missions.create_mission("FEATURE-001", mission_id="m-wp3-duplicate-failure")
    await missions.transition_mission("m-wp3-duplicate-failure", MissionStatus.CONTEXT_READY, 1)
    await missions.transition_mission("m-wp3-duplicate-failure", MissionStatus.AWAITING_REVIEW, 2)
    plan_row = await _approve_current_test_plan(clean_database, "m-wp3-duplicate-failure")
    artifact = await _add_artifact(clean_database, "m-wp3-duplicate-failure", plan_row, artifact_id="artifact-wp3-duplicate")
    await missions.transition_mission("m-wp3-duplicate-failure", MissionStatus.READY_FOR_EXECUTION, 3)

    calls = []
    triage = _CountingProductTriage()

    async def invoke(name, payload):
        calls.append((name, payload))
        if name == "stage8_create_ticket":
            return {"status": "APPROVAL_REQUIRED", "tool_name": name}
        return await _execution_invoker("EXEC-WP3-DUP")(name, payload)

    service = Stage8ExecutionService(
        clean_database,
        tool_invoker=invoke,
        triage_service=FailureTriageService(triage),
    )
    await service.start_execution(
        "m-wp3-duplicate-failure", generated_case_artifact_id=artifact.artifact_id
    )
    failed = ExecutionResult("EXEC-WP3-DUP", ExternalExecutionStatus.FAILED, "assertion failed")
    await service.ingest_result(failed)
    duplicate = await service.ingest_result(failed)

    assert duplicate.status is ExternalExecutionStatus.FAILED
    assert triage.calls == 1
    assert [name for name, _ in calls].count("stage8_create_ticket") == 1


@pytest.mark.asyncio
async def test_unknown_external_result_does_not_enter_failure_triage(clean_database):
    missions = MissionService(clean_database)
    await missions.create_mission("FEATURE-001", mission_id="m-wp3-unknown")
    await missions.transition_mission("m-wp3-unknown", MissionStatus.CONTEXT_READY, 1)
    await missions.transition_mission("m-wp3-unknown", MissionStatus.AWAITING_REVIEW, 2)
    plan_row = await _approve_current_test_plan(clean_database, "m-wp3-unknown")
    artifact = await _add_artifact(clean_database, "m-wp3-unknown", plan_row, artifact_id="artifact-wp3-unknown")
    await missions.transition_mission("m-wp3-unknown", MissionStatus.READY_FOR_EXECUTION, 3)
    service = Stage8ExecutionService(
        clean_database,
        tool_invoker=_execution_invoker("EXEC-WP3-UNKNOWN"),
    )
    await service.start_execution(
        "m-wp3-unknown", generated_case_artifact_id=artifact.artifact_id
    )

    job = await service.ingest_result(
        ExecutionResult("EXEC-WP3-UNKNOWN", ExternalExecutionStatus.UNKNOWN, "status unavailable")
    )
    assert job.status is ExternalExecutionStatus.UNKNOWN
    assert (await missions.get_mission("m-wp3-unknown")).status is MissionStatus.EXECUTING


class _NoResourceAuthorization:
    @staticmethod
    def extract(invocation):
        return None

    @staticmethod
    def require_authorized(request):
        raise AssertionError("Stage8 tools do not declare filesystem resources")


@pytest.mark.asyncio
async def test_governed_invoker_uses_durable_runtime_and_creates_pending_ticket_approval(clean_database):
    platform = DeterministicMockPlatform.seeded()
    registry = ToolRegistry()
    for registration in build_builtin_tool_registrations(platform):
        registry.register(registration)
    registry.freeze()
    catalog = ToolPolicyCatalog(
        tool_registry=registry, agent_registry=DEFAULT_AGENT_REGISTRY
    )
    register_default_tool_policies(catalog)
    catalog.freeze()
    run_control = DurableRunControlService(clean_database)
    approvals = DurableApprovalService(clean_database)
    invoker = GovernedToolInvoker(
        registry,
        ToolGovernanceService(catalog, DEFAULT_AGENT_REGISTRY),
        ToolExecutionService(
            durable_invocation_service=DurableToolInvocationService(clean_database)
        ),
        resource_authorization=_NoResourceAuthorization(),
        durable_run_control=run_control,
        durable_approval=approvals,
        owner_id="stage8-wp3-test",
    )

    started = await invoker(
        "stage8_start_execution",
        {"provider_case_id": "CASE-001", "case_path": "/official/CASE-001", "environment_id": "ENV-001", "environment_ip": "10.0.0.1", "execution_list_ref": "data/stage8/execution_lists/test.xls", "executor_id": "EXECUTOR-001"},
        principal_agent_id="test_planning",
    )
    assert started["execution_id"] == "EXEC-001"

    pending = await invoker(
        "stage8_create_ticket",
        {"title": "failure", "severity": "HIGH", "description": "assertion failed"},
        principal_agent_id="failure_triage",
    )
    assert pending["status"] == "APPROVAL_REQUIRED"
    assert (await approvals.get(pending["approval_id"])).approval_id == pending["approval_id"]
    assert platform.search_tickets("failure") == []


@pytest.mark.asyncio
async def test_failure_triage_rejects_fake_evidence_then_repairs_once():
    outputs = iter((
        '{"classification":"ENVIRONMENT","confidence":0.7,"evidence_ids":["FAKE"],"root_cause_hypothesis":"x","recommended_action":"retry","severity":"MEDIUM"}',
        '{"classification":"ENVIRONMENT","confidence":0.7,"evidence_ids":["EXEC_RESULT"],"root_cause_hypothesis":"x","recommended_action":"retry","severity":"MEDIUM"}',
    ))
    calls = []

    async def runner(agent_id, prompt):
        calls.append((agent_id, prompt))
        return next(outputs)

    result = await SpecialistAgentApplicationService(runner=runner).failure_triage(
        FailureTriageRequest(
            mission_id="mission",
            evidence={"EXEC_RESULT": {"status": "FAILED"}},
            available_evidence=[{"evidence_id": "EXEC_RESULT", "status": "FAILED"}],
        )
    )
    assert result.evidence_ids == ["EXEC_RESULT"]
    assert [agent_id for agent_id, _ in calls] == ["failure_triage", "failure_triage"]


async def _async_result(value):
    return value
