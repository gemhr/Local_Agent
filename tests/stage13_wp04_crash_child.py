"""独立 OS 进程的定点崩溃；Provider 为测试 transport，Core 使用正式实现。"""

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.persistence.database import Database, DatabaseConfig
from tests.test_stage13_wp04_triage import assembly


async def main():
    job_id, point, replies = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
    db = Database(DatabaseConfig(url=os.environ["LOCAL_AGENT_TEST_DATABASE_URL"]))
    service, calls, client = await assembly(
        db,
        replies,
        dispatch_crash={"after_model_dispatch": 1, "after_repair_dispatch": 2}.get(
            point, 0
        ),
    )
    claim = await service.claim("crash-child", job_id)

    async def fault(name):
        if name == point:
            os._exit(73)

    await service.run_job(claim, fault=fault)
    raise RuntimeError("CRASH_POINT_NOT_REACHED")


asyncio.run(main())
