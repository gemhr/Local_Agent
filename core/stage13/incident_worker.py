"""opt-in WP03 取证 worker；只准备 READY，不装配 Agent/LLM。"""

import asyncio
import os
import json
from pathlib import Path
from uuid import uuid4

import httpx

from core.persistence.database import Database, DatabaseConfig
from core.settings import Settings
from core.stage13.contracts import WorkloadConfig
from core.stage13.guardian import StaleClaim
from core.stage13.guardian_worker import build_guardian_invoker
from core.stage13.incident import IncidentAggregationService
from core.stage8.execution import Stage8ToolInvocationError, Stage8ValidationError


class IncidentWorker:
    def __init__(self, service, invoker, *, enabled=False, concurrency=8):
        if not 1 <= concurrency <= 20:
            raise ValueError("concurrency 必须在 1..20")
        self.service, self.invoker, self.enabled = service, invoker, enabled
        self.concurrency, self.stopping = concurrency, False
        self._tick_lock = asyncio.Lock()

    async def execute(self, claim):
        try:
            if claim[3]:
                await self.service.ingest(claim[0], [], claim=claim, missing=True)
                return
            version, packet = await self.service.source(claim[0])
            pages, offset = [], 0
            while True:
                payload = {
                    "remote_execution_business_key": version.business_key,
                    "request_digest": version.request_digest,
                    "remote_execution_id": version.remote_id,
                    "offset": offset,
                    "page_size": 50,
                }
                page = await self.invoker(
                    "stage13_ci_failure_detail",
                    payload,
                    principal_agent_id="core_router",
                    operation_identity=f"stage13-detail:{claim[0]}:{claim[2]}:{offset}",
                )
                pages.append(page)
                body = json.loads(page["content"])
                offset = body["next_offset"]
                if offset is None:
                    break
                if len(pages) == 4:
                    raise ValueError("DETAIL_PAGE_BUDGET")
            await self.service.ingest(claim[0], pages, claim=claim)
        except (Stage8ToolInvocationError, Stage8ValidationError, ValueError):
            await self.service.fail(claim)
        except StaleClaim:
            return

    async def tick(self):
        if not self.enabled or self.stopping:
            return 0
        async with self._tick_lock:
            await self.service.discover()
            claims = await self.service.claim(self.concurrency)
            await asyncio.gather(*(self.execute(c) for c in claims))
            return len(claims)

    async def close(self):
        self.stopping = True
        async with self._tick_lock:
            pass


async def run_from_environment():
    if os.getenv("STAGE13_AGGREGATION_ENABLED", "false").lower() != "true":
        raise ValueError("STAGE13_AGGREGATION_ENABLED 未启用")
    config = WorkloadConfig.model_validate_json(
        Path(os.environ["STAGE13_PROVIDER_CONFIG_PATH"]).read_text(encoding="utf-8")
    )
    database = Database(DatabaseConfig.from_settings(Settings.load()))
    async with httpx.AsyncClient(
        base_url=os.environ["STAGE13_PROVIDER_BASE_URL"],
        headers={
            "Authorization": "Bearer " + os.environ["STAGE13_PROVIDER_AGENT_TOKEN"]
        },
    ) as client:
        service = IncidentAggregationService(database, config.owner_scope_id)
        worker = IncidentWorker(
            service,
            build_guardian_invoker(
                database,
                client,
                config,
                owner_id=f"wp03-{uuid4()}",
                failure_details=True,
            ),
            enabled=True,
        )
        try:
            while True:
                await worker.tick()
                await service.seal(
                    config.automation_project_id, config.suite_id, config.business_date
                )
                await service.admit(
                    config.automation_project_id, config.suite_id, config.business_date
                )
                await asyncio.sleep(1)
        finally:
            await worker.close()
            await database.dispose()


if __name__ == "__main__":
    asyncio.run(run_from_environment())
