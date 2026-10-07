"""逐 Attempt deadline、回执绑定与恢复；仅使用隔离 PostgreSQL。"""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from core.stage13.execution_policy import (
    POLICY_VERSION,
    bind_execution_policy,
    semantic_input,
)
from core.stage13.triage_models import TriageRunRow
from core.stage13.triage_subject import digest
from core.stage13.contracts import canonical_bytes
from tests.test_stage13_wp04_triage import assembly, output, payload


def request(p, manifest, start=None):
    start = start or datetime.now(timezone.utc)
    policy = dict(
        version=POLICY_VERSION,
        evaluation_run_id=str(uuid4()),
        evaluation_attempt_id=str(uuid4()),
        timeout_seconds=180,
        execution_started_at=start.isoformat(),
        execution_deadline_at=(start + timedelta(seconds=180)).isoformat(),
        semantic_input_digest=digest(semantic_input(p)),
    )
    p = deepcopy(p)
    p["evidence_policy"]["deadline_at"] = policy["execution_deadline_at"]
    return dict(
        run_id=policy["evaluation_attempt_id"],
        agent_id=manifest["agent_id"],
        query=canonical_bytes(p).decode(),
        timeout_seconds=180,
        expected_subject_manifest=manifest,
        execution_policy=policy,
    )


def test_semantic_input_and_execution_request_binding():
    p = payload()
    old = deepcopy(p)
    a = request(p, {"agent_id": "baseline"})
    b = request(
        p,
        {"agent_id": "candidate"},
        datetime.now(timezone.utc) + timedelta(seconds=120),
    )
    left, right = bind_execution_policy(a), bind_execution_policy(b)
    assert (
        left["policy"]["semantic_input_digest"]
        == right["policy"]["semantic_input_digest"]
    )
    assert left["execution_request_digest"] != right["execution_request_digest"]
    assert p == old
    a["execution_policy"]["execution_deadline_at"] = b["execution_policy"][
        "execution_deadline_at"
    ]
    with pytest.raises(ValueError, match="EXECUTION_POLICY_BINDING_MISMATCH"):
        bind_execution_policy(a)


def test_execution_policy_receipt_repair_replay_and_restart(clean_database):
    async def run():
        p = payload()
        service, calls, client = await assembly(
            clean_database, ["invalid", json.dumps(output(p))]
        )
        try:
            body = request(p, service.manifests["ci_triage_candidate"])
            frozen = bind_execution_policy(body)
            # 真实 reservation 后重新装配消费者：期限使用同一 durable row。
            await service.reserve_run(
                body["run_id"],
                body["query"],
                body["expected_subject_manifest"],
                attempt_id=body["run_id"],
                lane="OFFLINE",
                execution_policy=frozen,
            )
            await service.services.close(timeout=5)
            await client.aclose()
            assert calls == []
            service, calls, client = await assembly(
                clean_database, ["invalid", json.dumps(output(p))]
            )
            async with clean_database.session() as session:
                row = await session.get(TriageRunRow, body["run_id"])
                assert row.deadline_at == datetime.fromisoformat(
                    body["execution_policy"]["execution_deadline_at"]
                )
                # DB guard 拒绝延长，失败事务仅在本测试库。
                with pytest.raises(Exception, match="STAGE13_TRIAGE_IMMUTABLE"):
                    await session.execute(
                        text(
                            "UPDATE stage13_triage_runs SET deadline_at=deadline_at+interval '60 seconds' WHERE run_id=:id"
                        ),
                        {"id": body["run_id"]},
                    )
                await session.rollback()
            response = await service.evaluation_execute(**body)
            receipts = [
                response["actual_subject_receipt"],
                *response["child_subject_receipts"],
            ]
            assert len(receipts) == 2
            assert all(
                r["semantic_input_digest"] == frozen["policy"]["semantic_input_digest"]
                and r["execution_request_digest"] == frozen["execution_request_digest"]
                and r["execution_policy"] == frozen["policy"]
                for r in receipts
            )
            assert await service.evaluation_execute(**body) == response
            assert len(calls) == 2
            async with clean_database.session() as session:
                rows = (await session.execute(select(TriageRunRow))).scalars().all()
                assert len({r.deadline_at for r in rows}) == 1
                assert len({r.input_digest for r in rows}) == 1
        finally:
            await service.services.close(timeout=5)
            await client.aclose()

    asyncio.run(run())


def test_expired_execution_never_dispatches_model(clean_database):
    async def run():
        p = payload()
        service, calls, client = await assembly(clean_database, [json.dumps(output(p))])
        try:
            body = request(
                p,
                service.manifests["ci_triage_candidate"],
                datetime.now(timezone.utc) - timedelta(seconds=181),
            )
            with pytest.raises((TimeoutError, ValueError)):
                await service.evaluation_execute(**body)
            assert calls == []
            async with clean_database.session() as session:
                row = await session.get(TriageRunRow, body["run_id"])
                assert row.deadline_at == datetime.fromisoformat(
                    body["execution_policy"]["execution_deadline_at"]
                )
        finally:
            await service.services.close(timeout=5)
            await client.aclose()

    asyncio.run(run())
