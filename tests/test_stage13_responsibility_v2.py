"""WP12C-T公开TEST-ONLY合同fixture；不接触真实cohort/GT，也不调用Provider。"""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from core.stage13 import input_contract_v2 as contract
from core.stage13.execution_policy import bind_execution_policy, semantic_input
from core.stage13.model_comparability import compare_receipts, project_receipt
from core.stage13.triage_models import TriageRunRow
from core.stage13.triage_subject import definitions, digest, check_manifest
from tests.test_stage13_model_comparability import policy_input, seal
from tests.test_stage13_wp04_triage import assembly

FIXTURE = Path(__file__).parent / "fixtures/stage13_contract_v2/input-test-only.json"


def payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def request(p, manifest):
    start = datetime.now(timezone.utc)
    policy = dict(
        version=contract.EXECUTION_V2,
        evaluation_run_id=str(uuid4()),
        evaluation_attempt_id=str(uuid4()),
        timeout_seconds=180,
        execution_started_at=start.isoformat(),
        execution_deadline_at=(start + timedelta(seconds=180)).isoformat(),
        semantic_input_digest=digest(semantic_input(p)),
        source_input_digest=digest(p),
        source_semantic_input_digest=digest(semantic_input(p)),
        projected_input_digest=digest(contract.project_input_v2(semantic_input(p))),
        comparison_domain_digest=digest(contract.DOMAIN),
        **contract.contract_identities(),
    )
    actual = deepcopy(p)
    actual["evidence_policy"]["deadline_at"] = policy["execution_deadline_at"]
    return dict(
        run_id=policy["evaluation_attempt_id"],
        agent_id=manifest["agent_id"],
        query=contract.canonical(actual).decode(),
        timeout_seconds=180,
        expected_subject_manifest=manifest,
        execution_policy=policy,
    )


def subject_fixture():
    value = policy_input()
    manifest = value["manifest"]
    # 只修改独立合同fixture的主体，绝不修改生产Candidate定义。
    manifest.update(
        subject_id="test_only_contract_subject",
        agent_id="test_only_contract_subject",
        subject_version="test-only-v2",
        agent_definition_version="test-only-v2",
        **contract.contract_identities(),
    )
    manifest["output_schema_digest"] = contract.DOMAIN["output_schema_digest"]
    manifest.pop("subject_manifest_digest")
    manifest["subject_manifest_digest"] = digest(manifest)
    body = request(payload(), manifest)
    receipt = value["receipt"]
    receipt.update(
        actual_subject_manifest=manifest,
        actual_input_digest=digest(json.loads(body["query"])),
        execution_policy=body["execution_policy"],
        execution_request_digest=digest(body),
        **contract.receipt_contract_fields(manifest, body["execution_policy"]),
    )
    seal(receipt)
    return value, body


def test_public_projection_and_cross_language_identity():
    p = payload()
    original = deepcopy(p)
    assert contract.validate_input_v2(p) == p
    assert (
        contract.digest(p)
        == "13b2ad0f9e43f384eaa29e3ba1cd97d213670801dfcb34b7139e06ee79c5d2e4"
    )
    assert json.loads(contract.materialize_input_v2(p)) == p == original
    assert list(inspect.signature(contract.component_order).parameters) == [
        "component_ids"
    ]
    assert contract.component_order(["component-b", "component-a"]) == [
        "component-a",
        "component-b",
    ]
    p["failure_summary"]["component_scope"].reverse()
    p["business_context"]["entities"].reverse()
    p["business_context"]["relations"].reverse()
    assert contract.project_input_v2(p) == original
    with pytest.raises(ValueError):
        contract.validate_input_v2(p)
    original["business_context"]["relations"] = []
    assert contract.project_input_v2(original)["business_context"]["relations"] == []


@pytest.mark.parametrize(
    "mode",
    [
        "GT",
        "GT-string",
        "hidden-id",
        "pointer",
        "digest",
        "scope",
        "snapshot",
        "endpoint",
        "budget",
        "availability",
        "version",
        "role",
        "missing-semantic",
        "null-entities",
    ],
)
def test_input_contract_rejection(mode):
    p = payload()
    if mode == "GT":
        p["failure_summary"]["owner_component_id"] = "component-a"
    if mode == "GT-string":
        p["failure_summary"]["note"] = '{"ground_truth_owner":"component-a"}'
    if mode == "hidden-id":
        p["failure_summary"]["note"] = "hidden-root-test"
    if mode == "pointer":
        p["business_context"]["relations"][0]["source_pointer"] = "/relations/999"
    if mode == "digest":
        p["visible_evidence"][0]["digest"] = "0" * 64
    if mode == "scope":
        p["scope"]["suite_id"] = "other"
    if mode == "snapshot":
        p["business_context"]["snapshot_ref"]["revision"] = "other"
    if mode == "endpoint":
        p["business_context"]["relations"][0]["object"]["id"] = "unknown"
    if mode == "budget":
        p["evidence_policy"]["max_tool_reads"] = 9
    if mode == "availability":
        p["visible_evidence"][0]["availability"] = "MISSING"
    if mode == "version":
        p["schema_version"] = "stage13.triage-input.v1"
    if mode == "role":
        p["responsibility_semantics"]["role"] = "PRODUCER"
    if mode == "missing-semantic":
        del p["responsibility_semantics"]
    if mode == "null-entities":
        p["business_context"]["entities"] = None
    with pytest.raises(ValueError):
        contract.validate_input_v2(p)


def test_subject_execution_receipt_and_comparability_v3():
    left, body = subject_fixture()
    right, _ = subject_fixture()
    frozen = bind_execution_policy(body)
    assert frozen["execution_request_digest"] == digest(body)
    check_manifest(left["manifest"], left["manifest"])
    assert contract.validate_receipt_v2(left["receipt"])

    def side(value):
        receipt = value["receipt"]
        return dict(
            manifest=value["manifest"],
            receipt=receipt,
            projection=project_receipt(value["manifest"], receipt),
            input_digest=receipt["actual_input_digest"],
            semantic_input_digest=receipt["semantic_input_digest"],
        )

    result = compare_receipts(side(left), side(right))
    assert result["policy_version"] == contract.COMPARABILITY_V3
    assert result["comparable"], result
    old = policy_input()
    old_side = dict(
        manifest=old["manifest"],
        projection=project_receipt(old["manifest"], old["receipt"]),
        input_digest=old["receipt"]["actual_input_digest"],
    )
    result = compare_receipts(side(left), old_side)
    assert not result["comparable"] and "DOMAIN_MISMATCH" in result["reasons"]
    for key in (
        "input_contract_digest",
        "source_input_digest",
        "projected_input_digest",
        "receipt_version",
        "comparison_domain_digest",
    ):
        bad = deepcopy(left["receipt"])
        bad[key] = "invalid"
        seal(bad)
        with pytest.raises(ValueError):
            contract.validate_receipt_v2(bad)
    bad = deepcopy(body)
    bad["execution_policy"]["projection_digest"] = "0" * 64
    with pytest.raises(ValueError):
        bind_execution_policy(bad)
    bad = deepcopy(body)
    bad["execution_policy"]["version"] = "stage13.attempt-execution.v1"
    with pytest.raises(ValueError):
        bind_execution_policy(bad)


def test_equivalent_relation_conflict_scope_and_source_order():
    p = payload()
    source = p["visible_evidence"][0]
    world = source["content"]
    original = next(r for r in world["relations"] if r["relation_id"] == "owns-helper")
    original["ownership_mode"] = "EQUIVALENT"
    alternative = deepcopy(original)
    alternative["relation_id"] = "owns-helper-b"
    alternative["subject"]["id"] = "component-b"
    world["relations"].append(alternative)

    def bind_source():
        world["relations"].sort(key=contract._relation_order)
        source["digest"] = digest(world)
        p["business_context"]["snapshot_ref"]["digest"] = source["digest"]
        ref = {k: source[k] for k in ("evidence_id", "digest", "type")}
        p["business_context"]["relations"] = [
            dict(**r, source_evidence_ref=ref, source_pointer=f"/relations/{i}")
            for i, r in enumerate(world["relations"])
        ]
        p["incident_ref"]["manifest_digest"] = digest(p["visible_evidence"])

    bind_source()
    assert contract.validate_input_v2(p)
    alternative["ownership_mode"] = "EXCLUSIVE"
    bind_source()
    with pytest.raises(ValueError, match="CONFLICTING_EXCLUSIVE_OWNERS"):
        contract.validate_input_v2(p)
    alternative["ownership_mode"] = "EQUIVALENT"
    world["component_scope"] = ["component-b", "component-a"]
    bind_source()
    with pytest.raises(ValueError, match="SOURCE_COMPONENT_ORDER_INVALID"):
        contract.project_input_v2(p)
    del world["component_scope"]
    bind_source()
    source["owner_scope_id"] = "other-project"
    with pytest.raises(ValueError, match="EVIDENCE_SCOPE_DENIED"):
        contract.validate_input_v2(p, scope_id="authorized-project")


def test_v2_registered_subject_durable_reservation_without_model(
    clean_database, monkeypatch
):
    from core.stage13 import triage_runtime

    base = definitions()[0]
    test_subject = replace(
        base,
        agent_id="test_only_contract_subject",
        agent_version="test-only-v2",
        business_options={
            "stage13_single_call": True,
            "stage13_input_contract_version": contract.INPUT_V2,
        },
    )
    monkeypatch.setattr(triage_runtime, "definitions", lambda: (test_subject,))

    async def run():
        service, calls, client = await assembly(clean_database, [])
        try:
            manifest = service.manifests[test_subject.agent_id]
            assert manifest["input_contract_version"] == contract.INPUT_V2
            assert (
                manifest["output_schema_digest"]
                == contract.DOMAIN["output_schema_digest"]
            )
            body = request(payload(), manifest)
            binding = bind_execution_policy(body)
            await service.reserve_run(
                body["run_id"],
                body["query"],
                manifest,
                attempt_id=body["run_id"],
                lane="OFFLINE",
                execution_policy=binding,
            )
            async with clean_database.session() as session:
                row = await session.get(TriageRunRow, body["run_id"])
                assert row.execution_policy == binding
                assert row.execution_policy["execution_request_digest"] == digest(body)
                assert row.model_call is None
                with pytest.raises(Exception, match="STAGE13_TRIAGE_IMMUTABLE"):
                    await session.execute(
                        text(
                            "UPDATE stage13_triage_runs SET execution_policy='{}'::jsonb WHERE run_id=:id"
                        ),
                        {"id": body["run_id"]},
                    )
                await session.rollback()
            await service.reserve_run(
                body["run_id"],
                body["query"],
                manifest,
                attempt_id=body["run_id"],
                lane="OFFLINE",
                execution_policy=binding,
            )
            # 明确的非模型terminal fixture只测试真实receipt builder；不能用于质量评分。
            raw = json.dumps(
                dict(
                    FailureCategory="UNKNOWN",
                    RootCauseCandidates=[],
                    EvidenceRefs=[],
                    RecommendedAction=dict(ActionCode="REQUEST_MORE_EVIDENCE"),
                    Confidence=0,
                    NeedMoreEvidence=True,
                    TicketDecision="REQUEST_MORE_EVIDENCE",
                )
            )
            root = SimpleNamespace(
                status="SUCCEEDED",
                stop_reason="COMPLETED",
                final_result_binding={"content": raw},
                resume_input=dict(
                    resolved_agent_id=manifest["agent_id"],
                    agent_version=manifest["agent_definition_version"],
                    toolset_identity=manifest["tool_profile_digest"],
                    resolved_model_profile_id=manifest["model_profile_id"],
                ),
            )

            async def terminal_fixture(_):
                return SimpleNamespace(root=root, steps=[])

            service.repository = SimpleNamespace(load=terminal_fixture)
            from core.stage13.triage_http import router

            app = FastAPI()
            app.include_router(router)
            app.state.stage13_service = service
            monkeypatch.setenv(
                "LOCAL_AGENT_STAGE13_SERVICE_TOKEN", "contract-test-only"
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://contract.test"
            ) as http:
                response = await http.post(
                    "/api/runtime/evaluation-execute/stage13/v1",
                    json=body,
                    headers={"Authorization": "Bearer contract-test-only"},
                )
                assert response.status_code == 200, response.text
                receipt = response.json()["actual_subject_receipt"]
                assert contract.validate_receipt_v2(receipt)
                assert receipt["actual_input_digest"] == digest(
                    json.loads(body["query"])
                )
                assert (
                    receipt["effective_payload_digest"]
                    == contract.hashlib.sha256(body["query"].encode()).hexdigest()
                )
                assert receipt["model_call_receipts"] == []
                assert not response.json()["model_comparability_decision"]["comparable"]
                replay = await http.post(
                    "/api/runtime/evaluation-execute/stage13/v1",
                    json=body,
                    headers={"Authorization": "Bearer contract-test-only"},
                )
                assert replay.json() == response.json()
            assert calls == []
        finally:
            await service.services.close(timeout=5)
            await client.aclose()

    asyncio.run(run())


def test_http_invalid_v2_rejected_before_dispatch(monkeypatch):
    from core.stage13.triage_http import router
    from core.stage13.triage_execution import TriageExecutionService

    class Service(TriageExecutionService):
        async def reserve_run(self, *args, **kwargs):
            raise AssertionError("invalid input must not reserve or dispatch")

    monkeypatch.setenv("LOCAL_AGENT_STAGE13_SERVICE_TOKEN", "contract-test-only")
    app = FastAPI()
    app.include_router(router)
    app.state.stage13_service = Service(None, "contract-test")
    _, body = subject_fixture()
    p = json.loads(body["query"])
    p["responsibility_semantics"]["role"] = "PRODUCER"
    body["query"] = contract.canonical(p).decode()

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://contract.test"
        ) as client:
            response = await client.post(
                "/api/runtime/evaluation-execute/stage13/v1",
                json=body,
                headers={"Authorization": "Bearer contract-test-only"},
            )
            assert response.status_code == 409, response.text

    asyncio.run(run())
