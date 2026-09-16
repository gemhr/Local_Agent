"""Stage8-WP1 typed specialist contract tests."""

import hashlib
import json

import pytest
from sqlalchemy import select

from core.stage8 import (
    BusinessReviewService,
    EvidenceRef,
    EvidenceSourceType,
    FeatureContext,
    FeatureUnderstandingRequest,
    RiskAnalysisRequest,
    RiskAnalysisResult,
    RiskItem,
    MissionService,
    SpecialistAgentApplicationService,
    TestPlanRepository as Stage8TestPlanRepository,
    TestPlanningRequest as PlanningRequest,
    TestPlanResult as PlanResult,
    TestScenario as Scenario,
    FeatureUnderstandingResult,
)
from core.persistence.models import Stage8TestPlanRow


def _context():
    return FeatureContext(
        feature_id="chat-1", feature_document="支持消息撤回",
        code_diff="+ revoke_message()", affected_modules=["messaging"],
        retrieval_evidence=[EvidenceRef(evidence_id="rag-1", source_type="RAG", source_ref="kb:1", summary="历史撤回缺少权限校验")],
    )


@pytest.mark.asyncio
async def test_valid_output_is_typed_and_invalid_output_repairs_once():
    calls = []
    async def runner(agent, prompt):
        calls.append((agent, prompt))
        if len(calls) == 1:
            return "not json"
        return '{"feature_id":"chat-1","summary":"撤回消息","change_points":[],"affected_components":[],"clarifications":[],"known_constraints":[],"evidence":[]}'

    result = await SpecialistAgentApplicationService(runner=runner).feature_understanding(
        FeatureUnderstandingRequest(context=_context())
    )
    assert isinstance(result, FeatureUnderstandingResult)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_runtime_run_id_is_attached_to_mission_reference():
    class Scope:
        run_id = "run-specialist-1"
        driver = type("Driver", (), {"output": '{"feature_id":"chat-1","summary":"ok","change_points":[],"affected_components":[],"clarifications":[],"known_constraints":[],"evidence":[]}'})()

        async def execute(self):
            return None

        async def close(self):
            return None

    class Factory:
        async def create_static_run_scope(self, agent_id, prompt, persist):
            assert agent_id == "feature_understanding"
            assert persist is False
            return Scope()

    class Missions:
        references = []

        async def attach_run_reference(self, mission_id, run_id, run_purpose):
            self.references.append((mission_id, run_id, run_purpose))

    missions = Missions()
    await SpecialistAgentApplicationService(
        Factory(), mission_service=missions
    ).feature_understanding(FeatureUnderstandingRequest(
        mission_id="mission-1", context=_context()
    ))
    assert missions.references == [
        ("mission-1", "run-specialist-1", "feature_understanding")
    ]


@pytest.mark.asyncio
async def test_invalid_output_twice_fails():
    async def runner(agent, prompt):
        return "{}"
    with pytest.raises(Exception, match="structured output invalid"):
        await SpecialistAgentApplicationService(runner=runner).feature_understanding(
            FeatureUnderstandingRequest(context=_context())
        )


def test_risk_requires_evidence_and_confidence_range():
    with pytest.raises(ValueError):
        RiskItem(risk_id="r1", severity="HIGH", affected_component="api", failure_mode="denied", evidence=[], recommended_test_focus=["auth"], confidence=.8)
    with pytest.raises(ValueError):
        RiskItem(risk_id="r1", severity="HIGH", affected_component="api", failure_mode="denied", evidence=[EvidenceRef(evidence_id="e", source_type=EvidenceSourceType.CODE_DIFF, source_ref="diff", summary="x")], recommended_test_focus=["auth"], confidence=1.1)


@pytest.mark.asyncio
async def test_fake_evidence_is_rejected_after_one_repair():
    calls = []

    async def runner(agent, prompt):
        calls.append((agent, prompt))
        return '{"feature_id":"chat-1","risks":[{"risk_id":"r1","severity":"HIGH","affected_component":"api","failure_mode":"denied","evidence":[{"evidence_id":"fake","source_type":"CODE_DIFF","source_ref":"fake-ticket","summary":"invented"}],"recommended_test_focus":["auth"],"confidence":0.8}]}'

    request = RiskAnalysisRequest(
        feature_context=_context(),
        feature_understanding=FeatureUnderstandingResult(feature_id="chat-1", summary="x"),
    )
    with pytest.raises(Exception, match="unknown evidence_id"):
        await SpecialistAgentApplicationService(runner=runner).risk_analysis(request)
    assert len(calls) == 2
    assert calls[0][0] == "risk_analysis"


@pytest.mark.asyncio
async def test_repaired_evidence_uses_authoritative_source_identity():
    calls = []

    async def runner(agent, prompt):
        calls.append((agent, prompt))
        evidence_id = "fake" if len(calls) == 1 else "rag-1"
        return f'{{"feature_id":"chat-1","risks":[{{"risk_id":"r1","severity":"HIGH","affected_component":"api","failure_mode":"denied","evidence":[{{"evidence_id":"{evidence_id}","source_type":"CODE_DIFF","source_ref":"model-owned","summary":"model interpretation"}}],"recommended_test_focus":["auth"],"confidence":0.8}}]}}'

    result = await SpecialistAgentApplicationService(runner=runner).risk_analysis(
        RiskAnalysisRequest(
            feature_context=_context(),
            feature_understanding=FeatureUnderstandingResult(feature_id="chat-1", summary="x"),
        )
    )
    evidence = result.risks[0].evidence[0]
    assert len(calls) == 2
    assert evidence.source_type is EvidenceSourceType.RAG
    assert evidence.source_ref == "kb:1"
    assert evidence.summary == "model interpretation"


def test_risk_ids_must_be_unique_within_result():
    evidence = EvidenceRef(
        evidence_id="e", source_type="CODE_DIFF", source_ref="diff", summary="x"
    )
    risk = dict(
        risk_id="r1", severity="HIGH", affected_component="api",
        failure_mode="denied", evidence=[evidence],
        recommended_test_focus=["auth"], confidence=.8,
    )
    with pytest.raises(ValueError, match="risk_id must be unique"):
        RiskAnalysisResult(feature_id="chat-1", risks=[RiskItem(**risk), RiskItem(**risk)])


@pytest.mark.asyncio
async def test_test_planning_must_reference_actual_risk_ids():
    async def runner(agent, prompt):
        return '{"subject_id":"plan-1","version":1,"summary":"plan","scenarios":[{"scenario_id":"s1","title":"negative","category":"NEGATIVE","covered_risk_ids":["unknown"],"test_focus":"auth","priority":"HIGH"}]}'
    understanding = FeatureUnderstandingResult(feature_id="chat-1", summary="x")
    risk = RiskAnalysisResult(feature_id="chat-1", risks=[RiskItem(risk_id="r1", severity="HIGH", affected_component="api", failure_mode="denied", evidence=[EvidenceRef(evidence_id="e", source_type="CODE_DIFF", source_ref="diff", summary="x")], recommended_test_focus=["auth"], confidence=.8)])
    with pytest.raises(Exception, match="unknown risk_id"):
        await SpecialistAgentApplicationService(runner=runner).test_planning(PlanningRequest(feature_understanding=understanding, risk_analysis=risk))


@pytest.mark.asyncio
async def test_test_planning_accepts_known_risk_coverage():
    async def runner(agent, prompt):
        assert agent == "test_planning"
        return '{"subject_id":"plan-1","version":1,"summary":"plan","scenarios":[{"scenario_id":"s1","title":"negative","category":"NEGATIVE","covered_risk_ids":["r1"],"test_focus":"auth","priority":"HIGH"}]}'

    understanding = FeatureUnderstandingResult(feature_id="chat-1", summary="x")
    risk = RiskAnalysisResult(feature_id="chat-1", risks=[RiskItem(risk_id="r1", severity="HIGH", affected_component="api", failure_mode="denied", evidence=[EvidenceRef(evidence_id="e", source_type="CODE_DIFF", source_ref="diff", summary="x")], recommended_test_focus=["auth"], confidence=.8)])
    result = await SpecialistAgentApplicationService(runner=runner).test_planning(
        PlanningRequest(feature_understanding=understanding, risk_analysis=risk)
    )
    assert result.scenarios[0].covered_risk_ids == ["r1"]


@pytest.mark.asyncio
async def test_test_planning_rejects_zero_coverage_after_one_repair():
    calls = []

    async def runner(agent, prompt):
        calls.append((agent, prompt))
        return '{"subject_id":"plan-1","version":1,"summary":"plan","scenarios":[{"scenario_id":"s1","title":"negative","category":"NEGATIVE","covered_risk_ids":[],"test_focus":"auth","priority":"HIGH"}]}'

    understanding = FeatureUnderstandingResult(feature_id="chat-1", summary="x")
    risk = RiskAnalysisResult(feature_id="chat-1", risks=[RiskItem(risk_id="r1", severity="HIGH", affected_component="api", failure_mode="denied", evidence=[EvidenceRef(evidence_id="e", source_type="CODE_DIFF", source_ref="diff", summary="x")], recommended_test_focus=["auth"], confidence=.8)])
    with pytest.raises(Exception, match="at least one risk_id"):
        await SpecialistAgentApplicationService(runner=runner).test_planning(
            PlanningRequest(feature_understanding=understanding, risk_analysis=risk)
        )
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_new_test_plan_version_must_start_at_one():
    calls = []

    async def runner(agent, prompt):
        calls.append((agent, prompt))
        return '{"subject_id":"plan-1","version":2,"summary":"plan","scenarios":[{"scenario_id":"s1","title":"negative","category":"NEGATIVE","covered_risk_ids":[],"test_focus":"auth","priority":"HIGH"}]}'

    with pytest.raises(Exception, match="version must be 1"):
        await SpecialistAgentApplicationService(runner=runner).test_planning(
            PlanningRequest(
                feature_understanding=FeatureUnderstandingResult(
                    feature_id="chat-1", summary="x"
                ),
                risk_analysis=RiskAnalysisResult(feature_id="chat-1"),
            )
        )
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_planning_workflow_persists_review_bound_test_plan(clean_database):
    outputs = iter((
        '{"feature_id":"chat-1","summary":"revoke","change_points":[],"affected_components":["messaging"],"clarifications":[],"known_constraints":[],"evidence":[{"evidence_id":"context:chat-1:code_diff","source_type":"RAG","source_ref":"model-owned","summary":"adds revoke"}]}',
        '{"feature_id":"chat-1","summary":"risk","risks":[{"risk_id":"r1","severity":"HIGH","affected_component":"messaging","failure_mode":"unauthorized revoke","evidence":[{"evidence_id":"context:chat-1:code_diff","source_type":"RAG","source_ref":"model-owned","summary":"permission path"}],"recommended_test_focus":["authorization"],"confidence":0.9}]}',
        '{"subject_id":"plan-1","version":1,"summary":"plan","scenarios":[{"scenario_id":"s1","title":"unauthorized revoke","category":"NEGATIVE","covered_risk_ids":["r1"],"test_focus":"authorization","priority":"HIGH"}]}',
    ))

    async def runner(agent, prompt):
        return next(outputs)

    missions = MissionService(clean_database)
    mission = await missions.create_mission("chat-1")
    service = SpecialistAgentApplicationService(
        runner=runner,
        mission_service=missions,
        review_service=BusinessReviewService(clean_database),
        test_plan_repository=Stage8TestPlanRepository(clean_database),
    )

    response = await service.planning_workflow(FeatureUnderstandingRequest(
        mission_id=mission.mission_id,
        context=_context(),
    ))

    async with clean_database.session() as session:
        stored = await session.scalar(select(Stage8TestPlanRow).where(
            Stage8TestPlanRow.subject_id == "plan-1"
        ))
    assert stored is not None
    canonical = json.dumps(
        stored.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    assert stored.mission_id == mission.mission_id
    assert stored.version == response["review"].subject_version == 1
    assert stored.subject_digest == hashlib.sha256(canonical).hexdigest()
    assert stored.subject_digest == response["review"].subject_digest
    assert response["review"].status.value == "PENDING"
    assert response["mission"].status.value == "AWAITING_REVIEW"


@pytest.mark.asyncio
async def test_planning_workflow_rejects_wrong_mission_feature_before_model_call(clean_database):
    calls = []

    async def runner(agent, prompt):
        calls.append((agent, prompt))
        raise AssertionError("model must not run for a mismatched mission")

    missions = MissionService(clean_database)
    mission = await missions.create_mission("another-feature")
    service = SpecialistAgentApplicationService(
        runner=runner,
        mission_service=missions,
        review_service=BusinessReviewService(clean_database),
        test_plan_repository=Stage8TestPlanRepository(clean_database),
    )
    with pytest.raises(Exception, match="mission feature binding mismatch"):
        await service.planning_workflow(FeatureUnderstandingRequest(
            mission_id=mission.mission_id,
            context=_context(),
        ))
    assert calls == []
