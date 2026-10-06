"""真实 OS crash probe；仅 TEST_SCOPE 使用 os._exit。"""

import asyncio
import json
import os
from pathlib import Path
import sys

import httpx

from core.persistence.database import Database, DatabaseConfig
from core.stage13.contracts import WorkloadConfig
from core.stage13.guardian import GuardianScheduleService
from core.stage13.guardian_worker import GuardianWorker, build_guardian_invoker


async def probe():
    config = WorkloadConfig.model_validate_json(
        Path(sys.argv[2]).read_text(encoding="utf-8")
    )
    database = Database(DatabaseConfig(url=os.environ["LOCAL_AGENT_TEST_DATABASE_URL"]))
    async with httpx.AsyncClient(
        base_url=os.environ["WP02_PROVIDER_URL"],
        headers={"Authorization": "Bearer wp02-process-test-only"},
    ) as client:
        service = GuardianScheduleService(database, config.owner_scope_id)
        mode = sys.argv[1]

        def crash(point):
            if point == mode:
                os._exit(41)

        worker = GuardianWorker(
            service,
            build_guardian_invoker(
                database, client, config, owner_id=f"probe-{os.getpid()}"
            ),
            enabled=True,
            fault=crash,
        )
        try:
            if mode == "after_claim":
                assert len(await service.claim_due(1)) == 1
                os._exit(42)
            count = await worker.tick()
            print(json.dumps({"claims": count}), flush=True)
        finally:
            await worker.close()
            await database.dispose()


if __name__ == "__main__":
    asyncio.run(probe())
