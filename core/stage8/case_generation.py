"""Stage8-WP7：将 approved TestScenario 委托给 Case Generation Platform。"""

from __future__ import annotations

import uuid

from core.persistence.errors import DatabaseErrorCode, PersistenceError
from core.stage8 import repositories as repo
from core.stage8.domain import GeneratedCaseArtifact
from core.stage8.platforms import CaseGenerationRequest
from core.stage8.service import Stage8ConflictError, Stage8NotFoundError, Stage8ValidationError


def _artifact(row) -> GeneratedCaseArtifact:
    return GeneratedCaseArtifact(
        row.artifact_id, row.mission_id, row.test_plan_subject_id,
        row.test_plan_version, row.test_plan_digest, row.scenario_id,
        row.provider_case_id, row.case_path, row.status, row.created_at,
    )


class CaseGenerationApplicationService:
    """业务事务 Owner；外部 Case creation 只通过 governed Tool invoker 执行。"""

    def __init__(self, database, *, tool_invoker, principal_agent_id: str = "test_planning"):
        self.database = database
        self.tool_invoker = tool_invoker
        self.principal_agent_id = principal_agent_id

    @staticmethod
    def _scenario(plan, scenario_id: str):
        scenarios = plan.payload.get("scenarios", [])
        for scenario in scenarios:
            if scenario.get("scenario_id") == scenario_id:
                return scenario
        raise Stage8ValidationError("scenario does not belong to current TestPlan")

    async def _require_current_approved_binding(self, mission_id: str, plan) -> None:
        async with self.database.session() as session:
            current = await repo.get_current_test_plan(session, mission_id)
            review = await repo.get_approved_test_plan_review(session, mission_id)
        binding = (plan.subject_id, plan.version, plan.subject_digest)
        if (
            current is None
            or review is None
            or (current.subject_id, current.version, current.subject_digest) != binding
            or (review.subject_id, review.subject_version, review.subject_digest) != binding
        ):
            raise Stage8ValidationError("approved current TestPlan review is required")

    async def _load_binding(self, mission_id: str, scenario_ids: list[str] | None):
        async with self.database.session() as session:
            if await repo.get_mission(session, mission_id) is None:
                raise Stage8NotFoundError("mission not found")
            plan = await repo.get_current_test_plan(session, mission_id)
            review = await repo.get_approved_test_plan_review(session, mission_id)
            if plan is None or review is None:
                raise Stage8ValidationError("approved current TestPlan review is required")
            plan_scenario_ids = [
                item["scenario_id"] for item in plan.payload.get("scenarios", [])
            ]
            if len(plan_scenario_ids) != len(set(plan_scenario_ids)):
                raise Stage8ValidationError("current TestPlan contains duplicate scenario_id")
            selected = plan_scenario_ids if scenario_ids is None else scenario_ids
            if not selected:
                raise Stage8ValidationError("scenario_ids must not be empty")
            if len(selected) != len(set(selected)):
                raise Stage8ValidationError("scenario_ids must not contain duplicates")
            # Validate all IDs before any external side effect.
            scenarios = [self._scenario(plan, scenario_id) for scenario_id in selected]
            existing = []
            for scenario_id in selected:
                row = await repo.get_generated_case_artifact(
                    session, mission_id=mission_id, subject_id=plan.subject_id,
                    version=plan.version, digest=plan.subject_digest, scenario_id=scenario_id,
                )
                if row is not None:
                    existing.append(_artifact(row))
            return plan, scenarios, existing

    async def generate(self, mission_id: str, scenario_ids: list[str] | None = None) -> list[GeneratedCaseArtifact]:
        plan, scenarios, existing = await self._load_binding(mission_id, scenario_ids)
        existing_by_scenario = {item.scenario_id: item for item in existing}
        results = list(existing)
        for scenario in scenarios:
            scenario_id = scenario["scenario_id"]
            if scenario_id in existing_by_scenario:
                continue
            await self._require_current_approved_binding(mission_id, plan)
            request = CaseGenerationRequest(
                feature_id=(await self._mission_feature_id(mission_id)),
                mission_id=mission_id,
                test_plan_subject_id=plan.subject_id,
                test_plan_version=plan.version,
                test_plan_digest=plan.subject_digest,
                scenario_id=scenario_id,
                scenario_description=f"{scenario['title']}: {scenario['test_focus']}",
                preconditions=[],
                expected_behavior=scenario["test_focus"],
            )
            raw = await self.tool_invoker(
                "stage8_generate_case", request.model_dump(mode="json"),
                principal_agent_id=self.principal_agent_id,
            )
            if hasattr(raw, "model_dump"):
                raw = raw.model_dump(mode="json")
            if raw.get("status") == "APPROVAL_REQUIRED":
                raise Stage8ValidationError("case generation requires runtime approval")
            if not raw.get("provider_case_id") or not raw.get("case_path"):
                raise Stage8ValidationError("case generation platform returned an invalid result")
            await self._require_current_approved_binding(mission_id, plan)
            values = dict(
                artifact_id=uuid.uuid4().hex, mission_id=mission_id,
                test_plan_subject_id=plan.subject_id, test_plan_version=plan.version,
                test_plan_digest=plan.subject_digest, scenario_id=scenario_id,
                provider_case_id=raw["provider_case_id"], case_path=raw["case_path"],
                status="GENERATED",
            )
            try:
                async with self.database.transaction() as session:
                    row = await repo.add_generated_case_artifact(session, values)
                results.append(_artifact(row))
            except PersistenceError as exc:
                if exc.error_code is not DatabaseErrorCode.DATABASE_INTEGRITY_VIOLATION:
                    raise
                # A concurrent request may have committed the same canonical binding.
                async with self.database.session() as session:
                    row = await repo.get_generated_case_artifact(
                        session, mission_id=mission_id, subject_id=plan.subject_id,
                        version=plan.version, digest=plan.subject_digest, scenario_id=scenario_id,
                    )
                if row is None:
                    raise Stage8ConflictError("generated case artifact persistence conflict")
                results.append(_artifact(row))
        return results

    async def _mission_feature_id(self, mission_id: str) -> str:
        async with self.database.session() as session:
            mission = await repo.get_mission(session, mission_id)
            if mission is None:
                raise Stage8NotFoundError("mission not found")
            return mission.feature_id

    async def list(self, mission_id: str) -> list[GeneratedCaseArtifact]:
        async with self.database.session() as session:
            if await repo.get_mission(session, mission_id) is None:
                raise Stage8NotFoundError("mission not found")
            rows = await repo.list_generated_case_artifacts(session, mission_id)
        return [_artifact(row) for row in rows]
