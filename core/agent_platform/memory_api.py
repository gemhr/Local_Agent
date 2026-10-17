"""受信 Principal 的 scoped Memory HTTP 边界；不接收正文授予的权限。"""
from typing import Literal
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from core.runtime.memory_scope import MemoryAccessContext, MemoryContextError


class MemoryMutationRequest(BaseModel):
    model_config=ConfigDict(extra="forbid")
    record_id: str=Field(min_length=1,max_length=255)
    expected_version: int=Field(ge=1,strict=True)
    command_id: str=Field(min_length=1,max_length=255)
    action: Literal["DELETE","REVOKE"]
    scope_kind: Literal["USER","SESSION","PROJECT"]="USER"
    project_scope_id: str | None=Field(default=None,max_length=255)
    record_kind: Literal["MEMORY","EXCHANGE"]="MEMORY"
    deletion_scope: str=Field(default="MEMORY_STORE_AND_FUTURE_USE",max_length=64)
    session_id: str=Field(default="default",min_length=1,max_length=255)


def register_memory_api(app, *, require_service, authorize_run):
    def repository():
        from core.persistence.memory import PostgresMemoryManagerBridge
        memory=require_service().router.memory_manager
        if not isinstance(memory,PostgresMemoryManagerBridge):
            raise HTTPException(503,"MEMORY_STORE_UNAVAILABLE")
        return memory.async_store.lifecycle

    def access(request, agent_id, session_id):
        return MemoryAccessContext.from_principal(request.state.principal,agent_id,session_id)

    @app.exception_handler(MemoryContextError)
    async def memory_error(_request, error):
        from fastapi.responses import JSONResponse
        status=409 if error.error_code in {"MEMORY_VERSION_CONFLICT","MEMORY_OPERATION_CONFLICT","MEMORY_BINDING_CONFLICT"} else 403
        if error.error_code=="MEMORY_ERASURE_SCOPE_UNSUPPORTED": status=422
        return JSONResponse(status_code=status,content={"detail":error.error_code})

    @app.get("/api/memory/scoped/{agent_id}")
    async def retrieve(agent_id:str,request:Request,session_id:str="default",query:str=""):
        ctx=access(request,agent_id,session_id); repo=repository()
        scopes=await repo.private_scopes(ctx) if ctx.principal_kind=="HUMAN" else []
        scopes+=await repo.project_scopes(ctx)
        return {"scopes":scopes,"records":await repo.retrieve(ctx,[s["scope_id"] for s in scopes],query)}

    @app.post("/api/memory/scoped/{agent_id}/mutations")
    async def mutate(agent_id:str,request:Request,payload:MemoryMutationRequest):
        ctx=access(request,agent_id,payload.session_id); repo=repository()
        if payload.deletion_scope!="MEMORY_STORE_AND_FUTURE_USE":
            raise MemoryContextError("MEMORY_ERASURE_SCOPE_UNSUPPORTED")
        sid=payload.project_scope_id if payload.scope_kind=="PROJECT" else ctx.scope_id(payload.scope_kind)
        if not sid: raise MemoryContextError("MEMORY_SCOPE_INVALID")
        if payload.record_kind=="EXCHANGE":
            if payload.scope_kind!="SESSION" or payload.action!="DELETE":
                raise MemoryContextError("MEMORY_ACTION_UNSUPPORTED")
            return await repo.delete_exchange(ctx,payload.record_id,expected_version=payload.expected_version,command_id=payload.command_id)
        return await repo.mutate(ctx,sid,payload.record_id,expected_version=payload.expected_version,
            command_id=payload.command_id,action=payload.action,deletion_scope=payload.deletion_scope)

    @app.get("/api/memory/scoped/{agent_id}/receipts/{operation_id}")
    async def receipt(agent_id:str,operation_id:str,scope_id:str,request:Request,session_id:str="default"):
        return await repository().receipt(access(request,agent_id,session_id),scope_id,operation_id)

    @app.get("/api/runtime/runs/{run_id}/memory-evidence")
    async def evidence(run_id:str,agent_id:str,request:Request,session_id:str="default"):
        from core.auth import AuthorizationAction
        await authorize_run(request,run_id,AuthorizationAction.READ)
        return await repository().evidence(access(request,agent_id,session_id),run_id)
