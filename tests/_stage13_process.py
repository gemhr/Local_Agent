"""WP01 独立 OS 进程 probe；只访问指定的 _test DB，不读取用户 data/。"""

import asyncio
import json
import os
from pathlib import Path
import sys

from sqlalchemy.engine import make_url

from core.persistence.database import Database, DatabaseConfig
from core.stage13.contracts import LookupRequest, ProviderError, WorkloadConfig
from core.stage13.provider import ControlledCIProvider, ControlledFaults
from core.stage13.store import ProviderStore
from core.stage13.workload import Stage13Workload


async def main():
    url = os.environ["LOCAL_AGENT_TEST_DATABASE_URL"]
    if not make_url(url).database.endswith("_test"):
        raise RuntimeError("TEST_DATABASE_REQUIRED")
    config = WorkloadConfig.model_validate_json(
        Path(sys.argv[2]).read_text(encoding="utf-8")
    )
    database = Database(DatabaseConfig(url=url, use_null_pool=True))
    workload = Stage13Workload(config)
    request = workload.request(int(sys.argv[3]))
    loss = sys.argv[1] == "submit-loss"
    provider = ControlledCIProvider(
        ProviderStore(database, workload),
        faults=ControlledFaults(
            response_loss_keys=(
                frozenset({request.remote_execution_business_key})
                if loss
                else frozenset()
            )
        ),
    )
    await provider.initialize()
    if sys.argv[1] == "concurrent-submit":
        print("READY", flush=True)
        if sys.stdin.readline().strip() != "GO":
            raise RuntimeError("PROCESS_BARRIER_REQUIRED")
    try:
        if sys.argv[1] == "lookup":
            result = await provider.lookup_by_business_key(
                LookupRequest(
                    remote_execution_business_key=request.remote_execution_business_key,
                    request_digest=request.request_digest,
                )
            )
        else:
            result = await provider.submit_execution(request)
        print(result.model_dump_json(), flush=True)
    except ProviderError as exc:
        if loss and exc.code == "RESPONSE_LOST_AFTER_COMMIT":
            os._exit(23)  # commit 后真实退出，不执行 dispose/finally。
        raise
    finally:
        await database.dispose()


if __name__ == "__main__":
    asyncio.run(main())
