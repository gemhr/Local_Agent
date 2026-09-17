import asyncio
import hashlib
import json

import pytest
from pydantic import ValidationError

from core.stage8 import BusinessReviewService, MissionService, TestPlanRepository as Stage8TestPlanRepository
from core.stage8.case_generation import CaseGenerationApplicationService
from core.stage8.platforms import (
    CaseGenerationRequest,
    DeterministicMockPlatform,
    case_generation_idempotency_key,
)
from core.stage8.service import Stage8ValidationError
from core.runtime.durable_approval import DurableApprovalService
from core.runtime.run_control import DurableRunControlService
from core.runtime.tool_idempotency import DurableToolInvocationService
from core.runtime import ToolExecutionService
from core.runtime.tool_governance import ToolGovernanceService, ToolPolicyCatalog, register_default_tool_policies
from core.runtime.tool_registry import ToolRegistry
from core.runtime.agent_registry import DEFAULT_AGENT_REGISTRY
from core.stage8.execution import GovernedToolInvoker
from server import Stage8CaseGenerationRequest
from tools.registry import build_builtin_tool_registrations


class _NoResourceAuthorization:
    def extract(self, invocation):
        return None

    def require_authorized(self, request):
        raise AssertionError("case generation must not declare filesystem resources")


def _payload(subject_id="plan-1", scenario_id="s1"):
    return {
        "subject_id": subject_id,
        "version": 1,
        "summary": "reviewed plan",
        "scenarios": [{
            "scenario_id": scenario_id,
            "title": "authorization",
            "category": "NEGATIVE",
            "covered_risk_ids": ["risk-1"],
            "test_focus": "unauthorized request is rejected",
            "priority": "HIGH",
        }],
        "environment_requirements": [],
        "regression_scope": [],
        "risk_coverage": ["risk-1"],
    }


async def _approved_plan(database, mission_id, *, subject_id="plan-1", version=1):
    payload = _payload(subject_id=subject_id)
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    await Stage8TestPlanRepository(database).save(mission_id, subject_id, version, digest, payload)
    reviews = BusinessReviewService(database)
    review = await reviews.create_review(mission_id, "TEST_PLAN", subject_id=subject_id, subject_version=version, subject_digest=digest)
    await reviews.approve_review(review.review_id, mission_id, subject_id=subject_id, subject_version=version, subject_digest=digest)
    return digest


@pytest.mark.asyncio
async def test_approved_scenario_generates_and_persists_artifact(clean_database):
    mission = await MissionService(clean_database).create_mission("FEATURE-001", mission_id="m-wp7")
    await _approved_plan(clean_database, mission.mission_id)
    platform = DeterministicMockPlatform.seeded()
    calls = []

    async def invoke(name, payload, *, principal_agent_id):
        calls.append((name, payload, principal_agent_id))
        return platform.generate_case(CaseGenerationRequest(**payload))

    service = CaseGenerationApplicationService(clean_database, tool_invoker=invoke)
    artifacts = await service.generate(mission.mission_id)
    assert artifacts[0].provider_case_id == "CASE-GEN-001"
    assert artifacts[0].case_path == "/generated/cases/CASE-GEN-001"
    assert calls[0][0] == "stage8_generate_case"
    assert calls[0][2] == "test_planning"
    assert len(await service.list(mission.mission_id)) == 1


@pytest.mark.asyncio
async def test_current_plan_review_and_scenario_binding_are_required(clean_database):
    mission = await MissionService(clean_database).create_mission("FEATURE-001", mission_id="m-wp7-gate")
    await _approved_plan(clean_database, mission.mission_id, subject_id="old-plan")
    payload = _payload(subject_id="new-plan")
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    await Stage8TestPlanRepository(clean_database).save(mission.mission_id, "new-plan", 2, digest, payload)
    async def invoke(*args, **kwargs):
        raise AssertionError("stale approval must block before Tool Runtime")
    service = CaseGenerationApplicationService(clean_database, tool_invoker=invoke)
    with pytest.raises(Stage8ValidationError, match="approved current TestPlan"):
        await service.generate(mission.mission_id)

    reviews = BusinessReviewService(clean_database)
    review = await reviews.create_review(
        mission.mission_id, "TEST_PLAN", subject_id="new-plan", subject_version=2, subject_digest=digest
    )
    await reviews.approve_review(
        review.review_id, mission.mission_id,
        subject_id="new-plan", subject_version=2, subject_digest=digest,
    )
    with pytest.raises(Stage8ValidationError, match="does not belong"):
        await service.generate(mission.mission_id, ["missing-scenario"])


@pytest.mark.asyncio
async def test_scenario_selection_rejects_empty_and_duplicate_ids(clean_database):
    mission = await MissionService(clean_database).create_mission(
        "FEATURE-001", mission_id="m-wp7-selection"
    )
    await _approved_plan(clean_database, mission.mission_id)

    async def invoke(*args, **kwargs):
        raise AssertionError("invalid scenario selection must fail before Tool Runtime")

    service = CaseGenerationApplicationService(clean_database, tool_invoker=invoke)
    with pytest.raises(Stage8ValidationError, match="must not be empty"):
        await service.generate(mission.mission_id, [])
    with pytest.raises(Stage8ValidationError, match="must not contain duplicates"):
        await service.generate(mission.mission_id, ["s1", "s1"])


@pytest.mark.asyncio
async def test_plan_payload_rejects_duplicate_scenario_ids(clean_database):
    mission = await MissionService(clean_database).create_mission(
        "FEATURE-001", mission_id="m-wp7-plan-duplicates"
    )
    payload = _payload()
    payload["scenarios"].append(dict(payload["scenarios"][0]))
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    await Stage8TestPlanRepository(clean_database).save(
        mission.mission_id, "plan-1", 1, digest, payload
    )
    reviews = BusinessReviewService(clean_database)
    review = await reviews.create_review(
        mission.mission_id, "TEST_PLAN", subject_id="plan-1",
        subject_version=1, subject_digest=digest,
    )
    await reviews.approve_review(
        review.review_id, mission.mission_id, subject_id="plan-1",
        subject_version=1, subject_digest=digest,
    )

    async def invoke(*args, **kwargs):
        raise AssertionError("invalid TestPlan must fail before Tool Runtime")

    service = CaseGenerationApplicationService(clean_database, tool_invoker=invoke)
    with pytest.raises(Stage8ValidationError, match="duplicate scenario_id"):
        await service.generate(mission.mission_id)


@pytest.mark.asyncio
async def test_same_binding_replays_durable_artifact_and_provider_result(clean_database):
    mission = await MissionService(clean_database).create_mission("FEATURE-001", mission_id="m-wp7-replay")
    await _approved_plan(clean_database, mission.mission_id)
    platform = DeterministicMockPlatform.seeded()
    calls = 0

    async def invoke(name, payload, *, principal_agent_id):
        nonlocal calls
        calls += 1
        from core.stage8.platforms import CaseGenerationRequest
        return platform.generate_case(CaseGenerationRequest(**payload))

    service = CaseGenerationApplicationService(clean_database, tool_invoker=invoke)
    first = await service.generate(mission.mission_id)
    second = await service.generate(mission.mission_id)
    assert first[0].artifact_id == second[0].artifact_id
    assert first[0].provider_case_id == second[0].provider_case_id
    assert calls == 1
    assert len(platform.generated_cases) == 1


def test_provider_replays_same_canonical_request_with_key():
    platform = DeterministicMockPlatform.seeded()
    request = CaseGenerationRequest(
        feature_id="FEATURE-001", mission_id="m", test_plan_subject_id="p",
        test_plan_version=1, test_plan_digest="d" * 64, scenario_id="s",
        scenario_description="scenario", expected_behavior="pass",
    )
    key = case_generation_idempotency_key(request)

    first, first_replayed = platform.generate_case_with_key(request, key)
    second, second_replayed = platform.generate_case_with_key(request, key)

    assert first == second
    assert first_replayed is False
    assert second_replayed is True
    assert len(platform.generated_cases) == 1


def test_http_request_rejects_caller_supplied_provider_identity():
    for forbidden_field in ("provider_case_id", "case_path"):
        with pytest.raises(ValidationError):
            Stage8CaseGenerationRequest.model_validate(
                {"scenario_ids": ["s1"], forbidden_field: "caller-controlled"}
            )


@pytest.mark.asyncio
async def test_concurrent_duplicate_returns_one_durable_artifact(clean_database):
    mission = await MissionService(clean_database).create_mission(
        "FEATURE-001", mission_id="m-wp7-concurrent"
    )
    await _approved_plan(clean_database, mission.mission_id)
    platform = DeterministicMockPlatform.seeded()
    both_invocations_started = asyncio.Event()
    invocation_count = 0

    async def invoke(name, payload, *, principal_agent_id):
        nonlocal invocation_count
        invocation_count += 1
        if invocation_count == 2:
            both_invocations_started.set()
        await asyncio.wait_for(both_invocations_started.wait(), timeout=2)
        return platform.generate_case(CaseGenerationRequest(**payload))

    service = CaseGenerationApplicationService(clean_database, tool_invoker=invoke)
    first, second = await asyncio.gather(
        service.generate(mission.mission_id),
        service.generate(mission.mission_id),
    )

    assert first[0].artifact_id == second[0].artifact_id
    assert first[0].provider_case_id == second[0].provider_case_id
    assert invocation_count == 2
    assert len(platform.generated_cases) == 1
    assert len(await service.list(mission.mission_id)) == 1


@pytest.mark.asyncio
async def test_case_generation_uses_governed_tool_runtime(clean_database):
    platform = DeterministicMockPlatform.seeded()
    registry = ToolRegistry()
    for registration in build_builtin_tool_registrations(platform):
        registry.register(registration)
    registry.freeze()
    catalog = ToolPolicyCatalog(tool_registry=registry, agent_registry=DEFAULT_AGENT_REGISTRY)
    register_default_tool_policies(catalog)
    catalog.freeze()
    invoker = GovernedToolInvoker(
        registry, ToolGovernanceService(catalog, DEFAULT_AGENT_REGISTRY),
        ToolExecutionService(durable_invocation_service=DurableToolInvocationService(clean_database)),
        resource_authorization=_NoResourceAuthorization(),
        durable_run_control=DurableRunControlService(clean_database),
        durable_approval=DurableApprovalService(clean_database),
        owner_id="stage8-wp7-test",
    )
    result = await invoker(
        "stage8_generate_case",
        CaseGenerationRequest(
            feature_id="FEATURE-001", mission_id="m", test_plan_subject_id="p",
            test_plan_version=1, test_plan_digest="d" * 64, scenario_id="s",
            scenario_description="scenario", expected_behavior="pass",
        ).model_dump(mode="json"),
        principal_agent_id="test_planning",
    )
    assert result["provider_case_id"] == "CASE-GEN-001"
    assert len(platform.generated_cases) == 1
