"""Stage8 Adapter 扩展，HTTP 调用必须由 GovernedToolInvoker/ToolExecution 执行。"""

from __future__ import annotations

from dataclasses import replace
import json

import httpx

from core.runtime.tool_adapters import ToolAdapterInvocationError, ToolAdapterResponse
from core.runtime.tool_contract import (
    ToolErrorCategory,
    ToolInvocation,
    ToolSideEffectState,
    thaw_json,
)
from core.runtime.tool_registry import ToolDescriptor, ToolRegistration
from core.stage8.platforms import Stage8PlatformToolAdapter
from core.stage13.contracts import (
    ArtifactRequest,
    DetailRequest,
    EvidencePacket,
    LookupRequest,
    LookupResult,
    RemoteRequest,
    SubmitReceipt,
    SubmitRequest,
    WorkloadConfig,
)


class ControlledCIAdapter(Stage8PlatformToolAdapter):
    """caller-owned AsyncClient 负责生命周期；adapter 无重试、无状态恢复 Authority。"""

    is_async = True

    def __init__(
        self,
        name,
        request_type,
        result_type,
        client: httpx.AsyncClient,
        config: WorkloadConfig,
        path,
        *,
        submit=False,
    ):
        super().__init__(name, request_type, result_type, None, side_effect=submit)
        self.client, self.config, self.path, self.submit = client, config, path, submit
        self.spec = replace(
            self.spec,
            max_output_bytes=64 * 1024 if path == "summary" else 256 * 1024,
            default_timeout_seconds=10,
            supports_idempotency_replay=submit,
        )

    def build_invocation(self, argument_text):
        invocation = super().build_invocation(argument_text)
        if not self.submit:
            return invocation
        request = SubmitRequest.model_validate(thaw_json(invocation.arguments))
        if (
            request.provider_namespace_id,
            request.owner_scope_id,
            request.automation_project_id,
            request.suite_id,
        ) != (
            self.config.provider_namespace_id,
            self.config.owner_scope_id,
            self.config.automation_project_id,
            self.config.suite_id,
        ):
            raise ValueError("工具请求越过已绑定 scope")
        return ToolInvocation.create(
            tool_name=self.spec.tool_name,
            arguments=request.model_dump(mode="json"),
            idempotency_key=request.remote_execution_business_key,
            resource_key=f"stage13:{self.config.provider_namespace_id}:{request.environment_id}",
        )

    async def invoke_once(self, invocation, context):
        request = self.request_type.model_validate(thaw_json(invocation.arguments))
        if self.submit:
            context.before_side_effect()
        try:
            response = await self.client.post(
                f"/v1/{self.path}", json=request.model_dump(mode="json"), timeout=10
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            rejected = isinstance(
                exc, httpx.HTTPStatusError
            ) and exc.response.status_code in {401, 409, 422}
            state = (
                ToolSideEffectState.UNKNOWN
                if self.submit and not rejected
                else ToolSideEffectState.NOT_STARTED
            )
            raise ToolAdapterInvocationError(
                category=(
                    ToolErrorCategory.SIDE_EFFECT_UNKNOWN
                    if state is ToolSideEffectState.UNKNOWN
                    else ToolErrorCategory.TRANSIENT
                ),
                safe_error_code=(
                    "STAGE13_SUBMIT_UNKNOWN"
                    if state is ToolSideEffectState.UNKNOWN
                    else "STAGE13_PROVIDER_REQUEST_FAILED"
                ),
                safe_message="Controlled CI Provider 请求未取得有效回执。",
                side_effect_state=state,
                side_effect_state_authoritative=rejected or not self.submit,
            ) from None
        try:
            if self.result_type is EvidencePacket:
                metadata = json.loads(response.headers["X-Stage13-Evidence"])
                result = EvidencePacket(
                    **metadata, content=response.content.decode("utf-8")
                )
                if response.headers["X-Content-SHA256"] != result.digest:
                    raise ValueError("HTTP body digest 不匹配")
                if (
                    result.owner_scope_id != self.config.owner_scope_id
                    or result.remote_execution_id != request.remote_execution_id
                ):
                    raise ValueError("证据 scope/remote binding 不匹配")
            else:
                result = self.result_type.model_validate(response.json())
                receipt = (
                    result
                    if isinstance(result, SubmitReceipt)
                    else result.receipt or result.seal_receipt
                )
                if receipt is not None and (
                    receipt.provider_namespace_id != self.config.provider_namespace_id
                    or receipt.remote_execution_business_key
                    != request.remote_execution_business_key
                    or receipt.request_digest != request.request_digest
                    or (
                        isinstance(request, RemoteRequest)
                        and getattr(receipt, "remote_execution_id", None)
                        != request.remote_execution_id
                    )
                ):
                    raise ValueError("Provider receipt binding 不匹配")
        except (ValueError, KeyError, UnicodeError):
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.OUTPUT_INVALID,
                safe_error_code="STAGE13_PROVIDER_RECEIPT_INVALID",
                safe_message="Controlled CI Provider 回执校验失败。",
                side_effect_state=(
                    ToolSideEffectState.UNKNOWN
                    if self.submit
                    else ToolSideEffectState.NOT_STARTED
                ),
                side_effect_state_authoritative=not self.submit,
            ) from None
        return ToolAdapterResponse(
            content=result.model_dump_json(),
            content_type="application/json",
            safe_summary=f"{self.spec.tool_name} completed",
            side_effect_state=(
                ToolSideEffectState.COMMITTED
                if self.submit
                else ToolSideEffectState.NOT_STARTED
            ),
            idempotency_replayed=self.submit
            and response.headers.get("X-Receipt-Replayed") == "true",
            provider_operation_id=(
                str(result.remote_execution_id)
                if isinstance(result, SubmitReceipt)
                else None
            ),
        )


def build_controlled_ci_tool_registrations(
    client: httpx.AsyncClient, config: WorkloadConfig
) -> tuple[ToolRegistration, ...]:
    """WP02 可注册到既有 Registry + explicit ToolPolicy；没有 seal/GT/control tool。"""
    definitions = (
        ("stage13_ci_submit", SubmitRequest, SubmitReceipt, "submit", True),
        ("stage13_ci_lookup", LookupRequest, LookupResult, "lookup", False),
        (
            "stage13_ci_lookup_remote",
            RemoteRequest,
            LookupResult,
            "lookup-remote",
            False,
        ),
        ("stage13_ci_summary", RemoteRequest, EvidencePacket, "summary", False),
        (
            "stage13_ci_failure_detail",
            DetailRequest,
            EvidencePacket,
            "failure-detail",
            False,
        ),
        ("stage13_ci_artifact", ArtifactRequest, EvidencePacket, "artifact", False),
    )
    return tuple(
        ToolRegistration(
            descriptor=ToolDescriptor(
                name=name, description=f"Controlled CI {path}; scoped and bounded."
            ),
            adapter=ControlledCIAdapter(
                name, request_type, result_type, client, config, path, submit=submit
            ),
        )
        for name, request_type, result_type, path, submit in definitions
    )
