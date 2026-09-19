"""仅用于 WP5 PLATFORM_ONLY 负载执行的无 Provider HTTP endpoint。"""
from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "mode": "deterministic-platform-only"}


@app.post("/api/v1/chat")
async def start_run() -> dict[str, str]:
    # Synthetic endpoint intentionally excludes LLM, tool side effects and business data.
    return {"run_id": "synthetic-capacity-run", "status": "ACCEPTED"}
