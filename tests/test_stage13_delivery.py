"""WP06 授权、绑定、幂等、恢复与竞争；只使用隔离 PostgreSQL。"""

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from uuid import uuid4
import httpx
from sqlalchemy import func, select
from core.stage13.delivery import (
    DeliveryService,
    authorization_digest,
    delivery_identity,
)
from core.stage13.delivery_models import DeliveryRow, ControlledSinkRow
from core.stage13.triage_subject import digest
from tests.test_stage13_wp04_triage import assembly, payload, output


class TestAuthority:
    __test__ = False

    def __init__(self, authorization):
        self.authorization = authorization

    async def verify(self, binding):
        return deepcopy(self.authorization)


async def setup(database):
    p = payload()
    service, calls, client = await assembly(database, [json.dumps(output(p))])
    run_id = str(uuid4())
    response = await service.evaluation_execute(
        run_id=run_id,
        agent_id="ci_triage_baseline",
        query=json.dumps(p),
        timeout_seconds=120,
        expected_subject_manifest=service.manifests["ci_triage_baseline"],
    )
    receipt = response["actual_subject_receipt"]
    binding = {
        "gate": {
            "gate_id": str(uuid4()),
            "gate_decision": "PASS",
            "gate_receipt_digest": "a" * 64,
            "dataset_version": "CONTROLLED_PASS_FIXTURE",
            "baseline_subject_digest": receipt["actual_subject_manifest"][
                "subject_manifest_digest"
            ],
            "candidate_subject_digest": receipt["actual_subject_manifest"][
                "subject_manifest_digest"
            ],
            "comparison_digest": "b" * 64,
            "snapshot_digest": "c" * 64,
            "policy_version": "TEST_SCOPE",
            "policy_digest": "d" * 64,
        },
        "outcome": {
            "case_id": str(uuid4()),
            "case_version": "TEST_SCOPE",
            "evaluation_run_id": str(uuid4()),
            "anchor_run_id": run_id,
            "selected_final_run_id": run_id,
            "subject_manifest_digest": receipt["actual_subject_manifest"][
                "subject_manifest_digest"
            ],
            "final_answer_digest": receipt["final_answer_digest"],
            "actual_subject_receipt_digest": receipt["receipt_digest"],
        },
        "destination": {
            "kind": "CONTROLLED_DELIVERY_SINK",
            "scope": "TEST_SCOPE",
            "id": "unit-sink",
        },
    }
    a = {
        "authorization_version": "stage13.delivery-authorization.v1",
        "project_id": "test-project",
        "gate": binding["gate"],
        "outcome": binding["outcome"],
        "destination": binding["destination"],
        "authorization_status": "AUTHORIZED",
        "reason": "GATE_PASS",
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "evaluation_result_refs": ["test-result"],
    }
    a["authorization_digest"] = authorization_digest(a)
    request = {
        "protocol_version": "stage13.delivery.v1",
        "delivery_id": delivery_identity(binding),
        "binding": binding,
        "gate_authorization": a,
    }
    await service.services.close(timeout=20)
    await client.aclose()
    return TestAuthority(a), request


async def writes(database):
    async with database.session() as session:
        return await session.scalar(select(func.count()).select_from(ControlledSinkRow))


def test_gate_denial_and_binding_fail_closed(clean_database):
    async def run():
        authority, request = await setup(clean_database)
        records = []
        for case in (
            "BLOCKED",
            "FAIL",
            "missing",
            "unavailable",
            "malformed",
            "gate_receipt_digest",
            "candidate_subject_digest",
            "comparison_digest",
            "snapshot_digest",
            "subject_manifest_digest",
            "final_answer_digest",
            "actual_subject_receipt_digest",
            "unknown_gate",
        ):
            q = deepcopy(request)
            q["binding"]["gate"]["gate_id"] = str(uuid4())
            a = deepcopy(q["gate_authorization"])
            a["gate"] = deepcopy(q["binding"]["gate"])
            a["authorization_digest"] = authorization_digest(a)
            auth = TestAuthority(a)
            q["gate_authorization"] = deepcopy(a)
            if case in ("BLOCKED", "FAIL"):
                q["binding"]["gate"]["gate_decision"] = case
                a["gate"]["gate_decision"] = case
                a["authorization_status"] = "DENIED"
                a["reason"] = "GATE_" + case
                a["authorization_digest"] = authorization_digest(a)
                auth.authorization = a
                q["gate_authorization"] = deepcopy(a)
            elif case == "missing":
                q["gate_authorization"] = None
            elif case in ("unavailable", "unknown_gate"):

                async def failure(binding):
                    raise httpx.ConnectError("test authority unavailable")

                auth.verify = failure
            elif case == "malformed":
                auth.authorization = {}
            elif case in q["binding"]["gate"]:
                q["binding"]["gate"][case] = "e" * 64
            else:
                q["binding"]["outcome"][case] = "e" * 64
            q["delivery_id"] = delivery_identity(q["binding"])
            result = await DeliveryService(
                clean_database, auth, "wp04-test", "test-project", "unit-sink"
            ).deliver(q)
            assert result["delivery_status"] == "DENIED", case
            assert await writes(clean_database) == 0
            records.append(case)
        assert len(records) == 13

    asyncio.run(run())


def test_authorized_sink_replay_and_hidden_gt_absent(clean_database):
    async def run():
        authority, q = await setup(clean_database)
        service = DeliveryService(
            clean_database, authority, "wp04-test", "test-project", "unit-sink"
        )
        first = await service.deliver(q)
        assert first["delivery_status"] == "DELIVERED"
        second = await service.deliver(q)
        assert first == second and await writes(clean_database) == 1
        copy = dict(first)
        rd = copy.pop("delivery_receipt_digest")
        assert digest(copy) == rd
        async with clean_database.session() as session:
            sink = await session.get(ControlledSinkRow, q["delivery_id"])
            raw = json.dumps(sink.payload)
            assert not any(
                k in raw
                for k in (
                    "hidden-root-",
                    "ExpectedFailureCategory",
                    "ExpectedTicketDecision",
                    "GroundTruth",
                    "critical_evaluator_label",
                )
            )
            assert digest(sink.payload) == sink.payload_digest
        new = deepcopy(q)
        new["binding"]["gate"]["gate_id"] = str(uuid4())
        assert delivery_identity(new["binding"]) != q["delivery_id"]

        # 已持久 sink 也不能通过不存在的 gate 绕过核心 authorization。
        async def unavailable(binding):
            raise httpx.ConnectError("gate unavailable")

        authority.verify = unavailable
        assert (await service.deliver(q))["delivery_status"] == "DENIED"
        assert await writes(clean_database) == 1

    asyncio.run(run())


def test_response_loss_unknown_then_lookup_before_write(clean_database):
    async def run():
        authority, q = await setup(clean_database)
        points = []

        async def lose(point):
            points.append(point)
            if point == "after_sink_commit":
                raise OSError("response lost")

        s = DeliveryService(
            clean_database,
            authority,
            "wp04-test",
            "test-project",
            "unit-sink",
            fault=lose,
        )
        unknown = await s.deliver(q)
        assert (
            unknown["delivery_status"] == "OUTCOME_UNKNOWN"
            and unknown["side_effect_state"] == "UNKNOWN"
        )
        assert await writes(clean_database) == 1
        recovered = await DeliveryService(
            clean_database, authority, "wp04-test", "test-project", "unit-sink"
        ).deliver(q)
        assert (
            recovered["delivery_status"] == "DELIVERED"
            and recovered["reconciliation_status"] == "FOUND_EXISTING"
        )
        assert await writes(clean_database) == 1
        assert "after_local_receipt" not in points

    asyncio.run(run())


def test_two_and_four_publication_workers(clean_database):
    async def run():
        authority, q = await setup(clean_database)
        for count in (2, 4):
            current = deepcopy(q)
            current["binding"]["destination"]["id"] = f"race-{count}"
            a = deepcopy(authority.authorization)
            a["destination"] = current["binding"]["destination"]
            a["authorization_digest"] = authorization_digest(a)
            current["gate_authorization"] = a
            current["delivery_id"] = delivery_identity(current["binding"])

            async def work():
                return await DeliveryService(
                    clean_database,
                    TestAuthority(a),
                    "wp04-test",
                    "test-project",
                    f"race-{count}",
                ).deliver(current)

            results = await asyncio.gather(*(work() for _ in range(count)))
            assert any(r["delivery_status"] == "DELIVERED" for r in results)
            assert (await work())["delivery_status"] == "DELIVERED"
            async with clean_database.session() as session:
                row = await session.get(DeliveryRow, current["delivery_id"])
                assert row.epoch == 1
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(ControlledSinkRow)
                        .where(ControlledSinkRow.delivery_id == current["delivery_id"])
                    )
                    == 1
                )

    asyncio.run(run())
