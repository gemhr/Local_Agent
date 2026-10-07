"""独立 opt-in 持久交付入口，不初始化模型，不改变 evaluation endpoint。"""

from contextlib import asynccontextmanager
import hmac
import os
import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from core.persistence.database import Database, DatabaseConfig
from core.settings import Settings
from core.stage13.delivery import DeliveryService, EvalOpsAuthorizationClient


class DeliveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    protocol_version: str
    delivery_id: str
    binding: dict
    gate_authorization: dict | None = None


def compose_delivery(database, client):
    project = os.environ["LOCAL_AGENT_STAGE13_DELIVERY_PROJECT"]
    authority = EvalOpsAuthorizationClient(
        client,
        os.environ["LOCAL_AGENT_STAGE13_EVALOPS_URL"],
        project,
        os.environ["LOCAL_AGENT_STAGE13_EVALOPS_KEY"],
    )
    return DeliveryService(
        database,
        authority,
        os.environ["LOCAL_AGENT_STAGE13_SCOPE"],
        project,
        os.environ["LOCAL_AGENT_STAGE13_DESTINATION_ID"],
    )


@asynccontextmanager
async def lifespan(app):
    if os.getenv("LOCAL_AGENT_STAGE13_DELIVERY_ENABLED") != "1":
        raise RuntimeError("STAGE13_DELIVERY_OPT_IN_REQUIRED")
    database = Database(DatabaseConfig.from_settings(Settings.load()))
    async with httpx.AsyncClient(trust_env=False) as client:
        try:
            await database.verify_reachable()
            app.state.stage13_delivery_service = compose_delivery(database, client)
            yield
        finally:
            await database.close()


router = APIRouter()


@router.post("/api/runtime/stage13/delivery/v1")
async def delivery(body: DeliveryRequest, request: Request):
    token = os.getenv("LOCAL_AGENT_STAGE13_SERVICE_TOKEN", "")
    if not token or not hmac.compare_digest(
        request.headers.get("authorization", ""), "Bearer " + token
    ):
        raise HTTPException(401, "SERVICE_CREDENTIAL_REQUIRED")
    service = getattr(request.app.state, "stage13_delivery_service", None)
    if service is None:
        raise HTTPException(503, "STAGE13_DELIVERY_OPT_IN_REQUIRED")
    try:
        return await service.deliver(body.model_dump())
    except (KeyError, ValueError, TypeError):
        raise HTTPException(409, "STAGE13_DELIVERY_BINDING_REJECTED") from None


app = FastAPI(lifespan=lifespan)
app.include_router(router)
