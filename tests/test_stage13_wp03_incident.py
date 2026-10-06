"""WP03：真实 WP02/Provider 输入、并发预算和不可变证据的高价值验收。"""

import asyncio
from dataclasses import replace
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import func, select, update

from core.persistence.errors import PersistenceError
from core.stage13.aggregation import (
    VisibleFailure,
    choose_representatives,
    incident_key,
    local_key,
    normalize_excerpt,
)
from core.stage13.contracts import EvidencePacket, canonical_bytes, sha256
from core.stage13.incident import IncidentAggregationService
from core.stage13.incident_worker import IncidentWorker
from core.stage13.guardian import StaleClaim
from core.stage13.guardian_models import GuardianRow, VersionRow
from core.stage13.incident_models import (
    AnalysisJobRow,
    BudgetRow,
    ClusterRow,
    CollectionRow,
    IncidentRow,
    MembershipRow,
    RevisionRow,
)
from core.stage13.guardian_worker import build_guardian_invoker
from core.stage13.workload import CHANGES
from tests.test_stage13_wp02_guardian import BASE, Cohort

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / ".ai/handoff/stage13_wp03/evidence"


def save(name, data):
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / name).write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


async def rows(db, model):
    async with db.session() as s:
        return (await s.execute(select(model))).scalars().all()


async def small(db, count=12, errors=False):
    c = Cohort(db)
    if errors:
        # TEST_SCOPE 场景在 Provider submit 之前冻结，未篡改 canonical rows。
        plans = list(c.workload.plans)
        index = next(i for i, p in enumerate(plans) if len(p.failed_cases) > 1)
        case = plans[index].failed_cases[0][0]
        plans[index] = replace(
            plans[index],
            failed_cases=plans[index].failed_cases[1:],
            error_cases=(case,),
        )
        c.workload.plans = tuple(plans)
        manifest = c.workload._build_manifest(None)
        manifest["error_case_count"] = 1
        manifest["manifest_digest"] = sha256(
            canonical_bytes(
                {k: v for k, v in manifest.items() if k != "manifest_digest"}
            )
        )
        c.workload.manifest = manifest
    await c.initialize()
    environment_ids = sorted(
        range(100),
        key=lambda i: (
            -sum(len(p.failed_cases) for p in c.workload.plans[i * 3 : i * 3 + 3]),
            i,
        ),
    )[:count]
    cycles = [(await c.discover(i))[1] for i in environment_ids]
    seconds = 480
    await c.clock(seconds)
    for ordinal in range(3):
        for _ in range(3):
            await c.drain()
            seconds += 1
            await c.clock(seconds)
        for cycle in cycles:
            version = (await c.versions(cycle))[ordinal]
            assert version.status == "ACTIVE"
            await c.remote_terminal(version)
        seconds += 400
        await c.clock(seconds)
        await c.drain()
    service = IncidentAggregationService(db, c.config.owner_scope_id)
    worker = IncidentWorker(
        service,
        build_guardian_invoker(
            db, c.client, c.config, owner_id="wp03-test", failure_details=True
        ),
        enabled=True,
    )
    while await worker.tick():
        pass
    return c, service, worker


def rewrite(page, transform):
    packet = EvidencePacket.model_validate(page)
    body = json.loads(packet.content)
    for case in body["cases"]:
        transform(case)
    content = canonical_bytes(body).decode("utf-8")
    return {
        **page,
        "content": content,
        "digest": sha256(content.encode()),
        "evidence_id": "evidence:test-" + sha256(content.encode())[:20],
    }


def test_normalizer_signature_collision_and_hidden_independence():
    first = "  timeout\n UUID=8b00baaa-0012-413c-b840-000000000123 at 2026-10-05T16:00:00.123Z host 10.2.3.4 ptr 0xaAbb attempt=123 V1 1.2.3 driver2 change-checkout-1 "
    second = (
        first.replace("000000000123", "000000000999")
        .replace("16:00:00.123Z", "16:01:00Z")
        .replace("10.2.3.4", "192.168.1.1")
        .replace("0xaAbb", "0xffff")
        .replace("attempt=123", "attempt=999")
    )
    assert normalize_excerpt(first) == normalize_excerpt(second)
    value = normalize_excerpt(first)
    assert all(
        x in value
        for x in (
            "<UUID>",
            "<TIMESTAMP>",
            "<IPV4>",
            "<HEX_ADDRESS>",
            "<DECIMAL>",
            "V1",
            "1.2.3",
            "DRIVER2",
            "CHANGE-CHECKOUT-1",
        )
    )
    base = dict(
        provider_case_id="case-1",
        outcome="ERROR",
        error_code="SAME",
        component="checkout",
    )
    assert (
        VisibleFailure(**base, error_excerpt="connection refused").grouping()
        != VisibleFailure(**base, error_excerpt="assertion mismatch").grouping()
    )
    assert VisibleFailure(provider_case_id="case-1", outcome="FAILED").grouping() == (
        "NO_SIGNATURE:case-1",
        ["UNKNOWN_COMPONENT"],
    )
    with pytest.raises(ValueError):
        VisibleFailure(**base, hidden_root_id="hidden-root-01")
    # evaluator 映射变化完全不传入规则；不同根可同组，同根可不同组。
    fixtures = [
        ("root-a", "visible symptom"),
        ("root-b", "visible symptom"),
        ("root-a", "other symptom"),
    ]
    signatures = [
        VisibleFailure(**base, error_excerpt=text).grouping() for _, text in fixtures
    ]
    assert signatures[0] == signatures[1] and signatures[0] != signatures[2]
    failure = VisibleFailure(**base, error_excerpt="visible symptom")
    assert local_key("version-a", failure) != local_key("version-b", failure)
    assert incident_key(
        "scope", "project", "suite", "2026-10-06", failure
    ) != incident_key("scope", "project", "suite", "2026-10-07", failure)
    from types import SimpleNamespace

    representatives = [
        SimpleNamespace(
            channel=f"channel-{i%12:02d}",
            environment=f"env-{i:04d}",
            ordinal=i % 3 + 1,
            case_id=f"case-{i:03d}",
            version_id=f"version-{i}",
        )
        for i in range(40)
    ]
    assert choose_representatives(representatives) == choose_representatives(
        list(reversed(representatives))
    )
    assert len(choose_representatives(representatives)) == 8
    save(
        "hidden-root-independence.json",
        {
            "two_hidden_roots_one_visible_signature": signatures[0] == signatures[1],
            "one_hidden_root_two_signatures": signatures[0] != signatures[2],
            "hidden_field_rejected": True,
            "model_calls": 0,
        },
    )


@pytest.mark.asyncio
async def test_small_real_input_replay_channels_sealing_and_no_model(
    clean_database, monkeypatch
):
    from core.agent_platform.application import AgentApplicationService

    async def no_model(*args, **kwargs):
        raise AssertionError("WP03 不允许 Agent execution")

    monkeypatch.setattr(AgentApplicationService, "execute", no_model)
    from core.stage13.workload import Stage13Workload

    def no_gt(*args, **kwargs):
        raise AssertionError("WP03 产品路径不得访问 evaluator GT")

    monkeypatch.setattr(Stage13Workload, "root_truth", no_gt)
    monkeypatch.setattr(Stage13Workload, "hidden_gt_manifest", no_gt)
    c, service, worker = await small(clean_database)
    try:
        members = await rows(clean_database, MembershipRow)
        assert len(members) == sum(
            v.counts["FAILED"] + v.counts["ERROR"]
            for v in await rows(clean_database, VersionRow)
        )
        incidents = await rows(clean_database, IncidentRow)
        assert any(
            i.draft["failure_summary"]["environment_count"] > 1 for i in incidents
        )
        assert any(
            len(i.draft["failure_summary"]["channel_distribution"]) > 1
            for i in incidents
        )
        assert any(
            len(i.draft["failure_summary"]["channel_distribution"]) == 1
            for i in incidents
        )
        assert any(
            len(i.draft["failure_summary"]["product_version_distribution"]) == 3
            for i in incidents
        )
        for i in incidents:
            data = i.draft
            assert (
                len(data["environment_samples"]) <= 8
                and data["failure_summary"]["represented_members"] <= 8
                and len(data["EvidenceRefs"]) <= 32
            )
            assert len(canonical_bytes(data)) <= 128 * 1024
            assert (
                data["failure_summary"]["omitted_count"]
                == data["failure_summary"]["total_members"]
                - data["failure_summary"]["represented_members"]
            )
            for e in data["visible_evidence"]:
                assert sha256(e["content"].encode()) == e["digest"]
        before = {i.incident_key: (i.material_digest, i.draft) for i in incidents}
        collections = await rows(clean_database, CollectionRow)
        await asyncio.gather(
            *(service.ingest(col.version_id, col.pages) for col in collections[:4])
        )
        assert {
            i.incident_key: (i.material_digest, i.draft)
            for i in await rows(clean_database, IncidentRow)
        } == before
        assert len(await rows(clean_database, MembershipRow)) == len(members)
        assert await service.seal(
            "automation-demo", "nightly-suite", "2026-10-06"
        ) == len(incidents)
        assert all(i.state == "SEALED" for i in await rows(clean_database, IncidentRow))
        await c.clock(2600)
        result = await service.admit("automation-demo", "nightly-suite", "2026-10-06")
        assert result["admitted_count"] == len(incidents)
        for job in await rows(clean_database, AnalysisJobRow):
            assert (
                job.input["evidence_policy"]["deadline_at"]
                == (BASE + timedelta(seconds=2780)).isoformat()
            )
        assert (await service.admit("automation-demo", "nightly-suite", "2026-10-06"))[
            "admitted_count"
        ] == len(incidents)
        assert c.provider.counters["artifact_reads"] == 0
        save(
            "small-cohort.json",
            {
                "environments": 12,
                "versions": 36,
                "members": len(members),
                "incidents": len(incidents),
                "admission": result,
                "model_calls": 0,
            },
        )
    finally:
        await worker.close()
        await c.close()


@pytest.mark.asyncio
async def test_initial_fifty_plus_twenty_updates_spend_only_ten_slots(clean_database):
    c, service, worker = await small(clean_database, count=20)
    try:
        collections = await rows(clean_database, CollectionRow)
        assignment = {}
        cursor = 0
        for col in collections:
            for page in col.pages:
                for value in json.loads(page["content"])["cases"]:
                    assignment[(col.version_id, value["provider_case_id"])] = (
                        cursor % 50
                    )
                    cursor += 1
        assert cursor >= 70
        transformed = {}
        for col in collections:

            def symptom(case):
                group = assignment[(col.version_id, case["provider_case_id"])]
                case.update(
                    error_code="DIAGNOSTIC",
                    component="checkout",
                    error_excerpt="visible symptom "
                    + chr(65 + group // 26)
                    + chr(65 + group % 26),
                    visible_change_refs=[],
                )

            transformed[col.version_id] = [rewrite(p, symptom) for p in col.pages]
            await service.ingest(col.version_id, transformed[col.version_id])
        await c.clock(2600)
        initial = await service.admit("automation-demo", "nightly-suite", "2026-10-06")
        assert initial["admitted_count"] == 50
        for col in collections:

            def change(case):
                if assignment[(col.version_id, case["provider_case_id"])] < 20:
                    case["visible_change_refs"] = ["change-catalog-3"]

            await service.ingest(
                col.version_id,
                [rewrite(p, change) for p in transformed[col.version_id]],
            )
        await c.clock(4400)
        result = await service.admit("automation-demo", "nightly-suite", "2026-10-06")
        assert (
            result["admitted_count"] == 60
            and result["eligible_count"] == 70
            and result["deferred_count"] == 10
        )
        jobs = await rows(clean_database, AnalysisJobRow)
        assert (
            sum(j.kind == "REANALYSIS" and j.admitted_at is not None for j in jobs)
            == 10
        )
        async with clean_database.transaction() as s:
            await s.execute(
                update(AnalysisJobRow)
                .where(AnalysisJobRow.admitted_at.is_not(None))
                .values(status="FAILED")
            )
        assert (await service.admit("automation-demo", "nightly-suite", "2026-10-06"))[
            "admitted_count"
        ] == 60
        save(
            "reanalysis-budget.json",
            {
                "initial_jobs": 50,
                "material_updates": 20,
                "admitted_reanalysis": 10,
                "deferred": 10,
                "failed_jobs_no_refund": True,
            },
        )
    finally:
        await worker.close()
        await c.close()


@pytest.mark.asyncio
async def test_real_process_crashes_rollback_and_committed_replay(clean_database):
    c, service, worker = await small(clean_database)
    try:
        collection = (await rows(clean_database, CollectionRow))[0]
        pages = [
            rewrite(
                p,
                lambda case: case.update(
                    error_excerpt="new diagnostic after process restart"
                ),
            )
            for p in collection.pages
        ]
        target = EVIDENCE / "crash-input.json"
        save("crash-input.json", {"version_id": collection.version_id, "pages": pages})
        before = len(await rows(clean_database, ClusterRow))
        outcomes = []
        for point in ("after_cluster_before_commit", "after_incident_before_commit"):
            output = await asyncio.to_thread(processes, "ingest", 1, point, str(target))
            assert output[0]["exit"] == 41, output
            assert len(await rows(clean_database, ClusterRow)) == before
            outcomes.append({"point": point, "exit": 41, "rolled_back": True})
        output = await asyncio.to_thread(
            processes, "ingest", 1, "after_ingest_commit", str(target)
        )
        assert output[0]["exit"] == 41
        after = len(await rows(clean_database, ClusterRow))
        assert after > before
        output = await asyncio.to_thread(
            processes, "ingest", 2, "no-crash", str(target)
        )
        assert (
            all(o["exit"] == 0 for o in output)
            and len(await rows(clean_database, ClusterRow)) == after
        )
        await c.clock(2600)
        output = await asyncio.to_thread(
            processes, "admit", 1, "after_revision_before_admission_commit"
        )
        assert (
            output[0]["exit"] == 41
            and not await rows(clean_database, RevisionRow)
            and not await rows(clean_database, AnalysisJobRow)
        )
        output = await asyncio.to_thread(
            processes, "admit", 1, "after_admission_commit"
        )
        assert output[0]["exit"] == 41
        canonical = len(await rows(clean_database, AnalysisJobRow))
        output = await asyncio.to_thread(processes, "admit", 2)
        assert (
            all(o["exit"] == 0 for o in output)
            and len(await rows(clean_database, AnalysisJobRow)) == canonical
        )
        save(
            "real-process-crashes.json",
            {
                "pre_commit": outcomes,
                "after_ingest_commit_exit": 41,
                "after_admission_commit_exit": 41,
                "canonical_jobs": canonical,
                "replay_workers": 2,
                "model_calls": 0,
            },
        )
    finally:
        await worker.close()
        await c.close()


@pytest.mark.asyncio
async def test_revision_material_coalescing_reanalysis_and_immutable(clean_database):
    c, service, worker = await small(clean_database)
    try:
        incidents = await rows(clean_database, IncidentRow)
        first = min(i.first_seen_at for i in incidents)
        await c.clock(int((first - BASE).total_seconds()) + 599)
        assert (await service.admit("automation-demo", "nightly-suite", "2026-10-06"))[
            "admitted_count"
        ] == 0
        await c.clock(2600)
        initial = await service.admit("automation-demo", "nightly-suite", "2026-10-06")
        revisions = await rows(clean_database, RevisionRow)
        frozen = {
            (r.incident_id, r.revision): (r.manifest, r.manifest_digest)
            for r in revisions
        }
        collection = (await rows(clean_database, CollectionRow))[0]
        # 无新可见内容的重放不产生 material revision。
        await service.ingest(collection.version_id, collection.pages)
        await c.clock(3000)
        assert (await service.admit("automation-demo", "nightly-suite", "2026-10-06"))[
            "admitted_count"
        ] == initial["admitted_count"]

        def changes(case):
            case["visible_change_refs"] = sorted(
                set(case["visible_change_refs"]) | set(CHANGES[:-1])
            )

        changed = [rewrite(page, changes) for page in collection.pages]
        await service.ingest(collection.version_id, changed)
        # material evidence 后仍受上次 admission+1800 限制。
        await c.clock(4399)
        assert (await service.admit("automation-demo", "nightly-suite", "2026-10-06"))[
            "admitted_count"
        ] == initial["admitted_count"]
        await c.clock(4400)
        reanalysis = await service.admit(
            "automation-demo", "nightly-suite", "2026-10-06"
        )
        assert reanalysis["admitted_count"] > initial["admitted_count"]
        for revision in await rows(clean_database, RevisionRow):
            if revision.revision == 2:
                changes = next(
                    e
                    for e in revision.manifest["visible_evidence"]
                    if e["type"] == "CHANGE_METADATA"
                )
                assert set(CHANGES[:-1]) <= set(
                    json.loads(changes["content"])["visible_change_refs"]
                )
        for r in await rows(clean_database, RevisionRow):
            if (r.incident_id, r.revision) in frozen:
                assert (r.manifest, r.manifest_digest) == frozen[
                    (r.incident_id, r.revision)
                ]
        r = revisions[0]
        with pytest.raises(PersistenceError):
            async with clean_database.transaction() as s:
                await s.execute(
                    update(RevisionRow)
                    .where(
                        RevisionRow.incident_id == r.incident_id,
                        RevisionRow.revision == r.revision,
                    )
                    .values(manifest={})
                )
        await service.ingest(
            collection.version_id,
            [
                rewrite(
                    page, lambda case: case.update(visible_change_refs=list(CHANGES))
                )
                for page in changed
            ],
        )
        await c.clock(6000)
        assert (await service.admit("automation-demo", "nightly-suite", "2026-10-06"))[
            "admitted_count"
        ] == reanalysis["admitted_count"]
        save(
            "material-reanalysis.json",
            {
                "initial": initial,
                "reanalysis": reanalysis,
                "immutable_old_revision": True,
                "non_material_replay": True,
                "max_one_reanalysis": True,
            },
        )
    finally:
        await worker.close()
        await c.close()


@pytest.mark.asyncio
async def test_error_cases_pending_missing_and_stale_collector_fence(clean_database):
    c, service, worker = await small(clean_database, count=20, errors=True)
    try:
        members = await rows(clean_database, MembershipRow)
        assert sum(m.outcome == "ERROR" for m in members) == 1
        error = next(m for m in members if m.outcome == "ERROR")
        assert await service.seal("automation-demo", "nightly-suite", "2026-10-06") > 0
        await service.refresh(error.version_id)
        assert await service.seal("automation-demo", "nightly-suite", "2026-10-06") == 0
        claim = (await service.claim())[0]
        from sqlalchemy import text

        async with clean_database.transaction() as s:
            await s.execute(
                update(CollectionRow)
                .where(CollectionRow.version_id == claim[0])
                .values(
                    lease_until=func.clock_timestamp() - text("interval '1 second'")
                )
            )
        newer = (await service.claim())[0]
        assert newer[2] > claim[2]
        with pytest.raises(StaleClaim):
            await service.ingest(claim[0], [], claim=claim, missing=True)
        assert not await service.fail(newer)
        await c.clock(2000)
        third = (await service.claim())[0]
        assert await service.fail(third)
        collection = next(
            r
            for r in await rows(clean_database, CollectionRow)
            if r.version_id == error.version_id
        )
        assert collection.state == "MISSING" and collection.attempts == 3
        assert await service.seal("automation-demo", "nightly-suite", "2026-10-06") > 0
        save(
            "error-missing-fence.json",
            {
                "ERROR_memberships": 1,
                "stale_writer_rejected": True,
                "detail_attempts": 3,
                "explicit_missing": True,
                "pending_evidence_prevents_seal": True,
                "lease_test_mode": "TEST_SCOPE_EXPLICIT_DB_EXPIRATION",
            },
        )
    finally:
        await worker.close()
        await c.close()


def processes(mode, count, *args):
    commands = [
        [sys.executable, str(ROOT / "tests/_stage13_wp03_process.py"), mode, *args]
        for _ in range(count)
    ]
    children = [
        subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8"
        )
        for cmd in commands
    ]
    outputs = []
    for child in children:
        stdout, stderr = child.communicate(timeout=60)
        outputs.append({"exit": child.returncode, "stdout": stdout, "stderr": stderr})
    return outputs


@pytest.mark.asyncio
async def test_non_material_new_environment_growth_does_not_revise(clean_database):
    c, service, worker = await small(clean_database)
    try:
        await c.clock(2600)
        initial = await service.admit("automation-demo", "nightly-suite", "2026-10-06")
        old_revisions = len(await rows(clean_database, RevisionRow))
        old_members = len(await rows(clean_database, MembershipRow))
        existing = {g.environment for g in await rows(clean_database, GuardianRow)}
        index = next(
            i
            for i in range(100)
            if c.workload.environment(i)["environment_id"] not in existing
            and any(p.failed_cases for p in c.workload.plans[i * 3 : i * 3 + 3])
        )
        _, cycle = await c.discover(index)
        seconds = 2700
        await c.clock(seconds)
        for ordinal in range(3):
            await c.drain()
            version = (await c.versions(cycle))[ordinal]
            assert version.status == "ACTIVE"
            await c.remote_terminal(version)
            seconds += 400
            await c.clock(seconds)
            await c.drain()
        while await worker.tick():
            pass
        assert len(await rows(clean_database, MembershipRow)) > old_members
        await c.clock(4400)
        result = await service.admit("automation-demo", "nightly-suite", "2026-10-06")
        assert result["admitted_count"] == initial["admitted_count"]
        assert len(await rows(clean_database, RevisionRow)) == old_revisions
        save(
            "non-material-growth.json",
            {
                "before_members": old_members,
                "after_members": len(await rows(clean_database, MembershipRow)),
                "new_revisions": 0,
                "new_analysis_jobs": 0,
                "new_environment_uses_real_wp02_path": True,
            },
        )
    finally:
        await worker.close()
        await c.close()


@pytest.mark.asyncio
async def test_four_process_budget_dedup_and_restart_atomicity(clean_database):
    c, service, worker = await small(clean_database, count=20)
    try:
        collections = await rows(clean_database, CollectionRow)
        # >60 独立可见症状；仅改变公开摘录，不用 private root 作为 key。
        for index, col in enumerate(collections):

            def distinct(case):
                case["error_excerpt"] = (
                    "distinct diagnostic token"
                    + chr(65 + index // 26)
                    + chr(65 + index % 26)
                    + case["provider_case_id"]
                )

            await service.ingest(
                col.version_id, [rewrite(page, distinct) for page in col.pages]
            )
        await c.clock(2400)
        output = await asyncio.to_thread(processes, "admit", 4)
        assert all(o["exit"] == 0 for o in output), output
        budgets = await rows(clean_database, BudgetRow)
        jobs = await rows(clean_database, AnalysisJobRow)
        assert budgets[0].admitted_total == 60
        admitted = [j for j in jobs if j.admitted_at]
        assert len(admitted) == 60 and len(jobs) > 60
        incidents = {i.incident_id: i for i in await rows(clean_database, IncidentRow)}
        ordered = sorted(
            [j for j in jobs],
            key=lambda j: (
                incidents[j.incident_id].first_seen_at,
                incidents[j.incident_id].incident_key,
            ),
        )
        assert {j.job_id for j in admitted} == {j.job_id for j in ordered[:60]}
        two = await asyncio.to_thread(processes, "admit", 2)
        assert all(o["exit"] == 0 for o in two)
        async with clean_database.transaction() as s:
            await s.execute(
                update(AnalysisJobRow)
                .where(
                    AnalysisJobRow.job_id.in_([admitted[0].job_id, admitted[1].job_id])
                )
                .values(status="UNRESOLVED")
            )
        assert (await service.admit("automation-demo", "nightly-suite", "2026-10-06"))[
            "admitted_count"
        ] == 60
        save(
            "budget-concurrency.json",
            {
                "workers": [2, 4],
                "process_results": output + two,
                "admitted": 60,
                "eligible": len(jobs),
                "deferred": len(jobs) - 60,
                "deterministic_cutoff": True,
                "restart_no_refund": True,
            },
        )
        save(
            "replay-restart.json",
            {
                "two_and_four_real_workers": True,
                "unique_memberships": len(await rows(clean_database, MembershipRow)),
                "unique_jobs": len(jobs),
                "budget": 60,
            },
        )
    finally:
        await worker.close()
        await c.close()
