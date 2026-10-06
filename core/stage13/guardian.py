"""Guardian daily cycle、短事务 due claim、对账与串行推进的业务 Owner。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
import hashlib
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from core.stage13.contracts import (
    CISummary,
    EvidencePacket,
    LookupResult,
    SubmitReceipt,
    SubmitRequest,
    business_key,
    canonical_bytes,
    sha256,
)
from core.stage13.guardian_models import (
    CycleRow,
    DueRow,
    GuardianRow,
    ObservationRow,
    OccupancyRow,
    SchedulerRow,
    VersionRow,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
CYCLE_TERMINAL = frozenset(
    {
        "SUCCEEDED",
        "COMPLETED_WITH_FAILURES",
        "FAILED",
        "UNRESOLVED",
        "CANCELLED",
        "SKIPPED_OVERLAP",
    }
)
VERSION_TERMINAL = frozenset(
    {
        "COMPLETED",
        "INFRA_FAILED",
        "DISPATCH_FAILED",
        "UNRESOLVED",
        "CANCELLED",
        "SKIPPED",
    }
)
READ_OPERATIONS = ("POLL_REMOTE", "RECONCILE_REMOTE", "FETCH_TERMINAL_RESULT")


def hash_offset(value: str, modulus: int) -> int:
    return (
        int.from_bytes(hashlib.sha256(value.encode("utf-8")).digest()[:8], "big")
        % modulus
    )


def poll_jitter(key: str, sequence: int) -> int:
    return hash_offset(business_key("poll-jitter", key, sequence), 61) - 30


def reconcile_jitter(key: str, sequence: int) -> int:
    return hash_offset(business_key("reconcile-jitter", key, sequence), 11) - 5


def eligibility(now: datetime) -> tuple[str, datetime]:
    day = now.astimezone(SHANGHAI).date()
    return day.isoformat(), datetime.combine(day, time(), SHANGHAI).astimezone(UTC)


@dataclass(frozen=True)
class WorkClaim:
    work_key: str
    token: str
    epoch: int
    version_id: str


@dataclass(frozen=True)
class PreparedWork:
    claim: WorkClaim
    operation: str
    payload: dict
    operation_identity: str


class StaleClaim(RuntimeError):
    pass


class GuardianScheduleService:
    """外部调用不在本类事务内；virtual clock 不改变 PostgreSQL lease 时间。"""

    def __init__(self, database, scope: str):
        self.database, self.scope = database, scope

    async def initialize(self, *, logical_now: datetime | None = None):
        async with self.database.transaction() as session:
            await session.execute(
                insert(SchedulerRow)
                .values(
                    scope=self.scope,
                    logical_now=logical_now,
                    submit_starts={},
                    read_starts={},
                    recovery_generation=0,
                    stale_writes_rejected=0,
                )
                .on_conflict_do_nothing()
            )
            state = await session.get(SchedulerRow, self.scope)
            if (state.logical_now is None) != (logical_now is None):
                raise ValueError("CLOCK_MODE_CONFLICT")

    async def _now(self, session):
        db_now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
        logical_now = (
            await session.execute(
                select(SchedulerRow.logical_now).where(SchedulerRow.scope == self.scope)
            )
        ).scalar_one()
        return db_now, logical_now or db_now

    async def advance_clock(self, now: datetime):
        if now.tzinfo is None:
            raise ValueError("logical clock 必须含时区")
        async with self.database.transaction() as session:
            state = await session.get(SchedulerRow, self.scope, with_for_update=True)
            if state.logical_now is None or now < state.logical_now:
                raise ValueError("logical clock 不允许倒退或切换实时时钟")
            state.logical_now = now

    async def register_guardian(
        self, project: str, suite: str, environment: str, channel: str
    ):
        key = business_key("guardian", self.scope, project, suite, environment)
        async with self.database.transaction() as session:
            await session.execute(
                insert(GuardianRow)
                .values(
                    guardian_id=str(uuid4()),
                    guardian_key=key,
                    scope=self.scope,
                    project=project,
                    suite=suite,
                    environment=environment,
                    channel=channel,
                    status="ACTIVE",
                )
                .on_conflict_do_nothing(index_elements=[GuardianRow.guardian_key])
            )
            row = (
                await session.execute(
                    select(GuardianRow).where(GuardianRow.guardian_key == key)
                )
            ).scalar_one()
            return row.guardian_id

    async def set_enabled(self, guardian_id: str, enabled: bool):
        async with self.database.transaction() as session:
            row = await session.get(GuardianRow, guardian_id, with_for_update=True)
            if row is None or row.scope != self.scope:
                raise ValueError("GUARDIAN_SCOPE_MISMATCH")
            row.status = "ACTIVE" if enabled else "DISABLED"

    async def discover(
        self,
        guardian_id: str,
        *,
        provider_namespace: str,
        versions: tuple[str, str, str],
        expected_cases: tuple[int, int, int],
        plan_revision: str,
    ):
        if (
            len(versions) != 3
            or len(set(versions)) != 3
            or len(expected_cases) != 3
            or any(type(n) is not int or n <= 0 for n in expected_cases)
        ):
            raise ValueError("当日必须冻结三个不同版本及 case counts")
        plan = {
            "versions": [
                {
                    "ordinal": i + 1,
                    "product_version": v,
                    "expected_cases": expected_cases[i],
                }
                for i, v in enumerate(versions)
            ],
            "version_plan_revision": plan_revision,
            "provider_namespace_id": provider_namespace,
        }
        digest = sha256(canonical_bytes(plan))
        async with self.database.transaction() as session:
            guardian = await session.get(GuardianRow, guardian_id, with_for_update=True)
            if guardian is None or guardian.scope != self.scope:
                raise ValueError("GUARDIAN_SCOPE_MISMATCH")
            _, now = await self._now(session)
            day, eligible = eligibility(now)
            key = business_key("cycle", guardian.guardian_key, day)
            existing = (
                await session.execute(select(CycleRow).where(CycleRow.cycle_key == key))
            ).scalar_one_or_none()
            if existing:
                if existing.plan_digest != digest:
                    raise ValueError("PLAN_CONFLICT")
                return existing.cycle_id
            if guardian.status != "ACTIVE":
                return None
            await session.execute(
                insert(OccupancyRow)
                .values(
                    scope=self.scope,
                    environment=guardian.environment,
                    safety_hold=False,
                    resolutions=[],
                )
                .on_conflict_do_nothing()
            )
            occupancy = await session.get(
                OccupancyRow, (self.scope, guardian.environment), with_for_update=True
            )
            resume_after = (
                occupancy.resolutions[-1].get("resume_after_business_date", "")
                if occupancy.resolutions
                else ""
            )
            blocked = (
                occupancy.cycle_id is not None
                or occupancy.safety_hold
                or day <= resume_after
            )
            cycle = CycleRow(
                cycle_id=str(uuid4()),
                cycle_key=key,
                guardian_id=guardian_id,
                business_date=day,
                plan=plan,
                plan_digest=digest,
                channel=guardian.channel,
                timezone="Asia/Shanghai",
                eligible_at=eligible,
                deadline_at=eligible + timedelta(hours=10),
                discovered_at=now,
                status="CREATED",
            )
            session.add(cycle)
            await session.flush()
            local_versions = []
            for ordinal, product_version in enumerate(versions, 1):
                version_key = business_key("version", key, ordinal, product_version)
                remote_key = business_key(
                    "remote",
                    provider_namespace,
                    self.scope,
                    guardian.project,
                    guardian.suite,
                    guardian.environment,
                    key,
                    ordinal,
                    product_version,
                )
                intent = dict(
                    provider_namespace_id=provider_namespace,
                    owner_scope_id=self.scope,
                    automation_project_id=guardian.project,
                    suite_id=guardian.suite,
                    environment_id=guardian.environment,
                    channel_group=guardian.channel,
                    cycle_key=key,
                    version_execution_key=version_key,
                    ordinal=ordinal,
                    product_version=product_version,
                    remote_execution_business_key=remote_key,
                    parameters={},
                )
                request = SubmitRequest(
                    **intent, request_digest=sha256(canonical_bytes(intent))
                )
                row = VersionRow(
                    version_execution_id=str(uuid4()),
                    version_execution_key=version_key,
                    cycle_id=cycle.cycle_id,
                    ordinal=ordinal,
                    product_version=product_version,
                    expected_cases=expected_cases[ordinal - 1],
                    request=request.model_dump(mode="json"),
                    business_key=remote_key,
                    request_digest=request.request_digest,
                    status="SKIPPED" if blocked else "PLANNED",
                    reason="SKIPPED_OVERLAP" if blocked else None,
                )
                session.add(row)
                local_versions.append(row)
            await session.flush()
            if blocked:
                cycle.status, cycle.reason, cycle.completed_at = (
                    "SKIPPED_OVERLAP",
                    "SKIPPED_OVERLAP",
                    now,
                )
            else:
                occupancy.cycle_id = cycle.cycle_id
                await self._due(
                    session,
                    cycle,
                    local_versions[0],
                    "DISPATCH_VERSION",
                    0,
                    eligible + timedelta(seconds=hash_offset(key, 480)),
                )
                cycle.status = "READY"
            return cycle.cycle_id

    async def _due(self, session, cycle, version, operation, sequence, due):
        key = business_key("due", version.version_execution_key, operation, sequence)
        await session.execute(
            insert(DueRow)
            .values(
                work_key=key,
                scope=self.scope,
                operation=operation,
                version_execution_id=version.version_execution_id,
                sequence=sequence,
                eligible_at=cycle.eligible_at,
                original_due_at=due,
                next_available_at=due,
                state="READY",
                claim_epoch=0,
                recovery_generation=0,
                takeovers=0,
            )
            .on_conflict_do_nothing()
        )
        return key

    async def claim_due(self, limit=20):
        if not 1 <= limit <= 100:
            raise ValueError("claim batch 必须在 1..100")
        async with self.database.transaction() as session:
            db_now, now = await self._now(session)
            rows = (
                (
                    await session.execute(
                        select(DueRow)
                        .where(
                            DueRow.scope == self.scope,
                            DueRow.completed_at.is_(None),
                            DueRow.next_available_at <= now,
                            or_(
                                DueRow.state == "READY",
                                and_(
                                    DueRow.state == "CLAIMED",
                                    DueRow.lease_until <= db_now,
                                ),
                            ),
                        )
                        .order_by(DueRow.next_available_at, DueRow.work_key)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            # 锁后取 DB 时间；等待锁不能延长旧 lease 的有效性。
            db_now = (
                await session.execute(select(func.clock_timestamp()))
            ).scalar_one()
            claims = []
            for row in rows:
                if row.state == "CLAIMED" and row.lease_until > db_now:
                    continue
                row.takeovers += int(row.state == "CLAIMED")
                row.state, row.claim_token = "CLAIMED", str(uuid4())
                row.claim_epoch += 1
                row.claimed_at, row.lease_until = db_now, db_now + timedelta(seconds=30)
                claims.append(
                    WorkClaim(
                        row.work_key,
                        row.claim_token,
                        row.claim_epoch,
                        row.version_execution_id,
                    )
                )
            return claims

    async def _locked(self, session, claim):
        work = await session.get(DueRow, claim.work_key, with_for_update=True)
        db_now, now = await self._now(session)
        if (
            work is None
            or work.scope != self.scope
            or work.state != "CLAIMED"
            or work.claim_token != claim.token
            or work.claim_epoch != claim.epoch
            or work.lease_until <= db_now
            or work.version_execution_id != claim.version_id
        ):
            raise StaleClaim("STALE_WORK_CLAIM")
        cycle_id = (
            await session.execute(
                select(VersionRow.cycle_id).where(
                    VersionRow.version_execution_id == claim.version_id
                )
            )
        ).scalar_one()
        cycle = await session.get(CycleRow, cycle_id, with_for_update=True)
        version = await session.get(VersionRow, claim.version_id, with_for_update=True)
        db_now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
        if work.lease_until <= db_now:
            raise StaleClaim("WORK_LEASE_EXPIRED_AFTER_LOCK")
        return work, cycle, version, db_now, now

    async def _record_stale(self):
        async with self.database.transaction() as session:
            await session.execute(
                update(SchedulerRow)
                .where(SchedulerRow.scope == self.scope)
                .values(stale_writes_rejected=SchedulerRow.stale_writes_rejected + 1)
            )

    @staticmethod
    def _unknown(version, now):
        if version.knowledge_state != "UNKNOWN":
            version.unknown_entries += 1
        version.knowledge_state = "UNKNOWN"
        version.first_unknown_at = version.first_unknown_at or now

    @staticmethod
    def _known(version):
        version.unknown_recoveries += int(version.knowledge_state == "UNKNOWN")
        version.knowledge_state = "KNOWN"

    @staticmethod
    def _unknown_expired(version, cycle, now):
        return (
            version.reconciliation_requests >= 60
            or now >= cycle.deadline_at
            or (
                version.first_unknown_at is not None
                and now >= version.first_unknown_at + timedelta(seconds=1800)
            )
        )

    async def prepare(self, claim):
        """durable intent/attempt/admission 必须先 commit，再进入 GovernedToolInvoker。"""
        try:
            async with self.database.transaction() as session:
                lane = await session.get(SchedulerRow, self.scope, with_for_update=True)
                work, cycle, version, db_now, now = await self._locked(session, claim)
                if cycle.status in CYCLE_TERMINAL or version.status in VERSION_TERMINAL:
                    work.state, work.completed_at = "COMPLETED", db_now
                    return None
                operation = work.operation
                if operation == "DISPATCH_VERSION" and work.started_at is not None:
                    self._unknown(version, version.intent_at or now)
                    operation = "RECONCILE_REMOTE"
                if version.knowledge_state == "UNKNOWN":
                    operation = "RECONCILE_REMOTE"
                terminal_known = version.last_observed_state in {
                    "COMPLETED",
                    "INFRA_FAILED",
                    "CANCELLED",
                }
                if (
                    terminal_known
                    and operation == "FETCH_TERMINAL_RESULT"
                    and now > cycle.deadline_at
                ):
                    await self._stop(
                        session,
                        cycle,
                        version,
                        "INFRA_FAILED",
                        "RESULT_COLLECTION_FAILED",
                        now,
                    )
                    work.state, work.completed_at = "COMPLETED", db_now
                    return None
                if not terminal_known and (
                    now >= cycle.deadline_at
                    or (
                        operation == "RECONCILE_REMOTE"
                        and self._unknown_expired(version, cycle, now)
                    )
                ):
                    await self._stop(
                        session,
                        cycle,
                        version,
                        "UNRESOLVED",
                        "RECONCILIATION_DEADLINE",
                        now,
                        hold=True,
                    )
                    work.state, work.completed_at = "COMPLETED", db_now
                    return None
                is_submit = operation == "DISPATCH_VERSION"
                lane_starts = lane.submit_starts if is_submit else lane.read_starts
                starts = lane_starts.get("business", [])
                recent = [
                    s
                    for s in starts
                    if datetime.fromisoformat(s) > now - timedelta(seconds=1)
                ]
                maximum = 10 if is_submit else 20
                submit_busy = and_(
                    DueRow.operation == "DISPATCH_VERSION",
                    VersionRow.knowledge_state == "KNOWN",
                )
                read_busy = or_(
                    DueRow.operation.in_(READ_OPERATIONS),
                    and_(
                        DueRow.operation == "DISPATCH_VERSION",
                        VersionRow.knowledge_state == "UNKNOWN",
                    ),
                )
                busy = (
                    await session.execute(
                        select(func.count())
                        .select_from(DueRow)
                        .join(VersionRow)
                        .where(
                            DueRow.scope == self.scope,
                            DueRow.state == "CLAIMED",
                            DueRow.started_at.is_not(None),
                            DueRow.lease_until > db_now,
                            DueRow.work_key != work.work_key,
                            submit_busy if is_submit else read_busy,
                        )
                    )
                ).scalar_one()
                if len(recent) >= maximum or busy >= maximum:
                    work.state, work.claim_token, work.lease_until = "READY", None, None
                    work.next_available_at = now + timedelta(seconds=1)
                    return None
                recent.append(now.isoformat())
                if is_submit:
                    lane.submit_starts = {**lane_starts, "business": recent}
                else:
                    lane.read_starts = {**lane_starts, "business": recent}
                if is_submit:
                    if version.submit_attempts >= 3:
                        await self._stop(
                            session,
                            cycle,
                            version,
                            "DISPATCH_FAILED",
                            "DISPATCH_BUDGET_EXHAUSTED",
                            now,
                        )
                        work.state, work.completed_at = "COMPLETED", db_now
                        return None
                    version.submit_attempts += 1
                    version.intent_at = version.intent_at or now
                    version.status, cycle.status = "DISPATCHING", "RUNNING"
                    payload = version.request
                else:
                    if operation == "RECONCILE_REMOTE":
                        version.reconciliation_requests += 1
                    else:
                        version.poll_requests += int(operation == "POLL_REMOTE")
                        version.last_poll_at = now
                    payload = {
                        "remote_execution_business_key": version.business_key,
                        "request_digest": version.request_digest,
                    }
                    if operation != "RECONCILE_REMOTE":
                        payload["remote_execution_id"] = version.remote_id
                work.started_at, work.business_started_at = db_now, now
                # 同一 work reclaim lookup 使用当前 claim identity，不能重放陈旧 read。
                identity = f"stage13:{work.work_key}:{operation}" + (
                    f":{claim.epoch}" if not is_submit else ""
                )
                return PreparedWork(claim, operation, payload, identity)
        except StaleClaim:
            await self._record_stale()
            raise

    async def finish(
        self, prepared, result=None, *, error=None, uncertain=False, fault=None
    ):
        try:
            async with self.database.transaction() as session:
                work, cycle, version, db_now, now = await self._locked(
                    session, prepared.claim
                )
                if cycle.status in CYCLE_TERMINAL or version.status in VERSION_TERMINAL:
                    raise StaleClaim("TERMINAL_OR_BINDING_CHANGED")
                operation = prepared.operation
                if error:
                    work.error = error
                    if operation == "DISPATCH_VERSION":
                        if uncertain:
                            self._unknown(version, version.intent_at or now)
                            await self._reconcile_due(session, cycle, version, now)
                        else:
                            await self._stop(
                                session, cycle, version, "DISPATCH_FAILED", error, now
                            )
                    elif operation == "RECONCILE_REMOTE":
                        version.reconciliation_errors += 1
                        self._unknown(version, now)
                        await self._reconcile_due(session, cycle, version, now)
                    elif operation == "FETCH_TERMINAL_RESULT":
                        await self._result_retry(session, cycle, version, now)
                    else:
                        version.consecutive_poll_errors += 1
                        last = version.last_successful_poll_at or version.bound_at
                        if now >= last + timedelta(seconds=900):
                            self._unknown(version, last + timedelta(seconds=900))
                            await self._reconcile_due(session, cycle, version, now)
                        else:
                            await self._poll_due(
                                session, cycle, version, now, backoff=True
                            )
                elif operation == "DISPATCH_VERSION":
                    try:
                        receipt = SubmitReceipt.model_validate(result)
                        self._check_receipt(version, receipt)
                    except ValueError:
                        await self._stop(
                            session,
                            cycle,
                            version,
                            "UNRESOLVED",
                            "PROVIDER_RECEIPT_CONFLICT",
                            now,
                            hold=True,
                        )
                    else:
                        await self._bind(session, cycle, version, receipt, now)
                elif operation == "RECONCILE_REMOTE":
                    lookup = LookupResult.model_validate(result)
                    if lookup.receipt or lookup.seal_receipt:
                        try:
                            self._check_receipt(
                                version, lookup.receipt or lookup.seal_receipt
                            )
                        except ValueError:
                            await self._stop(
                                session,
                                cycle,
                                version,
                                "UNRESOLVED",
                                "PROVIDER_IDENTITY_CONFLICT",
                                now,
                                hold=True,
                            )
                            work.state, work.completed_at = "COMPLETED", db_now
                            return
                    if lookup.result == "FOUND":
                        version.reconciliation_errors = 0
                        await self._bind(session, cycle, version, lookup.receipt, now)
                    elif lookup.result == "NOT_CREATED_FINAL":
                        self._check_receipt(version, lookup.seal_receipt)
                        await self._stop(
                            session,
                            cycle,
                            version,
                            "DISPATCH_FAILED",
                            "NOT_CREATED_FINAL",
                            now,
                        )
                    elif version.remote_id is None and version.submit_attempts < 3:
                        # 权威 NOT_CREATED 只允许同 key 重发；不用于 hold resolution。
                        self._known(version)
                        delay = (30, 120)[min(version.submit_attempts - 1, 1)]
                        await self._due(
                            session,
                            cycle,
                            version,
                            "DISPATCH_VERSION",
                            version.submit_attempts,
                            now + timedelta(seconds=delay),
                        )
                    elif version.remote_id is None:
                        # 普通 NOT_CREATED 不排除旧 in-flight 请求，保留安全 hold。
                        await self._stop(
                            session,
                            cycle,
                            version,
                            "UNRESOLVED",
                            "DISPATCH_BUDGET_UNCERTAIN",
                            now,
                            hold=True,
                        )
                    else:
                        await self._reconcile_due(session, cycle, version, now)
                else:
                    try:
                        packet, summary = self._summary(version, result)
                    except ValueError:
                        if (
                            operation == "FETCH_TERMINAL_RESULT"
                            or version.last_observed_state == "COMPLETED"
                        ):
                            await self._result_retry(session, cycle, version, now)
                        else:
                            version.consecutive_poll_errors += 1
                            last = version.last_successful_poll_at or version.bound_at
                            if now >= last + timedelta(seconds=900):
                                self._unknown(version, last + timedelta(seconds=900))
                                await self._reconcile_due(session, cycle, version, now)
                            else:
                                await self._poll_due(
                                    session, cycle, version, now, backoff=True
                                )
                    else:
                        await self._observe(
                            session, work, cycle, version, packet, summary, db_now, now
                        )
                work.state, work.completed_at = "COMPLETED", db_now
                if fault:
                    fault("before_progression_commit")
            if fault:
                fault("after_progression_commit")
        except StaleClaim:
            await self._record_stale()
            raise

    async def admit_http(self, *, submit: bool):
        """真实模式在 HTTP request hook 再限制 start，防止 Tool 准备延迟压缩请求。"""
        async with self.database.transaction() as session:
            state = await session.get(SchedulerRow, self.scope, with_for_update=True)
            if state.logical_now is not None:
                # logical profile 已由 prepare 的 business clock 限额，不声明真实容量。
                return 0.0
            now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            record = state.submit_starts if submit else state.read_starts
            recent = [
                s
                for s in record.get("http", [])
                if datetime.fromisoformat(s) > now - timedelta(seconds=1)
            ]
            maximum = 10 if submit else 20
            if len(recent) >= maximum:
                return max(
                    0.001,
                    (
                        datetime.fromisoformat(recent[0]) + timedelta(seconds=1) - now
                    ).total_seconds(),
                )
            recent.append(now.isoformat())
            if submit:
                state.submit_starts = {**record, "http": recent}
            else:
                state.read_starts = {**record, "http": recent}
            return 0.0

    @staticmethod
    def _check_receipt(version, receipt):
        if (
            receipt.provider_namespace_id != version.request["provider_namespace_id"]
            or receipt.remote_execution_business_key != version.business_key
            or receipt.request_digest != version.request_digest
        ):
            raise ValueError("PROVIDER_RECEIPT_CONFLICT")
        if (
            isinstance(receipt, SubmitReceipt)
            and version.receipt is not None
            and version.receipt != receipt.model_dump(mode="json")
        ):
            raise ValueError("PROVIDER_IDENTITY_CONFLICT")

    async def _bind(self, session, cycle, version, receipt, now):
        self._check_receipt(version, receipt)
        if version.remote_id is not None and version.remote_id != str(
            receipt.remote_execution_id
        ):
            await self._stop(
                session,
                cycle,
                version,
                "UNRESOLVED",
                "PROVIDER_IDENTITY_CONFLICT",
                now,
                hold=True,
            )
            return
        version.remote_id = str(receipt.remote_execution_id)
        version.receipt = receipt.model_dump(mode="json")
        version.binding_status, version.status = "BOUND", "ACTIVE"
        self._known(version)
        version.bound_at = version.bound_at or now
        if version.last_observed_state == "COMPLETED":
            await self._due(
                session,
                cycle,
                version,
                "FETCH_TERMINAL_RESULT",
                version.result_retries,
                now,
            )
        else:
            await self._poll_due(session, cycle, version, now)

    async def _reconcile_due(self, session, cycle, version, now):
        if self._unknown_expired(version, cycle, now):
            await self._stop(
                session,
                cycle,
                version,
                "UNRESOLVED",
                "RECONCILIATION_DEADLINE",
                now,
                hold=True,
            )
            return
        delay = (
            (30, 60, 120, 300)[min(max(version.reconciliation_errors - 1, 0), 3)]
            if version.reconciliation_errors
            else 30
            + reconcile_jitter(
                version.version_execution_key, version.reconciliation_requests
            )
        )
        deadline = min(
            cycle.deadline_at, version.first_unknown_at + timedelta(seconds=1800)
        )
        await self._due(
            session,
            cycle,
            version,
            "RECONCILE_REMOTE",
            version.reconciliation_requests,
            min(now + timedelta(seconds=delay), deadline),
        )

    async def _poll_due(self, session, cycle, version, now, *, backoff=False):
        version.poll_sequence += 1
        delay = (
            (30, 60, 120, 300)[min(version.consecutive_poll_errors - 1, 3)]
            if backoff
            else 300 + poll_jitter(version.version_execution_key, version.poll_sequence)
        )
        version.next_poll_at = min(now + timedelta(seconds=delay), cycle.deadline_at)
        await self._due(
            session,
            cycle,
            version,
            "POLL_REMOTE",
            version.poll_sequence,
            version.next_poll_at,
        )

    @staticmethod
    def _summary(version, value):
        packet = EvidencePacket.model_validate(value)
        summary = CISummary.model_validate_json(packet.content)
        request = version.request
        if (
            packet.owner_scope_id != request["owner_scope_id"]
            or str(packet.remote_execution_id) != version.remote_id
            or packet.type != "CI_SUMMARY"
        ):
            raise ValueError("SUMMARY_BINDING_INVALID")
        for field in (
            "environment_id",
            "channel_group",
            "automation_project_id",
            "suite_id",
            "ordinal",
            "product_version",
        ):
            if getattr(summary, field) != request[field]:
                raise ValueError("SUMMARY_PLAN_INVALID")
        if (
            str(summary.remote_execution_id) != version.remote_id
            or summary.status_revision < version.last_status_revision
            or summary.result_revision < version.last_result_revision
        ):
            raise ValueError("SUMMARY_REVISION_INVALID")
        if (
            version.last_observed_state in {"COMPLETED", "INFRA_FAILED", "CANCELLED"}
            and version.last_observed_state != summary.remote_state
        ):
            raise ValueError("REMOTE_TERMINAL_REWRITE")
        fields = (
            "remote_state",
            "status_revision",
            "result_revision",
            "result_available",
            "case_counts",
            "failure_index",
        )
        semantic = summary.model_dump(mode="json", include=set(fields))
        if sha256(canonical_bytes(semantic)) != summary.visible_semantic_digest:
            raise ValueError("SUMMARY_SEMANTIC_DIGEST_INVALID")
        return packet, summary

    async def _observe(
        self, session, work, cycle, version, packet, summary, db_now, now
    ):
        changed = version.last_observed_digest != summary.visible_semantic_digest
        session.add(
            ObservationRow(
                work_key=work.work_key,
                version_execution_id=version.version_execution_id,
                read_at=datetime.now(UTC),
                business_read_at=now,
                source_observed_at=packet.observed_at,
                semantic_digest=summary.visible_semantic_digest,
                packet_digest=packet.digest,
                changed=changed,
                evidence=packet.model_dump(mode="json") if changed else None,
            )
        )
        version.successful_polls += int(work.operation == "POLL_REMOTE")
        version.last_successful_poll_at, version.consecutive_poll_errors = now, 0
        version.last_observed_state, version.last_observed_digest = (
            summary.remote_state,
            summary.visible_semantic_digest,
        )
        version.last_status_revision, version.last_result_revision = (
            summary.status_revision,
            summary.result_revision,
        )
        self._known(version)
        if summary.remote_state == "COMPLETED":
            counts = summary.case_counts
            valid = (
                summary.result_available
                and summary.result_revision >= 1
                and counts is not None
                and sum(counts.model_dump().values()) == version.expected_cases
            )
            if valid:
                index = summary.failure_index
                valid = (
                    len(index) == counts.FAILED + counts.ERROR
                    and len({x.provider_case_id for x in index}) == len(index)
                    and sum(x.outcome == "FAILED" for x in index) == counts.FAILED
                )
            if not valid:
                await self._result_retry(session, cycle, version, now)
                return
            version.status, version.completed_at = "COMPLETED", now
            version.counts, version.terminal_summary = (
                counts.model_dump(),
                summary.model_dump(mode="json"),
            )
            await session.flush()
            await self._progress(session, cycle, version, now)
        elif summary.remote_state in {"INFRA_FAILED", "CANCELLED"}:
            await self._stop(
                session,
                cycle,
                version,
                summary.remote_state,
                f"REMOTE_{summary.remote_state}",
                now,
            )
        else:
            await self._poll_due(session, cycle, version, now)

    async def _result_retry(self, session, cycle, version, now):
        if version.result_retries >= 3 or now >= cycle.deadline_at:
            await self._stop(
                session, cycle, version, "INFRA_FAILED", "RESULT_COLLECTION_FAILED", now
            )
            return
        delay = (30, 120, 300)[version.result_retries]
        version.result_retries += 1
        await self._due(
            session,
            cycle,
            version,
            "FETCH_TERMINAL_RESULT",
            version.result_retries,
            min(now + timedelta(seconds=delay), cycle.deadline_at),
        )

    async def _progress(self, session, cycle, version, now):
        if version.ordinal < 3:
            successor = (
                await session.execute(
                    select(VersionRow).where(
                        VersionRow.cycle_id == cycle.cycle_id,
                        VersionRow.ordinal == version.ordinal + 1,
                    )
                )
            ).scalar_one()
            if successor.status == "PLANNED":
                await self._due(
                    session,
                    cycle,
                    successor,
                    "DISPATCH_VERSION",
                    0,
                    version.completed_at,
                )
        else:
            versions = (
                (
                    await session.execute(
                        select(VersionRow).where(VersionRow.cycle_id == cycle.cycle_id)
                    )
                )
                .scalars()
                .all()
            )
            if not all(v.status == "COMPLETED" for v in versions):
                raise ValueError("SERIAL_PROGRESSION_CONFLICT")
            failures = any(v.counts["FAILED"] or v.counts["ERROR"] for v in versions)
            cycle.status = "COMPLETED_WITH_FAILURES" if failures else "SUCCEEDED"
            cycle.completed_at = now
            await self._release(session, cycle)

    async def _occupancy(self, session, cycle):
        guardian = await session.get(GuardianRow, cycle.guardian_id)
        return await session.get(
            OccupancyRow, (self.scope, guardian.environment), with_for_update=True
        )

    async def _release(self, session, cycle):
        occupancy = await self._occupancy(session, cycle)
        if occupancy.cycle_id == cycle.cycle_id and not occupancy.safety_hold:
            occupancy.cycle_id = None

    async def _stop(self, session, cycle, version, status, reason, now, *, hold=False):
        version.status, version.reason, version.completed_at = status, reason, now
        cycle.status = (
            "UNRESOLVED"
            if status == "UNRESOLVED"
            else "CANCELLED" if status == "CANCELLED" else "FAILED"
        )
        cycle.reason, cycle.completed_at = reason, now
        await session.execute(
            update(VersionRow)
            .where(
                VersionRow.cycle_id == cycle.cycle_id, VersionRow.status == "PLANNED"
            )
            .values(status="SKIPPED", reason=reason, completed_at=now)
        )
        occupancy = await self._occupancy(session, cycle)
        if hold:
            occupancy.safety_hold = True
            occupancy.hold_acquired_at = occupancy.hold_acquired_at or now
        elif occupancy.cycle_id == cycle.cycle_id:
            occupancy.cycle_id = None

    async def recover(self, recovery_id: str, limit=100):
        """一次启动 generation；同 ID 分批扫描不能重复后移。"""
        if not 1 <= limit <= 100 or not recovery_id or len(recovery_id) > 128:
            raise ValueError("recovery ID / batch 无效")
        async with self.database.transaction() as session:
            state = await session.get(SchedulerRow, self.scope, with_for_update=True)
            if state.recovery_id != recovery_id:
                state.recovery_id = recovery_id
                state.recovery_generation += 1
            db_now, now = await self._now(session)
            generation = state.recovery_generation
            rows = (
                (
                    await session.execute(
                        select(DueRow)
                        .join(VersionRow)
                        .join(CycleRow)
                        .where(
                            DueRow.scope == self.scope,
                            DueRow.operation == "POLL_REMOTE",
                            DueRow.completed_at.is_(None),
                            DueRow.original_due_at <= now,
                            DueRow.recovery_generation < generation,
                            CycleRow.status.not_in(CYCLE_TERMINAL),
                            or_(DueRow.state == "READY", DueRow.lease_until <= db_now),
                        )
                        .order_by(DueRow.next_available_at, DueRow.work_key)
                        .limit(limit)
                        .with_for_update(of=DueRow, skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                row.next_available_at = max(now, row.original_due_at) + timedelta(
                    seconds=hash_offset(
                        business_key("recovery-spread", row.work_key, generation), 301
                    )
                )
                row.recovery_generation = generation
            # predecessor terminal + missing successor work 的补齐也限定 batch。
            predecessor = VersionRow.__table__.alias("predecessor")
            successor = VersionRow.__table__.alias("successor")
            missing = select(DueRow.work_key).where(
                DueRow.version_execution_id == successor.c.version_execution_id,
                DueRow.operation == "DISPATCH_VERSION",
            )
            ids = (
                (
                    await session.execute(
                        select(predecessor.c.version_execution_id)
                        .join(
                            successor,
                            and_(
                                successor.c.cycle_id == predecessor.c.cycle_id,
                                successor.c.ordinal == predecessor.c.ordinal + 1,
                            ),
                        )
                        .join(CycleRow, CycleRow.cycle_id == predecessor.c.cycle_id)
                        .where(
                            predecessor.c.status == "COMPLETED",
                            successor.c.status == "PLANNED",
                            CycleRow.status == "RUNNING",
                            ~missing.exists(),
                        )
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            for version_id in ids:
                version = await session.get(VersionRow, version_id)
                cycle = await session.get(
                    CycleRow, version.cycle_id, with_for_update=True
                )
                if cycle.status == "RUNNING":
                    await self._progress(session, cycle, version, now)
            return len(rows) + len(ids)

    async def resolve_hold(
        self,
        version_id: str,
        *,
        lookup: LookupResult,
        terminal_packet: EvidencePacket | None = None,
        source: str,
    ):
        """operator-only：consume seal/terminal proof，旧 terminal 永不复活。"""
        async with self.database.transaction() as session:
            version = await session.get(VersionRow, version_id)
            if version is None or version.request["owner_scope_id"] != self.scope:
                raise ValueError("RESOLUTION_SCOPE_MISMATCH")
            cycle = await session.get(CycleRow, version.cycle_id, with_for_update=True)
            if cycle.status != "UNRESOLVED":
                raise ValueError("RESOLUTION_REQUIRES_UNRESOLVED")
            occupancy = await self._occupancy(session, cycle)
            if not occupancy.safety_hold:
                return False
            if occupancy.cycle_id != cycle.cycle_id:
                raise ValueError("RESOLUTION_OCCUPANCY_CONFLICT")
            if lookup.result == "NOT_CREATED_FINAL":
                self._check_receipt(version, lookup.seal_receipt)
                proof = lookup.model_dump(mode="json")
            elif lookup.result == "FOUND" and terminal_packet is not None:
                self._check_receipt(version, lookup.receipt)
                if version.remote_id is not None and version.remote_id != str(
                    lookup.receipt.remote_execution_id
                ):
                    raise ValueError("RESOLUTION_REMOTE_CONFLICT")
                # 验证 proof，不能修改 frozen Version 的旧 binding/状态。
                shadow = VersionRow(
                    **{
                        c.name: getattr(version, c.name)
                        for c in VersionRow.__table__.columns
                    }
                )
                shadow.remote_id = str(lookup.receipt.remote_execution_id)
                _, summary = self._summary(
                    shadow, terminal_packet.model_dump(mode="json")
                )
                if summary.remote_state not in {
                    "COMPLETED",
                    "INFRA_FAILED",
                    "CANCELLED",
                }:
                    raise ValueError("RESOLUTION_NOT_TERMINAL")
                proof = {
                    "lookup": lookup.model_dump(mode="json"),
                    "terminal": terminal_packet.model_dump(mode="json"),
                }
            else:
                raise ValueError("RESOLUTION_REQUIRES_FINAL_PROOF")
            db_now, business_now = await self._now(session)
            occupancy.resolutions = [
                *occupancy.resolutions,
                {
                    "cycle_id": cycle.cycle_id,
                    "version_id": version_id,
                    "source": source,
                    "receipt": proof,
                    "digest": sha256(canonical_bytes(proof)),
                    "resolved_at": db_now.isoformat(),
                    "resume_after_business_date": eligibility(business_now)[0],
                },
            ]
            occupancy.safety_hold, occupancy.cycle_id = False, None
            return True
