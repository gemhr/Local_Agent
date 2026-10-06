"""WP01 真实 PostgreSQL、跨进程、分层 API 与正式 governed Tool 验证。"""

from collections import Counter
from dataclasses import replace
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

import httpx
import pytest
from sqlalchemy import select, text

from core.persistence.database import Database, DatabaseConfig
from core.persistence.errors import PersistenceError
from core.runtime.agent_registry import AgentRegistry, DEFAULT_AGENT_REGISTRY
from core.runtime.durable_approval import DurableApprovalService
from core.runtime.run_control import DurableRunControlService
from core.runtime.tool_execution import ToolExecutionService
from core.runtime.tool_adapters import ToolAdapterInvocationError
from core.runtime.tool_governance import (
    ToolGovernanceService,
    ToolPolicy,
    ToolPolicyCatalog,
)
from core.runtime.tool_idempotency import DurableToolInvocationService
from core.runtime.tool_registry import ToolRegistry
from core.stage8.execution import GovernedToolInvoker, Stage8ValidationError
from core.stage13.adapters import build_controlled_ci_tool_registrations
from core.stage13.contracts import (
    ArtifactRequest,
    DetailRequest,
    EvidencePacket,
    LookupRequest,
    ProviderError,
    RemoteRequest,
    SubmitRequest,
    WorkloadConfig,
    business_key,
    canonical_bytes,
    sha256,
)
from core.stage13.http import create_provider_app
from core.stage13.provider import (
    ControlledCIProvider,
    ControlledFaults,
    ControlledProviderOperator,
)
from core.stage13.store import ProviderKeyRow, ProviderStore
from core.stage13.workload import Stage13Workload
from tests.test_stage13_wp01_workload import assert_agent_visible


def workload_config(namespace=None):
    return WorkloadConfig(
        provider_namespace_id=namespace or f"test-{uuid.uuid4().hex}",
        environment_count=100,
    )


async def create_provider(database, *, config=None, faults=None):
    provider = ControlledCIProvider(
        ProviderStore(database, Stage13Workload(config or workload_config())),
        faults=faults,
    )
    await provider.initialize()
    return provider


def lookup_request(request):
    return LookupRequest(
        remote_execution_business_key=request.remote_execution_business_key,
        request_digest=request.request_digest,
    )


def remote_request(request, receipt):
    return RemoteRequest(
        **lookup_request(request).model_dump(),
        remote_execution_id=receipt.remote_execution_id,
    )


def change_cycle(request):
    intent = request.intent()
    intent["cycle_key"] = business_key("cycle", "test-guardian", "2026-10-07")
    intent["version_execution_key"] = business_key(
        "version", intent["cycle_key"], request.ordinal, request.product_version
    )
    intent["remote_execution_business_key"] = business_key(
        "remote",
        request.provider_namespace_id,
        request.owner_scope_id,
        request.automation_project_id,
        request.suite_id,
        request.environment_id,
        intent["cycle_key"],
        request.ordinal,
        request.product_version,
    )
    return SubmitRequest(**intent, request_digest=sha256(canonical_bytes(intent)))


@pytest.mark.asyncio
async def test_durable_identity_conflict_restart_cycle_version_and_namespace(
    clean_database,
):
    provider = await create_provider(clean_database)
    request = provider.workload.request(0)
    first = await provider.submit_execution(request)
    assert await provider.submit_execution(request) == first
    with pytest.raises(ProviderError, match="^CONFLICT$"):
        await provider.submit_execution(
            provider.workload.request(0, parameters={"mode": "alternative"})
        )
    restarted = await create_provider(clean_database, config=provider.workload.config)
    assert await restarted.submit_execution(request) == first
    assert (
        await restarted.lookup_by_remote_execution_id(remote_request(request, first))
    ).receipt == first
    different_cycle = await provider.submit_execution(change_cycle(request))
    different_version = await provider.submit_execution(provider.workload.request(1))
    assert (
        len(
            {
                first.remote_execution_id,
                different_cycle.remote_execution_id,
                different_version.remote_execution_id,
            }
        )
        == 3
    )
    assert (await provider.store.counts())["unique_executions"] == 3
    with pytest.raises(ProviderError, match="NAMESPACE_CONFLICT"):
        await create_provider(
            clean_database,
            config=provider.workload.config.model_copy(update={"seed": 1302}),
        )
    other = await create_provider(
        clean_database,
        config=provider.workload.config.model_copy(
            update={"provider_namespace_id": "new-isolated-seed", "seed": 1302}
        ),
    )
    assert (await other.store.counts())["unique_executions"] == 0
    assert (
        await other.lookup_by_business_key(lookup_request(request))
    ).result == "NOT_CREATED"


@pytest.mark.asyncio
async def test_response_loss_after_commit_lookup_and_repeat_submit_same_receipt(
    clean_database,
):
    config = workload_config()
    request = Stage13Workload(config).request(0)
    provider = await create_provider(
        clean_database,
        config=config,
        faults=ControlledFaults(
            response_loss_keys=frozenset({request.remote_execution_business_key})
        ),
    )
    with pytest.raises(ProviderError, match="RESPONSE_LOST_AFTER_COMMIT"):
        await provider.submit_execution(request)
    assert (await provider.store.counts())["unique_executions"] == 1
    restarted = await create_provider(
        clean_database, config=config, faults=provider.faults
    )
    result = await restarted.lookup_by_business_key(lookup_request(request))
    assert result.result == "FOUND"
    assert await restarted.submit_execution(request) == result.receipt
    assert await provider.submit_execution(request) == result.receipt
    assert (await restarted.store.counts())["unique_executions"] == 1
    assert provider.counters["response_loss_injected"] == 1


@pytest.mark.asyncio
async def test_seal_absent_existing_and_concurrent_submit_seal_linearization(
    clean_database,
):
    provider = await create_provider(clean_database)
    operator = ControlledProviderOperator(provider.store)
    request = provider.workload.request(0)
    assert (
        await provider.lookup_by_business_key(lookup_request(request))
    ).result == "NOT_CREATED"
    sealed = await operator.seal_absent_key(lookup_request(request))
    assert sealed.result == "NOT_CREATED_FINAL"
    restarted = await create_provider(clean_database, config=provider.workload.config)
    assert await restarted.lookup_by_business_key(lookup_request(request)) == sealed
    with pytest.raises(ProviderError, match="KEY_CLOSED"):
        await restarted.submit_execution(request)
    with pytest.raises(ProviderError, match="KEY_CLOSED"):
        await restarted.submit_execution(
            provider.workload.request(0, parameters={"mode": "late"})
        )
    existing_request = provider.workload.request(1)
    existing = await provider.submit_execution(existing_request)
    assert (
        await operator.seal_absent_key(lookup_request(existing_request))
    ).receipt == existing
    assert (
        await provider.get_ci_summary(remote_request(existing_request, existing))
    ).content.find('"remote_state":"QUEUED"') >= 0
    race_request = provider.workload.request(2)
    results = await asyncio.gather(
        provider.submit_execution(race_request),
        operator.seal_absent_key(lookup_request(race_request)),
        return_exceptions=True,
    )
    final = await provider.lookup_by_business_key(lookup_request(race_request))
    if final.result == "FOUND":
        assert results[0] == results[1].receipt == final.receipt
    else:
        assert final.result == "NOT_CREATED_FINAL"
        assert isinstance(results[0], ProviderError) and results[0].code == "KEY_CLOSED"
    assert (await provider.store.counts())["key_rows"] == 3


@pytest.mark.asyncio
async def test_remote_states_results_faults_layering_pagination_and_artifact_revisions(
    clean_database,
):
    config = workload_config()
    workload = Stage13Workload(config)
    plan = next(plan for plan in workload.plans if len(plan.failed_cases) == 15)
    request = workload.request(plan.index)
    faults = ControlledFaults(
        result_delay_seconds=120,
        lookup_failures=Counter({request.remote_execution_business_key: 1}),
        status_failures=Counter({request.remote_execution_business_key: 1}),
    )
    provider = await create_provider(clean_database, config=config, faults=faults)
    operator = ControlledProviderOperator(provider.store)
    receipt = await provider.submit_execution(request)
    remote = remote_request(request, receipt)
    with pytest.raises(ProviderError, match="TEMPORARILY_UNAVAILABLE"):
        await provider.lookup_by_business_key(lookup_request(request))
    with pytest.raises(ProviderError, match="TEMPORARILY_UNAVAILABLE"):
        await provider.get_ci_summary(remote)
    queued = await provider.get_ci_summary(remote)
    assert json.loads(queued.content)["remote_state"] == "QUEUED"
    assert provider.counters["detail_reads"] == provider.counters["artifact_reads"] == 0
    with pytest.raises(ProviderError, match="RESULT_NOT_READY"):
        await provider.fetch_failure_detail(DetailRequest(**remote.model_dump()))
    await operator.advance_clock(60)
    assert (
        json.loads((await provider.get_ci_summary(remote)).content)["remote_state"]
        == "RUNNING"
    )
    await operator.advance_clock(60 + plan.duration_seconds)
    delayed = json.loads((await provider.get_ci_summary(remote)).content)
    assert (
        delayed["remote_state"] == "COMPLETED" and delayed["result_available"] is False
    )
    assert delayed["case_counts"] is None
    await operator.advance_clock(60 + plan.duration_seconds + 120)
    summary = await provider.get_ci_summary(remote)
    assert summary == await provider.get_ci_summary(remote)
    body = json.loads(summary.content)
    assert body["case_counts"]["FAILED"] == 15
    assert sum(body["case_counts"].values()) == plan.case_count
    assert body["status_revision"] == 3
    assert len(summary.model_dump_json().encode()) <= 64 * 1024
    with pytest.raises(ProviderError, match="INVALID_REMOTE_TRANSITION"):
        await operator.advance_execution(
            request.remote_execution_business_key, "RUNNING"
        )
    pages = [
        await provider.fetch_failure_detail(
            DetailRequest(**remote.model_dump(), offset=offset, page_size=7)
        )
        for offset in (0, 7, 14)
    ]
    assert [len(json.loads(page.content)["cases"]) for page in pages] == [7, 7, 1]
    assert [json.loads(page.content)["next_offset"] for page in pages] == [7, 14, None]
    assert all(len(page.model_dump_json().encode()) <= 256 * 1024 for page in pages)
    assert pages[0] == await provider.fetch_failure_detail(
        DetailRequest(**remote.model_dump(), page_size=7)
    )
    for packet in [summary, *pages]:
        assert_agent_visible(packet.model_dump(mode="json"))
        assert_agent_visible(json.loads(packet.content))
        assert sha256(packet.content.encode()) == packet.digest
    # 当前 execution 未必含四种模式，分别选对应失败来核对完整 evidence 场景。
    clock = 60 + plan.duration_seconds + 120
    used_plans = {plan.index}
    for mode in ("AVAILABLE", "ABSENT", "UNAVAILABLE", "DELAYED"):
        chosen_plan, case = next(
            (item, case)
            for item in workload.plans
            for case, _ in item.failed_cases
            if item.index not in used_plans
            and workload.artifact_mode(item.index, case) == mode
        )
        used_plans.add(chosen_plan.index)
        chosen_request = workload.request(chosen_plan.index)
        chosen_receipt = await provider.submit_execution(chosen_request)
        await operator.advance_execution(
            chosen_request.remote_execution_business_key, "RUNNING"
        )
        await operator.advance_execution(
            chosen_request.remote_execution_business_key, "COMPLETED"
        )
        clock += 120
        await operator.advance_clock(clock)
        chosen_remote = remote_request(chosen_request, chosen_receipt)
        page = await provider.fetch_failure_detail(
            DetailRequest(**chosen_remote.model_dump())
        )
        detail = next(
            item
            for item in json.loads(page.content)["cases"]
            if item["provider_case_id"] == workload.case_id(case)
        )
        if mode == "ABSENT":
            assert detail["artifact_refs"] == []
            continue
        artifact_request = ArtifactRequest(
            **chosen_remote.model_dump(), artifact_ref=detail["artifact_refs"][0]
        )
        artifact = await provider.fetch_artifact(artifact_request)
        assert artifact.availability == mode
        assert_agent_visible(json.loads(artifact.content))
        assert sha256(artifact.content.encode()) == artifact.digest
        assert len(artifact.model_dump_json().encode()) <= 256 * 1024
        if mode == "DELAYED":
            clock += 300
            await operator.advance_clock(clock)
            ready = await provider.fetch_artifact(artifact_request)
            assert ready.availability == "AVAILABLE"
            assert (
                ready.evidence_id != artifact.evidence_id
                and ready.digest != artifact.digest
            )
            assert ready == await provider.fetch_artifact(artifact_request)
    for index, terminal in ((0, "INFRA_FAILED"), (1, "CANCELLED")):
        new_cycle_request = change_cycle(workload.request(index))
        other_receipt = await provider.submit_execution(new_cycle_request)
        await operator.advance_execution(
            new_cycle_request.remote_execution_business_key, terminal
        )
        body = json.loads(
            (
                await provider.get_ci_summary(
                    remote_request(new_cycle_request, other_receipt)
                )
            ).content
        )
        assert body["remote_state"] == terminal and body["case_counts"] is None


@pytest.mark.asyncio
async def test_database_binding_receipt_and_terminal_are_immutable(clean_database):
    provider = await create_provider(clean_database)
    request = provider.workload.request(0)
    receipt = await provider.submit_execution(request)
    with pytest.raises(PersistenceError):
        async with clean_database.transaction() as session:
            await session.execute(
                text(
                    "UPDATE stage13_provider_keys SET receipt = receipt || '{\"status_revision\":9}'::jsonb WHERE remote_execution_id=:id"
                ),
                {"id": str(receipt.remote_execution_id)},
            )
    assert await provider.submit_execution(request) == receipt
    operator = ControlledProviderOperator(provider.store)
    await operator.advance_execution(request.remote_execution_business_key, "CANCELLED")
    with pytest.raises(PersistenceError):
        async with clean_database.transaction() as session:
            await session.execute(
                text(
                    "UPDATE stage13_provider_keys SET state='RUNNING',status_revision=status_revision+1 WHERE remote_execution_id=:id"
                ),
                {"id": str(receipt.remote_execution_id)},
            )


def _probe(mode, config_path, index=0, *, env):
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tests._stage13_process",
            mode,
            str(config_path),
            str(index),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
    )


@pytest.mark.REAL_PROCESS_CRASH_E2E
def test_real_process_crash_after_commit_restart_and_multiprocess_same_submit(
    clean_database, pg_schema, tmp_path
):
    config = workload_config()
    config_path = tmp_path / "workload-config.json"
    config_path.write_text(config.model_dump_json(), encoding="utf-8")
    env = {
        **os.environ,
        "LOCAL_AGENT_TEST_DATABASE_URL": pg_schema,
        "PYTHONIOENCODING": "utf-8",
    }
    processes = []
    try:
        crashed = _probe("submit-loss", config_path, env=env)
        processes.append(crashed)
        stdout, stderr = crashed.communicate(timeout=30)
        assert crashed.returncode == 23, stderr
        assert stdout == ""  # 没有成功 receipt 到达 parent。
        first = _probe("lookup", config_path, env=env)
        processes.append(first)
        stdout, stderr = first.communicate(timeout=30)
        assert first.returncode == 0, stderr
        recovered = json.loads(stdout)
        assert recovered["result"] == "FOUND"
        for _ in range(4):
            child = _probe("concurrent-submit", config_path, env=env)
            processes.append(child)
        children = processes[-4:]
        for child in children:
            assert child.stdout.readline().strip() == "READY"
        for child in children:
            child.stdin.write("GO\n")
            child.stdin.flush()
        for child in children:
            stdout, stderr = child.communicate(timeout=30)
            assert child.returncode == 0, stderr
            assert json.loads(stdout) == recovered["receipt"]

        async def verify():
            provider = await create_provider(clean_database, config=config)
            request = provider.workload.request(0)
            receipts = await asyncio.gather(
                *(provider.submit_execution(request) for _ in range(12))
            )
            assert all(
                receipt.model_dump(mode="json") == recovered["receipt"]
                for receipt in receipts
            )
            assert (await provider.store.counts())["unique_executions"] == 1

        asyncio.run(verify())
    finally:
        for child in processes:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)


@pytest.mark.asyncio
async def test_http_agent_boundary_and_governed_tool_replay(clean_database):
    provider = await create_provider(clean_database)
    app = create_provider_app(provider, agent_token="synthetic-agent-test-only")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://controlled-ci",
        headers={"Authorization": "Bearer synthetic-agent-test-only"},
    ) as client:
        for hidden_route in (
            "/v1/hidden-gt",
            "/v1/seal",
            "/v1/advance-clock",
            "/v1/metrics",
            "/v1/manifest",
        ):
            assert (await client.post(hidden_route, json={})).status_code == 404
        request = provider.workload.request(0)
        assert (
            await client.post(
                "/v1/submit",
                json=request.model_dump(mode="json"),
                headers={"Authorization": "Bearer invalid"},
            )
        ).status_code == 401
        registry = ToolRegistry()
        registrations = build_controlled_ci_tool_registrations(
            client, provider.workload.config
        )
        for registration in registrations:
            registry.register(registration)
        registry.freeze()
        allowed_tools = frozenset(
            registration.descriptor.name for registration in registrations
        )
        agent_registry = AgentRegistry(
            (
                replace(
                    DEFAULT_AGENT_REGISTRY.resolve("core_router"),
                    actual_allowed_tools=allowed_tools,
                ),
            )
        )
        catalog = ToolPolicyCatalog(
            tool_registry=registry, agent_registry=agent_registry
        )
        for registration in registrations:
            catalog.register(
                ToolPolicy(
                    tool_name=registration.descriptor.name,
                    allowed_agent_ids=frozenset({"core_router"}),
                )
            )
        catalog.freeze()

        class ResourceBoundary:
            def extract(self, invocation):
                return (
                    None  # 这些 Tool 不访问文件系统，scope 由 adapter/Provider 检验。
                )

        invoker = GovernedToolInvoker(
            registry,
            ToolGovernanceService(catalog, agent_registry),
            ToolExecutionService(
                durable_invocation_service=DurableToolInvocationService(clean_database)
            ),
            resource_authorization=ResourceBoundary(),
            durable_run_control=DurableRunControlService(clean_database),
            durable_approval=DurableApprovalService(clean_database),
            owner_id="wp01-governed-test",
        )
        operation = f"wp01:{request.remote_execution_business_key}"
        first = await invoker(
            "stage13_ci_submit",
            request.model_dump(mode="json"),
            principal_agent_id="core_router",
            operation_identity=operation,
        )
        replay = await invoker(
            "stage13_ci_submit",
            request.model_dump(mode="json"),
            principal_agent_id="core_router",
            operation_identity=operation,
        )
        assert first == replay
        assert (await provider.store.counts())["unique_executions"] == 1
        with pytest.raises(Stage8ValidationError):
            await invoker(
                "stage13_ci_submit",
                request.model_dump(mode="json"),
                principal_agent_id="failure_triage",
            )
        remote = {
            **lookup_request(request).model_dump(mode="json"),
            "remote_execution_id": first["remote_execution_id"],
        }
        found = await invoker(
            "stage13_ci_lookup",
            lookup_request(request).model_dump(mode="json"),
            principal_agent_id="core_router",
        )
        assert found["receipt"] == first
        summary = await invoker(
            "stage13_ci_summary", remote, principal_agent_id="core_router"
        )
        packet = EvidencePacket.model_validate(summary)
        assert sha256(packet.content.encode()) == packet.digest
        assert_agent_visible(json.loads(packet.content))
        assert (
            provider.counters["detail_reads"]
            == provider.counters["artifact_reads"]
            == 0
        )
        http_summary = await client.post("/v1/summary", json=remote)
        assert http_summary.headers["X-Content-SHA256"] == sha256(http_summary.content)


@pytest.mark.asyncio
async def test_lookup_adapter_rejects_seal_receipt_from_another_binding():
    from datetime import UTC, datetime
    from core.stage13.contracts import LookupResult, SealReceipt

    config = workload_config()
    workload = Stage13Workload(config)
    request = workload.request(0)
    wrong = LookupResult(
        result="NOT_CREATED_FINAL",
        seal_receipt=SealReceipt(
            provider_namespace_id="other-namespace",
            remote_execution_business_key=request.remote_execution_business_key,
            request_digest=request.request_digest,
            sealed_at=datetime.now(UTC),
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=wrong.model_dump(mode="json"))
        ),
        base_url="http://controlled-ci",
    ) as client:
        adapter = next(
            item.adapter
            for item in build_controlled_ci_tool_registrations(client, config)
            if item.descriptor.name == "stage13_ci_lookup"
        )
        invocation = adapter.build_invocation(lookup_request(request).model_dump_json())
        with pytest.raises(ToolAdapterInvocationError) as caught:
            await adapter.invoke_once(
                invocation, None
            )  # readonly，无 side-effect checkpoint。
        assert caught.value.safe_error_code == "STAGE13_PROVIDER_RECEIPT_INVALID"


@pytest.mark.REAL_PROCESS_CRASH_E2E
def test_real_http_process_restart_response_loss_and_receipt_replay(
    clean_database, pg_schema, tmp_path
):
    config = workload_config()
    workload = Stage13Workload(config)
    plan = next(plan for plan in workload.plans if plan.failed_cases)
    request = workload.request(plan.index)
    config_path = tmp_path / "http-config.json"
    config_path.write_text(config.model_dump_json(), encoding="utf-8")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = {
        **os.environ,
        "STAGE13_PROVIDER_DATABASE_URL": pg_schema,
        "STAGE13_PROVIDER_CONFIG_PATH": str(config_path),
        "STAGE13_PROVIDER_AGENT_TOKEN": "synthetic-http-test-only",
        "STAGE13_PROVIDER_RESPONSE_LOSS_KEYS": json.dumps(
            [request.remote_execution_business_key]
        ),
        "PYTHONIOENCODING": "utf-8",
    }
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "core.stage13.http:provider_app_from_environment",
        "--factory",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--log-level",
        "warning",
        "--no-access-log",
    ]
    processes = []
    with httpx.Client(
        base_url=f"http://127.0.0.1:{port}",
        headers={"Authorization": "Bearer synthetic-http-test-only"},
        timeout=5,
    ) as client:

        def start():
            process = subprocess.Popen(
                command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            processes.append(process)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                assert (
                    process.poll() is None
                ), "Provider process exited before readiness"
                try:
                    result = client.post(
                        "/v1/lookup",
                        json=lookup_request(request).model_dump(mode="json"),
                    )
                    if result.status_code == 200:
                        return process
                except httpx.TransportError:
                    pass
                time.sleep(0.05)
            pytest.fail("Provider HTTP readiness deadline exceeded")

        try:
            first = start()
            lost = client.post("/v1/submit", json=request.model_dump(mode="json"))
            assert lost.status_code == 503 and lost.json() == {
                "code": "RESPONSE_LOST_AFTER_COMMIT"
            }
            first.kill()
            first.wait(timeout=10)
            start()
            found = client.post(
                "/v1/lookup", json=lookup_request(request).model_dump(mode="json")
            ).json()
            assert found["result"] == "FOUND"
            replay = client.post("/v1/submit", json=request.model_dump(mode="json"))
            assert replay.json() == found["receipt"]
            assert replay.headers["X-Receipt-Replayed"] == "true"

            async def complete():
                provider = await create_provider(clean_database, config=config)
                await ControlledProviderOperator(provider.store).advance_clock(
                    60 + plan.duration_seconds
                )
                assert (await provider.store.counts())["unique_executions"] == 1

            asyncio.run(complete())
            remote = {
                **lookup_request(request).model_dump(mode="json"),
                "remote_execution_id": found["receipt"]["remote_execution_id"],
            }
            summary = client.post("/v1/summary", json=remote)
            assert summary.json()["remote_state"] == "COMPLETED"
            assert sha256(summary.content) == summary.headers["X-Content-SHA256"]
            detail = client.post("/v1/failure-detail", json=remote)
            assert detail.status_code == 200 and len(detail.json()["cases"]) == len(
                plan.failed_cases
            )
            assert sha256(detail.content) == detail.headers["X-Content-SHA256"]
            assert_agent_visible(detail.json())
            assert client.post("/v1/hidden-gt", json={}).status_code == 404
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
