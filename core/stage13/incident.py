"""WP03 Incident Truth 与原子准入；外部取证及未来 Agent 均在事务外。"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from uuid import uuid4

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from core.stage13.aggregation import (
    CONTROLLED_SUBJECT,
    NORMALIZER_VERSION,
    SUBJECT_DIGEST,
    VisibleFailure,
    choose_representatives,
    incident_key,
    local_key,
)
from core.stage13.contracts import (
    CISummary,
    EvidencePacket,
    business_key,
    canonical_bytes,
    sha256,
)
from core.stage13.guardian import CYCLE_TERMINAL, StaleClaim
from core.stage13.guardian_models import (
    CycleRow,
    DueRow,
    GuardianRow,
    ObservationRow,
    SchedulerRow,
    VersionRow,
)
from core.stage13.incident_models import (
    AnalysisJobRow,
    BudgetRow,
    ClusterRow,
    CollectionRow,
    IncidentRow,
    MembershipRow,
    RevisionRow,
)


class IncidentAggregationService:
    def __init__(self, database, scope):
        self.database, self.scope = database, scope

    async def _now(self, session):
        clock = await session.get(SchedulerRow, self.scope)
        return (
            clock.logical_now
            if clock and clock.logical_now
            else (await session.execute(select(func.clock_timestamp()))).scalar_one()
        )

    async def _day_lock(self, session, project, suite, day):
        # 同日短事务使用一致锁顺序；不持锁等待外部 HTTP/未来 Agent。
        key = business_key("incident-day-lock", self.scope, project, suite, day)
        value = int.from_bytes(bytes.fromhex(key)[:8], "big", signed=True)
        await session.execute(select(func.pg_advisory_xact_lock(value)))

    async def discover(self, limit=100):
        """有限 anti-join，只消费 WP02 已校验完成且有 FAILED/ERROR 的版本。"""
        if not 1 <= limit <= 100:
            raise ValueError("collection batch 必须在 1..100")
        async with self.database.transaction() as session:
            now = await self._now(session)
            ids = (
                (
                    await session.execute(
                        select(VersionRow.version_execution_id)
                        .join(CycleRow, VersionRow.cycle_id == CycleRow.cycle_id)
                        .join(
                            GuardianRow, CycleRow.guardian_id == GuardianRow.guardian_id
                        )
                        .where(
                            GuardianRow.scope == self.scope,
                            VersionRow.status == "COMPLETED",
                            (
                                VersionRow.counts["FAILED"].as_integer()
                                + VersionRow.counts["ERROR"].as_integer()
                            )
                            > 0,
                            ~exists(
                                select(CollectionRow.version_id).where(
                                    CollectionRow.version_id
                                    == VersionRow.version_execution_id
                                )
                            ),
                        )
                        .order_by(
                            VersionRow.completed_at, VersionRow.version_execution_key
                        )
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            for vid in ids:
                await session.execute(
                    insert(CollectionRow)
                    .values(
                        version_id=vid,
                        state="PENDING",
                        epoch=0,
                        attempts=0,
                        pages=[],
                        next_available_at=now,
                    )
                    .on_conflict_do_nothing()
                )
            return len(ids)

    async def claim(self, limit=20):
        if not 1 <= limit <= 20:
            raise ValueError("collection concurrency 必须在 1..20")
        async with self.database.transaction() as session:
            await session.get(SchedulerRow, self.scope, with_for_update=True)
            now = await self._now(session)
            dbnow = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            busy_collections = (
                await session.execute(
                    select(func.count())
                    .select_from(CollectionRow)
                    .join(
                        VersionRow,
                        CollectionRow.version_id == VersionRow.version_execution_id,
                    )
                    .where(
                        VersionRow.request["owner_scope_id"].as_string() == self.scope,
                        CollectionRow.state == "CLAIMED",
                        CollectionRow.lease_until > dbnow,
                    )
                )
            ).scalar_one()
            busy_guardian = (
                await session.execute(
                    select(func.count())
                    .select_from(DueRow)
                    .where(
                        DueRow.scope == self.scope,
                        DueRow.state == "CLAIMED",
                        DueRow.started_at.is_not(None),
                        DueRow.lease_until > dbnow,
                        DueRow.operation.in_(
                            ("POLL_REMOTE", "RECONCILE_REMOTE", "FETCH_TERMINAL_RESULT")
                        ),
                    )
                )
            ).scalar_one()
            limit = min(limit, max(0, 20 - busy_collections - busy_guardian))
            if not limit:
                return []
            rows = (
                (
                    await session.execute(
                        select(CollectionRow)
                        .join(
                            VersionRow,
                            CollectionRow.version_id == VersionRow.version_execution_id,
                        )
                        .where(
                            VersionRow.request["owner_scope_id"].as_string()
                            == self.scope,
                            CollectionRow.next_available_at <= now,
                            or_(
                                CollectionRow.state == "PENDING",
                                and_(
                                    CollectionRow.state == "CLAIMED",
                                    CollectionRow.lease_until <= dbnow,
                                ),
                            ),
                        )
                        .order_by(
                            CollectionRow.next_available_at, CollectionRow.version_id
                        )
                        .limit(limit)
                        .with_for_update(of=CollectionRow, skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            result = []
            for row in rows:
                # 锁后取真实 DB clock；逻辑时钟不压缩 lease。
                dbnow = (
                    await session.execute(select(func.clock_timestamp()))
                ).scalar_one()
                row.state, row.token = "CLAIMED", str(uuid4())
                row.epoch += 1
                row.lease_until = dbnow + timedelta(seconds=30)
                exhausted = row.attempts >= 3
                if not exhausted:
                    row.attempts += 1
                result.append((row.version_id, row.token, row.epoch, exhausted))
            return result

    async def source(self, version_id):
        """读取 refs 后关闭 session；调用方才可执行 detail HTTP。"""
        async with self.database.session() as session:
            version = await session.get(VersionRow, version_id)
            if (
                not version
                or version.request["owner_scope_id"] != self.scope
                or version.status != "COMPLETED"
            ):
                raise ValueError("VERSION_SOURCE_INVALID")
            observation = (
                await session.execute(
                    select(ObservationRow)
                    .where(
                        ObservationRow.version_execution_id == version_id,
                        ObservationRow.changed.is_(True),
                        ObservationRow.evidence.is_not(None),
                    )
                    .order_by(
                        ObservationRow.business_read_at.desc(),
                        ObservationRow.work_key.desc(),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if not observation:
                raise ValueError("MISSING_WP02_OBSERVATION")
            packet = EvidencePacket.model_validate(observation.evidence)
            summary = CISummary.model_validate_json(packet.content)
            if summary.model_dump(mode="json") != version.terminal_summary:
                raise ValueError("TERMINAL_SUMMARY_BINDING_INVALID")
            return version, packet

    async def _fence(self, session, claim):
        vid, token, epoch = claim[:3]
        row = await session.get(CollectionRow, vid, with_for_update=True)
        dbnow = (await session.execute(select(func.clock_timestamp()))).scalar_one()
        if (
            not row
            or row.state != "CLAIMED"
            or row.token != token
            or row.epoch != epoch
            or row.lease_until <= dbnow
        ):
            raise StaleClaim("COLLECTION_STALE_WRITER")
        return row

    async def fail(self, claim, code="DETAIL_COLLECTION_FAILED"):
        async with self.database.transaction() as session:
            row = await self._fence(session, claim)
            row.error = code
            now = await self._now(session)
            if row.attempts < 3:
                row.state = "PENDING"
                row.next_available_at = now + timedelta(
                    seconds=(30, 120)[row.attempts - 1]
                )
                return False
        # 耗尽后明确 missing；仍使用当前 claim/fence，不能伪造 detail。
        await self.ingest(claim[0], [], claim=claim, missing=True)
        return True

    async def refresh(self, version_id):
        """显式补取可见来源；不刷新已消耗的 Analysis budget。"""
        async with self.database.transaction() as session:
            row = await session.get(CollectionRow, version_id, with_for_update=True)
            version = await session.get(VersionRow, version_id)
            if (
                not row
                or version.request["owner_scope_id"] != self.scope
                or row.state == "CLAIMED"
            ):
                raise ValueError("COLLECTION_REFRESH_CONFLICT")
            cycle = await session.get(CycleRow, version.cycle_id)
            await self._day_lock(
                session,
                version.request["automation_project_id"],
                version.request["suite_id"],
                cycle.business_date,
            )
            await session.execute(
                update(IncidentRow)
                .where(
                    IncidentRow.incident_id.in_(
                        select(MembershipRow.incident_id).where(
                            MembershipRow.version_id == version_id
                        )
                    )
                )
                .values(state="OPEN")
            )
            row.state, row.attempts, row.error = "PENDING", 0, None
            row.next_available_at = await self._now(session)

    async def ingest(self, version_id, pages, *, claim=None, missing=False, fault=None):
        """原子写 cluster/membership/incident；重放和补充证据都校验 WP02 binding。"""
        version, summary_packet = await self.source(version_id)
        summary = CISummary.model_validate_json(summary_packet.content)
        expected = {f.provider_case_id: f.outcome for f in summary.failure_index}
        cases, valid_pages = {}, []
        if len(pages) > 4:
            raise ValueError("DETAIL_PAGE_BUDGET")
        offset = 0
        for raw in pages:
            packet = EvidencePacket.model_validate(raw)
            if (
                packet.type != "FAILURE_DETAIL"
                or packet.availability != "AVAILABLE"
                or packet.owner_scope_id != self.scope
                or str(packet.remote_execution_id) != version.remote_id
            ):
                raise ValueError("DETAIL_SOURCE_BINDING_INVALID")
            body = json.loads(packet.content)
            if (
                set(body)
                != {
                    "schema_version",
                    "remote_execution_id",
                    "cases",
                    "total_failure_cases",
                    "next_offset",
                }
                or body["schema_version"] != "stage13.failure-page.v1"
                or body["remote_execution_id"] != version.remote_id
                or body["total_failure_cases"] != len(expected)
                or len(body["cases"]) > 50
            ):
                raise ValueError("DETAIL_PAGE_INVALID")
            for value in body["cases"]:
                case = VisibleFailure.model_validate(value)
                if (
                    case.provider_case_id in cases
                    or expected.get(case.provider_case_id) != case.outcome
                ):
                    raise ValueError("DETAIL_CASE_BINDING_INVALID")
                if (
                    case.component
                    and case.component not in summary.visible_component_inventory
                    or set(case.visible_change_refs)
                    - set(summary.visible_change_inventory)
                ):
                    raise ValueError("DETAIL_INVENTORY_INVALID")
                cases[case.provider_case_id] = case
            offset += len(body["cases"])
            if body["next_offset"] != (offset if offset < len(expected) else None):
                raise ValueError("DETAIL_PAGINATION_INVALID")
            valid_pages.append(packet.model_dump(mode="json"))
        if not missing and set(cases) != set(expected):
            raise ValueError("DETAIL_CASE_COVERAGE_INVALID")
        for cid, outcome in expected.items():
            cases.setdefault(cid, VisibleFailure(provider_case_id=cid, outcome=outcome))
        prepared = sorted(
            cases.values(),
            key=lambda c: (
                incident_key(
                    self.scope, summary.automation_project_id, summary.suite_id, "", c
                ),
                c.provider_case_id,
            ),
        )
        async with self.database.transaction() as session:
            collection = (
                await self._fence(session, claim)
                if claim
                else await session.get(CollectionRow, version_id, with_for_update=True)
            )
            if not collection:
                raise ValueError("MISSING_COLLECTION_INTENT")
            cycle = await session.get(CycleRow, version.cycle_id)
            await self._day_lock(
                session,
                summary.automation_project_id,
                summary.suite_id,
                cycle.business_date,
            )
            now, touched = await self._now(session), set()
            for case in prepared:
                key = incident_key(
                    self.scope,
                    summary.automation_project_id,
                    summary.suite_id,
                    cycle.business_date,
                    case,
                )
                signature, components = case.grouping()
                await session.execute(
                    insert(IncidentRow)
                    .values(
                        incident_id=str(uuid4()),
                        incident_key=key,
                        scope=self.scope,
                        project=summary.automation_project_id,
                        suite=summary.suite_id,
                        business_date=cycle.business_date,
                        normalizer_version=NORMALIZER_VERSION,
                        signature=signature,
                        components=components,
                        state="OPEN",
                        first_seen_at=now,
                        last_seen_at=now,
                        changes=[],
                        material_digest=sha256(canonical_bytes({})),
                        draft={},
                        evidence_revision=0,
                    )
                    .on_conflict_do_nothing(index_elements=[IncidentRow.incident_key])
                )
                incident = (
                    await session.execute(
                        select(IncidentRow)
                        .where(IncidentRow.incident_key == key)
                        .with_for_update()
                    )
                ).scalar_one()
                ckey = local_key(version.version_execution_key, case)
                await session.execute(
                    insert(ClusterRow)
                    .values(
                        cluster_id=str(uuid4()),
                        cluster_key=ckey,
                        version_id=version_id,
                        incident_id=incident.incident_id,
                        normalizer_version=NORMALIZER_VERSION,
                        signature=signature,
                        components=components,
                        summary=summary_packet.model_dump(mode="json"),
                    )
                    .on_conflict_do_nothing(index_elements=[ClusterRow.cluster_key])
                )
                cluster = (
                    await session.execute(
                        select(ClusterRow).where(ClusterRow.cluster_key == ckey)
                    )
                ).scalar_one()
                old = await session.get(
                    MembershipRow, (version_id, case.provider_case_id)
                )
                if old:
                    touched.add(old.incident_id)
                detail_bytes = canonical_bytes(case.model_dump(mode="json"))
                digest = sha256(detail_bytes)
                ekey = business_key(
                    "evidence",
                    self.scope,
                    version.remote_id,
                    case.provider_case_id,
                    "ERROR_EXCERPT",
                    digest,
                )
                # UUID 在同 membership 重放时复用；key/digest 为持久身份依据。
                eid = (
                    old.evidence["evidence_id"]
                    if old and old.evidence["digest"] == digest
                    else f"evidence:{uuid4()}"
                )
                evidence = {
                    "evidence_id": eid,
                    "evidence_key": ekey,
                    "type": "ERROR_EXCERPT",
                    "digest": digest,
                    "owner_scope_id": self.scope,
                    "remote_execution_id": version.remote_id,
                    "provider_case_id": case.provider_case_id,
                    "schema_version": "stage13.visible-failure.v1",
                    "availability": "MISSING" if missing else "AVAILABLE",
                    "content": detail_bytes.decode("utf-8"),
                    "source_detail_refs": [
                        {"evidence_id": p["evidence_id"], "digest": p["digest"]}
                        for p in valid_pages
                        if case.provider_case_id in p["content"]
                    ],
                }
                values = dict(
                    cluster_id=cluster.cluster_id,
                    incident_id=incident.incident_id,
                    environment=summary.environment_id,
                    channel=summary.channel_group,
                    product_version=summary.product_version,
                    ordinal=summary.ordinal,
                    outcome=case.outcome,
                    availability=evidence["availability"],
                    evidence=evidence,
                )
                await session.execute(
                    insert(MembershipRow)
                    .values(
                        version_id=version_id, case_id=case.provider_case_id, **values
                    )
                    .on_conflict_do_update(
                        index_elements=[
                            MembershipRow.version_id,
                            MembershipRow.case_id,
                        ],
                        set_=values,
                    )
                )
                incident.changes = sorted(
                    set(incident.changes) | set(case.visible_change_refs)
                )
                incident.last_seen_at = max(incident.last_seen_at, now)
                touched.add(incident.incident_id)
            if fault:
                fault("after_cluster_before_commit")
            await session.flush()
            for iid in sorted(touched):
                incident = await session.get(IncidentRow, iid, with_for_update=True)
                await self._refresh_draft(session, incident)
            if claim:
                await self._fence(session, claim)
            collection.state, collection.pages = (
                "MISSING" if missing else "COMPLETE"
            ), valid_pages
            collection.read_at = (
                await session.execute(select(func.clock_timestamp()))
            ).scalar_one()
            if fault:
                fault("after_incident_before_commit")
        if fault:
            fault("after_ingest_commit")

    async def _refresh_draft(self, session, incident):
        predicate = MembershipRow.incident_id == incident.incident_id
        groups = (
            await session.execute(
                select(
                    MembershipRow.channel,
                    MembershipRow.product_version,
                    MembershipRow.outcome,
                    MembershipRow.availability,
                    func.count(),
                )
                .where(predicate)
                .group_by(
                    MembershipRow.channel,
                    MembershipRow.product_version,
                    MembershipRow.outcome,
                    MembershipRow.availability,
                )
            )
        ).all()
        channels, versions, outcomes, missing, total = {}, {}, {}, 0, 0
        for channel, version, outcome, availability, n in groups:
            channels[channel] = channels.get(channel, 0) + n
            versions[version] = versions.get(version, 0) + n
            outcomes[outcome] = outcomes.get(outcome, 0) + n
            total += n
            missing += n if availability == "MISSING" else 0
        env_count = (
            await session.execute(
                select(func.count(func.distinct(MembershipRow.environment))).where(
                    predicate
                )
            )
        ).scalar_one()
        ranks = (
            select(
                MembershipRow.version_id,
                MembershipRow.case_id,
                func.row_number()
                .over(
                    partition_by=MembershipRow.channel,
                    order_by=(
                        MembershipRow.environment,
                        MembershipRow.ordinal,
                        MembershipRow.case_id,
                        MembershipRow.version_id,
                    ),
                )
                .label("position"),
            )
            .where(predicate)
            .subquery()
        )
        candidates = (
            (
                await session.execute(
                    select(MembershipRow)
                    .join(
                        ranks,
                        and_(
                            MembershipRow.version_id == ranks.c.version_id,
                            MembershipRow.case_id == ranks.c.case_id,
                        ),
                    )
                    .where(ranks.c.position <= 8)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        reps = choose_representatives(candidates)
        bounded, projected, projected_ids = [], [], set()
        for member in reps:
            cluster = await session.get(ClusterRow, member.cluster_id)
            additions = [
                p
                for p in (member.evidence, cluster.summary)
                if p["evidence_id"] not in projected_ids
            ]
            if len(canonical_bytes(projected + additions)) > 100 * 1024:
                continue
            bounded.append(member)
            projected.extend(additions)
            projected_ids.update(p["evidence_id"] for p in additions)
        reps = bounded
        evidence, seen = [], set()
        refs, clusters, version_refs, environments = [], [], [], []
        for member in reps:
            cluster = await session.get(ClusterRow, member.cluster_id)
            for packet in (member.evidence, cluster.summary):
                if packet["evidence_id"] not in seen:
                    seen.add(packet["evidence_id"])
                    evidence.append(packet)
                    if packet["availability"] == "AVAILABLE":
                        refs.append(
                            {k: packet[k] for k in ("evidence_id", "digest", "type")}
                        )
            clusters.append(member.cluster_id)
            version_refs.append(member.version_id)
            environments.append(
                {"environment_id": member.environment, "channel_group": member.channel}
            )
        represented_channels = sorted({m.channel for m in reps})
        change_content = canonical_bytes(
            {
                "component_scope": incident.components,
                "visible_change_refs": incident.changes,
            }
        ).decode("utf-8")
        change_digest = sha256(change_content.encode("utf-8"))
        prior_change = next(
            (
                e
                for e in incident.draft.get("visible_evidence", [])
                if e["type"] == "CHANGE_METADATA" and e["digest"] == change_digest
            ),
            None,
        )
        change_evidence = prior_change or {
            "evidence_id": f"evidence:{uuid4()}",
            "type": "CHANGE_METADATA",
            "digest": change_digest,
            "owner_scope_id": self.scope,
            "source_incident_id": incident.incident_id,
            "schema_version": "stage13.visible-changes.v1",
            "availability": "AVAILABLE",
            "content": change_content,
        }
        evidence.append(change_evidence)
        refs.append({k: change_evidence[k] for k in ("evidence_id", "digest", "type")})
        # Material digest 不含成员数量、时刻、随机 refs 或代表样本身份。
        incident.material_digest = sha256(
            canonical_bytes(
                {
                    "normalizer_version": NORMALIZER_VERSION,
                    "signature": incident.signature,
                    "components": incident.components,
                    "changes": incident.changes,
                    "evidence_complete": missing == 0,
                }
            )
        )
        incident.draft = {
            "schema_version": "stage13.triage-input.v1",
            "scope": {
                "automation_project_id": incident.project,
                "suite_id": incident.suite,
                "business_date": incident.business_date,
            },
            "environment_samples": list(
                {
                    (e["environment_id"], e["channel_group"]): e for e in environments
                }.values()
            ),
            "version_samples": sorted({m.product_version for m in reps}),
            "failure_summary": {
                "signature": incident.signature,
                "component_scope": incident.components,
                "case_counts": outcomes,
                "total_members": total,
                "represented_members": len(reps),
                "omitted_count": total - len(reps),
                "environment_count": env_count,
                "channel_distribution": channels,
                "product_version_distribution": versions,
                "evidence_completeness": "COMPLETE" if missing == 0 else "MISSING",
                "missing_case_count": missing,
                "evidence_truncated": total > len(reps),
                "omitted_types": (
                    ["ERROR_EXCERPT", "CI_SUMMARY"] if total > len(reps) else []
                ),
                "channel_coverage": {
                    "observed": sorted(channels),
                    "represented": represented_channels,
                    "omitted": sorted(set(channels) - set(represented_channels)),
                },
                "VersionExecution_refs": version_refs,
                "LocalCluster_refs": clusters,
            },
            "visible_evidence": evidence,
            "EvidenceRefs": refs,
            "visible_change_inventory": incident.changes,
            "evidence_policy": {
                "allowed_types": [
                    "CI_SUMMARY",
                    "ERROR_EXCERPT",
                    "FAILURE_DETAIL",
                    "AUTHORIZED_ARTIFACT",
                    "CHANGE_METADATA",
                ],
                "max_tool_reads": 8,
                "max_artifact_reads": 4,
                "max_bytes": 131072,
                "job_deadline_seconds": 180,
            },
        }
        if len(refs) > 32 or len(canonical_bytes(incident.draft)) > 128 * 1024 - 1024:
            raise ValueError("INCIDENT_INPUT_BYTE_BUDGET")

    async def seal(self, project, suite, day):
        async with self.database.transaction() as session:
            await self._day_lock(session, project, suite, day)
            cycles = (
                select(CycleRow.cycle_id)
                .join(GuardianRow)
                .where(
                    GuardianRow.scope == self.scope,
                    GuardianRow.project == project,
                    GuardianRow.suite == suite,
                    CycleRow.business_date == day,
                )
            )
            active = exists(cycles.where(CycleRow.status.not_in(CYCLE_TERMINAL)))
            pending = exists(
                select(VersionRow.version_execution_id).where(
                    VersionRow.cycle_id.in_(cycles),
                    VersionRow.status == "COMPLETED",
                    (
                        VersionRow.counts["FAILED"].as_integer()
                        + VersionRow.counts["ERROR"].as_integer()
                    )
                    > 0,
                    ~exists(
                        select(CollectionRow.version_id).where(
                            CollectionRow.version_id == VersionRow.version_execution_id,
                            CollectionRow.state.in_(("COMPLETE", "MISSING")),
                        )
                    ),
                )
            )
            if (await session.execute(select(or_(active, pending)))).scalar_one():
                return 0
            result = await session.execute(
                update(IncidentRow)
                .where(
                    IncidentRow.scope == self.scope,
                    IncidentRow.project == project,
                    IncidentRow.suite == suite,
                    IncidentRow.business_date == day,
                    IncidentRow.state == "OPEN",
                )
                .values(state="SEALED")
            )
            return result.rowcount

    async def admit(self, project, suite, day, *, subject_manifest=None, fault=None):
        subject = subject_manifest or CONTROLLED_SUBJECT
        if canonical_bytes(subject) != canonical_bytes(CONTROLLED_SUBJECT):
            raise ValueError("WP03_CONTROLLED_SUBJECT_ONLY")
        subject = json.loads(canonical_bytes(CONTROLLED_SUBJECT))
        digest = SUBJECT_DIGEST
        bkey = business_key(
            "analysis-budget", self.scope, project, suite, day, digest, "NIGHTLY"
        )
        async with self.database.transaction() as session:
            await self._day_lock(session, project, suite, day)
            await session.execute(
                insert(BudgetRow)
                .values(
                    budget_key=bkey,
                    scope=self.scope,
                    project=project,
                    suite=suite,
                    business_date=day,
                    subject_digest=digest,
                    lane="NIGHTLY",
                    admitted_total=0,
                )
                .on_conflict_do_nothing()
            )
            budget = await session.get(BudgetRow, bkey, with_for_update=True)
            now = await self._now(session)
            incidents = (
                (
                    await session.execute(
                        select(IncidentRow)
                        .where(
                            IncidentRow.scope == self.scope,
                            IncidentRow.project == project,
                            IncidentRow.suite == suite,
                            IncidentRow.business_date == day,
                            IncidentRow.first_seen_at <= now - timedelta(seconds=600),
                        )
                        .order_by(IncidentRow.first_seen_at, IncidentRow.incident_key)
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            eligible = 0
            for incident in incidents:
                if not incident.draft["failure_summary"]["total_members"]:
                    continue
                jobs = (
                    (
                        await session.execute(
                            select(AnalysisJobRow)
                            .where(
                                AnalysisJobRow.incident_id == incident.incident_id,
                                AnalysisJobRow.subject_digest == digest,
                                AnalysisJobRow.admitted_at.is_not(None),
                            )
                            .order_by(AnalysisJobRow.admitted_at)
                        )
                    )
                    .scalars()
                    .all()
                )
                if (
                    len(jobs) >= 2
                    or jobs
                    and now < jobs[-1].admitted_at + timedelta(seconds=1800)
                ):
                    continue
                latest = (
                    await session.get(
                        RevisionRow, (incident.incident_id, incident.evidence_revision)
                    )
                    if incident.evidence_revision
                    else None
                )
                if jobs and latest.material_digest == incident.material_digest:
                    continue
                if not latest or latest.material_digest != incident.material_digest:
                    incident.evidence_revision += 1
                    manifest = json.loads(canonical_bytes(incident.draft))
                    manifest_digest = sha256(canonical_bytes(manifest))
                    latest = RevisionRow(
                        incident_id=incident.incident_id,
                        revision=incident.evidence_revision,
                        material_digest=incident.material_digest,
                        manifest_digest=manifest_digest,
                        manifest=manifest,
                        frozen_at=now,
                    )
                    session.add(latest)
                    incident.evidence_manifest_digest = manifest_digest
                    await session.flush()
                akey = business_key(
                    "analysis", incident.incident_id, latest.revision, digest
                )
                existing = (
                    await session.execute(
                        select(AnalysisJobRow).where(
                            AnalysisJobRow.admission_key == akey
                        )
                    )
                ).scalar_one_or_none()
                if existing and existing.admitted_at:
                    continue
                eligible += 1
                if existing:
                    # 同日 hard budget 不退款；旧 deferred 不因重启获得新 slot。
                    continue
                payload = json.loads(canonical_bytes(latest.manifest))
                payload["incident_ref"] = {
                    "incident_id": incident.incident_id,
                    "evidence_revision": latest.revision,
                    "manifest_digest": latest.manifest_digest,
                }
                payload["evidence_policy"]["deadline_at"] = (
                    now + timedelta(seconds=180)
                ).isoformat()
                if len(canonical_bytes(payload)) > 128 * 1024:
                    raise ValueError("ANALYSIS_INPUT_BYTE_BUDGET")
                admitted = budget.admitted_total < 60
                session.add(
                    AnalysisJobRow(
                        job_id=str(uuid4()),
                        admission_key=akey,
                        incident_id=incident.incident_id,
                        revision=latest.revision,
                        subject_digest=digest,
                        subject_manifest=subject,
                        budget_key=bkey,
                        status="READY" if admitted else "DEFERRED_BUDGET",
                        kind="REANALYSIS" if jobs else "INITIAL",
                        admitted_at=now if admitted else None,
                        input=payload,
                        input_digest=sha256(canonical_bytes(payload)),
                    )
                )
                if admitted:
                    budget.admitted_total += 1
                    budget.cutoff = {
                        "first_seen_at": incident.first_seen_at.isoformat(),
                        "incident_key": incident.incident_key,
                    }
                if fault:
                    fault("after_revision_before_admission_commit")
            await session.flush()
            statuses = dict(
                (
                    await session.execute(
                        select(AnalysisJobRow.status, func.count())
                        .where(AnalysisJobRow.budget_key == bkey)
                        .group_by(AnalysisJobRow.status)
                    )
                ).all()
            )
            result = {
                "eligible_count": sum(statuses.values()),
                "new_eligible_count": eligible,
                "admitted_count": budget.admitted_total,
                "deferred_count": statuses.get("DEFERRED_BUDGET", 0),
                "cutoff": budget.cutoff,
                "statuses": statuses,
            }
        if fault:
            fault("after_admission_commit")
        return result
