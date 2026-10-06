"""受控真实进程 admission/replay/crash probe，不进入产品入口。"""

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.persistence.database import Database, DatabaseConfig
from core.stage13.incident import IncidentAggregationService


async def main():
    db = Database(DatabaseConfig(url=os.environ["LOCAL_AGENT_TEST_DATABASE_URL"]))
    service = IncidentAggregationService(db, "stage13-controlled")
    mode = sys.argv[1]

    def fault(point):
        if len(sys.argv) > 2 and point == sys.argv[2]:
            os._exit(41)

    try:
        if mode == "admit":
            await service.seal("automation-demo", "nightly-suite", "2026-10-06")
            print(
                json.dumps(
                    await service.admit(
                        "automation-demo", "nightly-suite", "2026-10-06", fault=fault
                    )
                ),
                flush=True,
            )
        elif mode == "ingest":
            value = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
            await service.ingest(value["version_id"], value["pages"], fault=fault)
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
