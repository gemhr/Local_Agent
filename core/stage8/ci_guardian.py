"""Stage8-WP4 CI Guardian：确定性聚类/对比/关联 + 共享 Specialist。"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator
from core.stage8 import repositories as repo
from core.stage8.service import Stage8NotFoundError, Stage8ValidationError


class CIRunStatus(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    RUNNING = "RUNNING"


class ChangeType(StrEnum):
    FEATURE_MERGE = "FEATURE_MERGE"
    CODE_COMMIT = "CODE_COMMIT"
    TOOL_RELEASE = "TOOL_RELEASE"
    CASE_CHANGE = "CASE_CHANGE"
    ENVIRONMENT_CHANGE = "ENVIRONMENT_CHANGE"
    CONFIG_CHANGE = "CONFIG_CHANGE"
    EXECUTOR_CHANGE = "EXECUTOR_CHANGE"


class CIExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    execution_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    status: str = Field(pattern="^(PASS|FAILED|SKIPPED|ERROR)$")
    failure_signature: str | None = None
    classification: str | None = None
    environment_id: str = Field(min_length=1)
    executor_id: str = Field(min_length=1)
    duration: float | None = Field(default=None, ge=0)
    completed_at: datetime
    affected_components: list[str] = Field(default_factory=list)
    logs: list[str] = Field(default_factory=list)


class CIRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ci_run_id: str = Field(min_length=1)
    suite_id: str = Field(min_length=1)
    started_at: datetime
    completed_at: datetime
    status: CIRunStatus
    branch: str = Field(min_length=1)
    executions: list[CIExecutionResult] = Field(min_length=1)
    related_feature_ids: list[str] = Field(default_factory=list)
    change_timeline: list["ChangeEvent"] = Field(default_factory=list)


class ChangeEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    change_id: str = Field(min_length=1)
    change_type: ChangeType
    occurred_at: datetime
    source_ref: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    affected_components: list[str] = Field(default_factory=list)
    feature_id: str | None = None
    version: str | None = None


class FailureCluster(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    cluster_id: str
    signature: str
    classification: str | None = None
    execution_ids: list[str]
    case_ids: list[str]
    failure_count: int
    affected_environments: list[str]
    affected_components: list[str]
    first_seen_at: datetime
    last_seen_at: datetime


class HistoricalComparison(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    cluster_id: str
    current_count: int
    previous_count: int
    first_seen: datetime
    last_seen: datetime
    is_new: bool
    delta: int
    regression_candidate: bool


class ChangeCorrelationCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    change_id: str
    time_distance: float
    component_overlap: list[str]
    reason: str


class CIGuardianRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    current_run_id: str
    clusters: list[FailureCluster]
    comparisons: list[HistoricalComparison]
    change_candidates: list[ChangeCorrelationCandidate]
    evidence_ids: list[str]


class CIGuardianFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    cluster_id: str
    severity: str = Field(pattern="^(LOW|MEDIUM|HIGH|CRITICAL)$")
    suspected_change_ids: list[str] = Field(default_factory=list)
    root_cause_hypothesis: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(default_factory=list)
    recommended_action: str = Field(min_length=1)


class CIGuardianResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    summary: str
    health_status: str = Field(pattern="^(HEALTHY|DEGRADED|CRITICAL|UNKNOWN)$")
    findings: list[CIGuardianFinding] = Field(default_factory=list)
    @model_validator(mode="after")
    def unique_findings(self):
        ids = [x.cluster_id for x in self.findings]
        if len(ids) != len(set(ids)):
            raise ValueError("cluster_id must be unique")
        return self


def _signature(item: CIExecutionResult) -> str:
    # Signature is an authority supplied by the CI result, not model output.
    # Collapse insignificant whitespace so equivalent values share one cluster.
    signature = " ".join(item.failure_signature.split()).upper() if item.failure_signature else ""
    if signature:
        return signature
    classification = " ".join((item.classification or "UNKNOWN").split()).upper() or "UNKNOWN"
    return f"{classification}:UNKNOWN"


def _components(values: list[str]) -> set[str]:
    """Return stable component keys (case/whitespace normalized, deduplicated)."""
    return {normalized for value in values if (normalized := " ".join(value.split()).upper())}


def cluster_failures(run: CIRun) -> list[FailureCluster]:
    groups: dict[str, list[CIExecutionResult]] = {}
    for item in run.executions:
        if item.status == "FAILED":
            groups.setdefault(_signature(item), []).append(item)
    result = []
    for signature, items in sorted(groups.items()):
        components = sorted({c for item in items for c in _components(item.affected_components)})
        result.append(FailureCluster(
            cluster_id=f"CLUSTER-{hashlib.sha256(signature.encode()).hexdigest()[:12]}",
            signature=signature, classification=next((x.classification for x in items if x.classification), None),
            execution_ids=[x.execution_id for x in items], case_ids=[x.case_id for x in items],
            failure_count=len(items), affected_environments=sorted({x.environment_id for x in items}),
            affected_components=components, first_seen_at=min(x.completed_at for x in items),
            last_seen_at=max(x.completed_at for x in items),
        ))
    return result


def compare_history(current: list[FailureCluster], previous: list[FailureCluster]) -> list[HistoricalComparison]:
    old = {x.signature: x for x in previous}
    return [HistoricalComparison(
        cluster_id=x.cluster_id, current_count=x.failure_count,
        previous_count=old.get(x.signature, FailureCluster.model_construct(failure_count=0)).failure_count,
        first_seen=x.first_seen_at, last_seen=x.last_seen_at,
        is_new=x.signature not in old, delta=x.failure_count - (old.get(x.signature).failure_count if x.signature in old else 0),
        regression_candidate=x.signature not in old or x.failure_count > old[x.signature].failure_count,
    ) for x in current]


def correlate_changes(clusters: list[FailureCluster], run: CIRun, previous_completed_at: datetime | None) -> list[ChangeCorrelationCandidate]:
    start = previous_completed_at or run.started_at
    candidates = []
    for change in run.change_timeline:
        if not (start < change.occurred_at <= run.completed_at):
            continue
        for cluster in clusters:
            overlap = sorted(set(cluster.affected_components) & _components(change.affected_components))
            if overlap:
                distance = abs((cluster.first_seen_at - change.occurred_at).total_seconds())
                candidates.append(ChangeCorrelationCandidate(
                    change_id=change.change_id, time_distance=distance,
                    component_overlap=overlap, reason="time-window and affected-component overlap",
                ))
    return candidates


class CIGuardianApplicationService:
    def __init__(self, database, specialist):
        self.database = database
        self.specialist = specialist

    async def ingest_ci_run(self, run: CIRun) -> CIRun:
        payload = run.model_dump(mode="json")
        async with self.database.transaction() as session:
            if await repo.get_ci_run(session, run.ci_run_id):
                raise Stage8ValidationError("ci_run_id already exists")
            await repo.add_ci_run(session, {"ci_run_id": run.ci_run_id, "suite_id": run.suite_id, "started_at": run.started_at, "completed_at": run.completed_at, "status": run.status.value, "branch": run.branch, "payload": payload})
        return run

    async def analyze(self, ci_run_id: str) -> CIGuardianResult:
        # Lock the immutable run row for the complete check/compute/insert unit.
        # This makes the one-analysis-per-run UNIQUE constraint a real idempotency
        # contract even when two requests arrive at the same time.
        async with self.database.transaction() as session:
            row = await repo.get_ci_run(session, ci_run_id, for_update=True)
            if row is None:
                raise Stage8NotFoundError("ci run not found")
            existing = await repo.get_ci_analysis(session, ci_run_id)
            if existing is not None:
                return CIGuardianResult.model_validate(existing.payload["result"])

            run = CIRun.model_validate(row.payload)
            rows = await repo.list_ci_runs(session, suite_id=row.suite_id)
            prior = [
                CIRun.model_validate(candidate.payload)
                for candidate in rows
                if candidate.ci_run_id != ci_run_id
                and candidate.status in {
                    CIRunStatus.COMPLETED.value,
                    CIRunStatus.FAILED.value,
                }
                and candidate.completed_at < run.completed_at
            ]
            previous = max(prior, key=lambda candidate: (candidate.completed_at, candidate.ci_run_id), default=None)
            clusters = cluster_failures(run)
            comparisons = compare_history(clusters, cluster_failures(previous)) if previous else compare_history(clusters, [])
            candidates = correlate_changes(clusters, run, previous.completed_at if previous else None)
            evidence_ids = [f"CI_RUN:{run.ci_run_id}"] + [f"CI_EXECUTION:{x.execution_id}" for x in run.executions] + [f"CI_CLUSTER:{x.cluster_id}" for x in clusters] + [f"CHANGE:{x.change_id}" for x in run.change_timeline]
            request = CIGuardianRequest(current_run_id=ci_run_id, clusters=clusters, comparisons=comparisons, change_candidates=candidates, evidence_ids=evidence_ids)
            result = await self.specialist.ci_guardian(request)
            candidate_ids = {x.change_id for x in candidates}
            if any(set(f.suspected_change_ids) - candidate_ids for f in result.findings) or any(set(f.evidence_ids) - set(evidence_ids) for f in result.findings):
                raise Stage8ValidationError("guardian output references unknown change or evidence id")
            payload = {"request": request.model_dump(mode="json"), "result": result.model_dump(mode="json")}
            digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            await repo.add_ci_analysis(session, {"analysis_id": uuid.uuid4().hex, "ci_run_id": ci_run_id, "version": 1, "digest": digest, "payload": payload})
            return result

    async def get_analysis(self, ci_run_id: str) -> CIGuardianResult:
        async with self.database.session() as session:
            row = await repo.get_ci_analysis(session, ci_run_id)
        if row is None:
            raise Stage8NotFoundError("ci analysis not found")
        return CIGuardianResult.model_validate(row.payload["result"])
