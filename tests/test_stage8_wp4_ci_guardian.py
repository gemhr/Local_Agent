"""Stage8-WP4 CI Guardian 的确定性分析与 Evidence/Change ID 边界。"""

from datetime import datetime, timedelta, timezone

import pytest

from core.stage8 import (
    CIRun, CIRunStatus, CIExecutionResult, ChangeEvent, ChangeType,
    CIGuardianApplicationService, CIGuardianFinding, CIGuardianResult,
    SpecialistAgentApplicationService, Stage8ValidationError,
    cluster_failures, compare_history, correlate_changes,
)


def _item(execution_id, signature, component="RRC", status="FAILED", at=None):
    return CIExecutionResult(
        execution_id=execution_id, case_id=execution_id.replace("EXEC", "CASE"),
        status=status, failure_signature=signature, classification="PRODUCT",
        environment_id="ENV-001", executor_id="RUNNER-001",
        completed_at=at or datetime(2026, 9, 17, 3, tzinfo=timezone.utc),
        affected_components=[component],
    )


def _run(run_id, executions, *, start=1, timeline=None):
    base = datetime(2026, 9, 17, start, tzinfo=timezone.utc)
    return CIRun(
        ci_run_id=run_id, suite_id="nightly", started_at=base,
        completed_at=base + timedelta(hours=2), status=CIRunStatus.COMPLETED,
        branch="main", executions=executions, change_timeline=timeline or [],
    )


def test_wp4_clusters_and_compares_deterministically():
    previous = _run("CI-PREV", [_item("EXEC-P1", "RRC_SETUP_TIMEOUT", at=datetime(2026, 9, 16, 3, tzinfo=timezone.utc))])
    current = _run("CI-CURRENT", [_item("EXEC-1", "RRC_SETUP_TIMEOUT"), _item("EXEC-2", "RRC_SETUP_TIMEOUT"), _item("EXEC-3", "DB_CONNECTION_ERROR", "DB")])
    clusters = cluster_failures(current)
    assert len(clusters) == 2
    assert sorted(x.failure_count for x in clusters) == [1, 2]
    comparison = compare_history(clusters, cluster_failures(previous))
    rrc = next(x for x in comparison if x.cluster_id == next(c for c in clusters if c.signature == "RRC_SETUP_TIMEOUT").cluster_id)
    assert (rrc.current_count, rrc.previous_count, rrc.delta, rrc.regression_candidate) == (2, 1, 1, True)


def test_wp4_change_correlation_requires_window_and_component_overlap():
    at = datetime(2026, 9, 17, 3, tzinfo=timezone.utc)
    run = _run("CI-CURRENT", [_item("EXEC-1", "RRC_SETUP_TIMEOUT", at=at)], start=1, timeline=[
        ChangeEvent(change_id="CHANGE-001", change_type=ChangeType.CODE_COMMIT, occurred_at=datetime(2026, 9, 17, 2, tzinfo=timezone.utc), source_ref="commit:1", summary="RRC change", affected_components=["RRC"]),
        ChangeEvent(change_id="CHANGE-002", change_type=ChangeType.CODE_COMMIT, occurred_at=datetime(2026, 9, 17, 2, tzinfo=timezone.utc), source_ref="commit:2", summary="DB change", affected_components=["DB"]),
    ])
    candidates = correlate_changes(cluster_failures(run), run, datetime(2026, 9, 17, 1, tzinfo=timezone.utc))
    assert [x.change_id for x in candidates] == ["CHANGE-001"]


@pytest.mark.asyncio
async def test_wp4_closed_loop_persists_analysis_and_rejects_fake_ids(clean_database):
    run = _run("CI-CURRENT", [_item("EXEC-1", "RRC_SETUP_TIMEOUT"), _item("EXEC-2", "RRC_SETUP_TIMEOUT")], timeline=[
        ChangeEvent(change_id="CHANGE-001", change_type=ChangeType.CODE_COMMIT, occurred_at=datetime(2026, 9, 17, 2, tzinfo=timezone.utc), source_ref="commit:1", summary="RRC change", affected_components=["RRC"]),
    ])

    async def runner(agent_id, prompt):
        assert agent_id == "ci_guardian"
        return '{"summary":"recent change correlates with failures","health_status":"CRITICAL","findings":[{"cluster_id":"CLUSTER-unknown","severity":"HIGH","suspected_change_ids":["CHANGE-999"],"root_cause_hypothesis":"hypothesis","confidence":0.8,"evidence_ids":["E-FAKE"],"recommended_action":"INVESTIGATE_CHANGE"}]}'

    specialist = SpecialistAgentApplicationService(runner=runner)
    service = CIGuardianApplicationService(clean_database, specialist)
    await service.ingest_ci_run(run)
    with pytest.raises(Stage8ValidationError):
        await service.analyze(run.ci_run_id)


@pytest.mark.asyncio
async def test_wp4_closed_loop_persists_valid_guardian_analysis(clean_database):
    run = _run("CI-VALID", [_item("EXEC-1", "RRC_SETUP_TIMEOUT")], timeline=[
        ChangeEvent(change_id="CHANGE-001", change_type=ChangeType.CODE_COMMIT, occurred_at=datetime(2026, 9, 17, 2, tzinfo=timezone.utc), source_ref="commit:1", summary="RRC change", affected_components=["RRC"]),
    ])

    async def runner(agent_id, prompt):
        request = __import__("json").loads(prompt)["request"]
        cluster_id = request["clusters"][0]["cluster_id"]
        return __import__("json").dumps({"summary":"correlated","health_status":"CRITICAL","findings":[{"cluster_id":cluster_id,"severity":"HIGH","suspected_change_ids":["CHANGE-001"],"root_cause_hypothesis":"recent RRC change may correlate with timeout","confidence":0.8,"evidence_ids":[request["evidence_ids"][0]],"recommended_action":"INVESTIGATE_CHANGE"}]})

    service = CIGuardianApplicationService(clean_database, SpecialistAgentApplicationService(runner=runner))
    await service.ingest_ci_run(run)
    result = await service.analyze(run.ci_run_id)
    stored = await service.get_analysis(run.ci_run_id)
    assert result == stored
    assert stored.findings[0].suspected_change_ids == ["CHANGE-001"]


@pytest.mark.asyncio
async def test_wp4_specialist_repairs_fake_change_and_evidence_once():
    outputs = iter([
        '{"summary":"x","health_status":"DEGRADED","findings":[{"cluster_id":"C","severity":"HIGH","suspected_change_ids":["CHANGE-999"],"root_cause_hypothesis":"x","confidence":0.5,"evidence_ids":["E-FAKE"],"recommended_action":"RE-RUN"}]}',
        '{"summary":"x","health_status":"DEGRADED","findings":[{"cluster_id":"C","severity":"HIGH","suspected_change_ids":[],"root_cause_hypothesis":"x","confidence":0.5,"evidence_ids":["CI_RUN:1"],"recommended_action":"RE-RUN"}]}',
    ])

    async def runner(agent_id, prompt):
        return next(outputs)

    from core.stage8 import CIGuardianRequest, FailureCluster, HistoricalComparison, ChangeCorrelationCandidate
    now = datetime.now(timezone.utc)
    result = await SpecialistAgentApplicationService(runner=runner).ci_guardian(CIGuardianRequest(
        current_run_id="1", clusters=[FailureCluster(cluster_id="C", signature="X", execution_ids=["E"], case_ids=["C"], failure_count=1, affected_environments=["ENV"], affected_components=[], first_seen_at=now, last_seen_at=now)],
        comparisons=[HistoricalComparison(cluster_id="C", current_count=1, previous_count=0, first_seen=now, last_seen=now, is_new=True, delta=1, regression_candidate=True)],
        change_candidates=[], evidence_ids=["CI_RUN:1"],
    ))
    assert result.findings[0].evidence_ids == ["CI_RUN:1"]


@pytest.mark.asyncio
async def test_wp4_specialist_rejects_unknown_cluster_after_one_repair():
    outputs = iter([
        '{"summary":"x","health_status":"DEGRADED","findings":[{"cluster_id":"CLUSTER-999","severity":"HIGH","suspected_change_ids":[],"root_cause_hypothesis":"x","confidence":0.5,"evidence_ids":["CI_RUN:1"],"recommended_action":"HUMAN_REVIEW"}]}',
        '{"summary":"x","health_status":"DEGRADED","findings":[{"cluster_id":"CLUSTER-999","severity":"HIGH","suspected_change_ids":[],"root_cause_hypothesis":"x","confidence":0.5,"evidence_ids":["CI_RUN:1"],"recommended_action":"HUMAN_REVIEW"}]}',
    ])

    async def runner(agent_id, prompt):
        return next(outputs)

    from core.stage8 import CIGuardianRequest, FailureCluster, HistoricalComparison
    now = datetime.now(timezone.utc)
    request = CIGuardianRequest(
        current_run_id="1",
        clusters=[FailureCluster(
            cluster_id="CLUSTER-1", signature="X", execution_ids=["E"],
            case_ids=["C"], failure_count=1, affected_environments=["ENV"],
            affected_components=[], first_seen_at=now, last_seen_at=now,
        )],
        comparisons=[HistoricalComparison(
            cluster_id="CLUSTER-1", current_count=1, previous_count=0,
            first_seen=now, last_seen=now, is_new=True, delta=1,
            regression_candidate=True,
        )],
        change_candidates=[], evidence_ids=["CI_RUN:1"],
    )

    with pytest.raises(Stage8ValidationError, match="unknown cluster_id"):
        await SpecialistAgentApplicationService(runner=runner).ci_guardian(request)


def test_wp4_clusters_only_failed_and_normalizes_missing_signature_and_components():
    failed = _item("EXEC-1", "  RRC   SETUP_TIMEOUT ", component=" rrc ")
    missing = _item("EXEC-2", "   ", component="RRC", status="FAILED")
    error = _item("EXEC-3", "DB_ERROR", component="DB", status="ERROR")
    clusters = cluster_failures(_run("CI-CURRENT", [failed, missing, error]))

    assert [cluster.signature for cluster in clusters] == ["PRODUCT:UNKNOWN", "RRC SETUP_TIMEOUT"]
    assert all("DB" not in cluster.affected_components for cluster in clusters)
    assert all(cluster.affected_components == ["RRC"] for cluster in clusters)


@pytest.mark.asyncio
async def test_wp4_previous_is_latest_completed_same_suite_before_current(clean_database):
    current = _run("CI-CURRENT", [_item("EXEC-C", "TIMEOUT")], start=10)
    old = _run("CI-OLD", [_item("EXEC-O", "TIMEOUT")], start=1)
    previous = _run("CI-PREVIOUS", [_item("EXEC-P1", "TIMEOUT"), _item("EXEC-P2", "TIMEOUT")], start=7).model_copy(update={"status": CIRunStatus.FAILED})
    future = _run("CI-FUTURE", [_item("EXEC-F", "TIMEOUT")], start=13)
    unfinished = _run("CI-UNFINISHED", [_item("EXEC-U", "TIMEOUT")], start=8).model_copy(update={"status": CIRunStatus.RUNNING})
    other_suite = _run("CI-OTHER-SUITE", [_item("EXEC-S", "TIMEOUT")], start=9).model_copy(update={"suite_id": "weekly"})

    async def runner(agent_id, prompt):
        request = __import__("json").loads(prompt)["request"]
        assert request["comparisons"][0]["previous_count"] == 2
        return '{"summary":"ok","health_status":"DEGRADED","findings":[]}'

    service = CIGuardianApplicationService(clean_database, SpecialistAgentApplicationService(runner=runner))
    for run in (old, previous, future, unfinished, other_suite, current):
        await service.ingest_ci_run(run)
    await service.analyze(current.ci_run_id)


@pytest.mark.asyncio
async def test_wp4_duplicate_analyze_is_idempotent(clean_database):
    run = _run("CI-IDEMPOTENT", [_item("EXEC-1", "TIMEOUT")])
    calls = 0

    async def runner(agent_id, prompt):
        nonlocal calls
        calls += 1
        return '{"summary":"stable","health_status":"DEGRADED","findings":[]}'

    service = CIGuardianApplicationService(clean_database, SpecialistAgentApplicationService(runner=runner))
    await service.ingest_ci_run(run)
    first = await service.analyze(run.ci_run_id)
    second = await service.analyze(run.ci_run_id)
    assert first == second
    assert calls == 1
