"""opt-in application worker；固定短任务池通过正式 GovernedToolInvoker 访问 CI。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx

from core.persistence.database import Database, DatabaseConfig
from core.runtime.agent_registry import AgentRegistry, DEFAULT_AGENT_REGISTRY
from core.runtime.durable_approval import DurableApprovalService
from core.runtime.resource_authorization import (
    FilesystemResourcePolicy,
    ResourceAuthorizationService,
    ToolResourceExtractorCatalog,
)
from core.runtime.run_control import DurableRunControlService
from core.runtime.tool_execution import ToolExecutionService
from core.runtime.tool_governance import (
    ToolGovernanceService,
    ToolPolicy,
    ToolPolicyCatalog,
)
from core.runtime.tool_idempotency import DurableToolInvocationService
from core.runtime.tool_registry import ToolRegistry
from core.stage8.execution import (
    GovernedToolInvoker,
    Stage8ToolInvocationError,
    Stage8ValidationError,
)
from core.stage13.adapters import build_controlled_ci_tool_registrations
from core.stage13.contracts import WorkloadConfig
from core.stage13.guardian import GuardianScheduleService, StaleClaim


def build_guardian_invoker(database, client, config, *, owner_id):
    """为后台业务主体显式编译局部 grants；不更改默认 Agent Registry。"""
    admission = GuardianScheduleService(database, config.owner_scope_id)

    async def before_request(request):
        if not request.url.path.startswith("/v1/"):
            raise ValueError("Guardian HTTP client 只允许 Provider tools")
        while delay := await admission.admit_http(
            submit=request.url.path == "/v1/submit"
        ):
            # 有界 Tool timeout 覆盖等待；DB budget 才是 admission truth。
            await asyncio.sleep(delay)

    before_request.stage13_scope = config.owner_scope_id
    existing_hooks = [
        hook for hook in client.event_hooks["request"] if hasattr(hook, "stage13_scope")
    ]
    if any(hook.stage13_scope != config.owner_scope_id for hook in existing_hooks):
        raise ValueError("Guardian HTTP client 不允许跨 scope 复用")
    if not existing_hooks:
        client.event_hooks["request"].append(before_request)
    registry = ToolRegistry()
    registrations = build_controlled_ci_tool_registrations(client, config)
    allowed = frozenset(
        {"stage13_ci_submit", "stage13_ci_lookup", "stage13_ci_summary"}
    )
    for registration in registrations:
        if registration.descriptor.name in allowed:
            registry.register(registration)
    registry.freeze()
    agent_registry = AgentRegistry(
        (
            replace(
                DEFAULT_AGENT_REGISTRY.resolve("core_router"),
                actual_allowed_tools=allowed,
            ),
        )
    )
    catalog = ToolPolicyCatalog(tool_registry=registry, agent_registry=agent_registry)
    for name in allowed:
        catalog.register(
            ToolPolicy(tool_name=name, allowed_agent_ids=frozenset({"core_router"}))
        )
    catalog.freeze()
    extractors = ToolResourceExtractorCatalog()
    extractors.freeze()
    return GovernedToolInvoker(
        registry,
        ToolGovernanceService(catalog, agent_registry),
        ToolExecutionService(
            durable_invocation_service=DurableToolInvocationService(database)
        ),
        resource_authorization=ResourceAuthorizationService(
            FilesystemResourcePolicy(()), extractors
        ),
        durable_run_control=DurableRunControlService(database),
        durable_approval=DurableApprovalService(database),
        owner_id=owner_id,
    )


class GuardianWorker:
    def __init__(self, service, invoker, *, enabled=False, concurrency=20, fault=None):
        if not 1 <= concurrency <= 20:
            raise ValueError("worker concurrency 必须在 1..20")
        self.service, self.invoker = service, invoker
        self.enabled, self.concurrency, self.fault = enabled, concurrency, fault
        self.stopping = False
        self._drained = asyncio.Event()
        self._drained.set()
        self._tick_lock = asyncio.Lock()

    async def execute_claim(self, claim):
        try:
            prepared = await self.service.prepare(claim)
            if prepared is None:
                return
            if self.fault:
                self.fault("after_intent_commit")
            name = {
                "DISPATCH_VERSION": "stage13_ci_submit",
                "RECONCILE_REMOTE": "stage13_ci_lookup",
                "POLL_REMOTE": "stage13_ci_summary",
                "FETCH_TERMINAL_RESULT": "stage13_ci_summary",
            }[prepared.operation]
            try:
                result = await self.invoker(
                    name,
                    prepared.payload,
                    principal_agent_id="core_router",
                    operation_identity=prepared.operation_identity,
                )
            except Stage8ToolInvocationError as exc:
                await self.service.finish(
                    prepared, error=exc.safe_error_code, uncertain=exc.outcome_unknown
                )
                return
            except Stage8ValidationError:
                await self.service.finish(prepared, error="TOOL_PERMISSION_DENIED")
                return
            if self.fault:
                self.fault("after_provider_receipt")
            await self.service.finish(prepared, result, fault=self.fault)
        except StaleClaim:
            # 旧 writer 被 fence 拒绝；由当前 owner / recovery 接手。
            return

    async def tick(self):
        if not self.enabled or self.stopping:
            return 0
        async with self._tick_lock:
            if self.stopping:
                return 0
            self._drained.clear()
            try:
                claims = await self.service.claim_due(self.concurrency)
                batch = asyncio.gather(
                    *(self.execute_claim(c) for c in claims), return_exceptions=True
                )
                try:
                    results = await asyncio.shield(batch)
                except asyncio.CancelledError:
                    # shutdown 停止 claim，并等待已开始的 bounded HTTP/DB 操作收口。
                    await batch
                    raise
                # 完成整个 bounded batch 后传播真实错误；不遗留无人等待的任务。
                for result in results:
                    if isinstance(result, BaseException):
                        raise result
                return len(claims)
            finally:
                self._drained.set()

    async def close(self):
        self.stopping = True
        await self._drained.wait()


async def run_from_environment():
    """独立 application composition；默认禁用，不依赖 LLM、不启动旧 mock。"""
    if os.getenv("STAGE13_GUARDIAN_ENABLED", "false").lower() != "true":
        raise RuntimeError("STAGE13_GUARDIAN_ENABLED 必须显式为 true")
    from core.settings import Settings

    config = WorkloadConfig.model_validate_json(
        Path(os.environ["STAGE13_PROVIDER_CONFIG_PATH"]).read_text(encoding="utf-8")
    )
    plans = json.loads(
        Path(os.environ["STAGE13_GUARDIAN_PLAN_PATH"]).read_text(encoding="utf-8")
    )
    database = Database(DatabaseConfig.from_settings(Settings.load()))
    async with httpx.AsyncClient(
        base_url=os.environ["STAGE13_PROVIDER_BASE_URL"],
        headers={
            "Authorization": f"Bearer {os.environ['STAGE13_PROVIDER_AGENT_TOKEN']}"
        },
    ) as client:
        service = GuardianScheduleService(database, config.owner_scope_id)
        worker = GuardianWorker(
            service,
            build_guardian_invoker(
                database, client, config, owner_id=f"guardian-{uuid4()}"
            ),
            enabled=True,
        )
        try:
            await database.verify_reachable()
            await service.initialize()
            recovery_id = str(uuid4())
            while await service.recover(recovery_id):
                pass
            guardians = []
            for plan in plans:
                guardian_id = await service.register_guardian(
                    config.automation_project_id,
                    config.suite_id,
                    plan["environment_id"],
                    plan["channel_group"],
                )
                guardians.append((guardian_id, tuple(plan["expected_cases"])))
            last_day = None
            while not worker.stopping:
                from core.stage13.guardian import eligibility
                from datetime import UTC, datetime

                day, _ = eligibility(datetime.now(UTC))
                if last_day != day:
                    for guardian_id, counts in guardians:
                        await service.discover(
                            guardian_id,
                            provider_namespace=config.provider_namespace_id,
                            versions=config.product_versions,
                            expected_cases=counts,
                            plan_revision=config.generator_version,
                        )
                    last_day = day
                await worker.tick()
                # 仅控制扫描频率；durable next_available_at 才是 scheduling truth。
                await asyncio.sleep(0.1)
        finally:
            await worker.close()
            await database.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(run_from_environment())
    except KeyboardInterrupt:
        pass
