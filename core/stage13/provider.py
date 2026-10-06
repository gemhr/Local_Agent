"""分层 CI 证据与受控故障；不创建 Guardian、Incident 或 Evaluation。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from core.stage13.contracts import (
    ArtifactContent,
    ArtifactRequest,
    CaseCounts,
    CISummary,
    DetailRequest,
    FailureDetail,
    FailureIndex,
    FailurePage,
    LookupRequest,
    ProviderError,
    RemoteRequest,
    SubmitReceipt,
    SubmitRequest,
    canonical_bytes,
    evidence_packet,
    sha256,
)
from core.stage13.store import ProviderStore
from core.stage13.workload import CHANGES, COMPONENTS, ExecutionPlan

COUNTERS = (
    "submit_attempts",
    "receipt_replays",
    "lookup_requests",
    "response_loss_injected",
    "key_conflicts",
    "summary_reads",
    "detail_reads",
    "artifact_reads",
    "lookup_failures_injected",
    "status_failures_injected",
    "delivered_bytes",
)


@dataclass
class ControlledFaults:
    """TEST_SCOPE/受控 harness 配置，不属于 Agent request。"""

    response_loss_keys: frozenset[str] = frozenset()
    lookup_failures: Counter = field(default_factory=Counter)
    status_failures: Counter = field(default_factory=Counter)
    result_delay_seconds: int = 0

    def __post_init__(self):
        if type(self.result_delay_seconds) is not int or self.result_delay_seconds < 0:
            raise ValueError("result delay 必须为非负整数")
        if any(
            type(count) is not int or count < 0
            for count in (
                *self.lookup_failures.values(),
                *self.status_failures.values(),
            )
        ):
            raise ValueError("故障次数必须为非负整数")


class ControlledCIProvider:
    """仅提供 remote truth API；控制、GT 导出在独立 operator/offline 入口。"""

    def __init__(self, store: ProviderStore, *, faults: ControlledFaults | None = None):
        self.store = store
        self.workload = store.workload
        self.faults = faults or ControlledFaults()
        self.counters = Counter({name: 0 for name in COUNTERS})

    async def initialize(self):
        await self.store.initialize()

    async def submit_execution(self, request: SubmitRequest):
        receipt, _ = await self.submit_execution_with_replay(request)
        return receipt

    async def submit_execution_with_replay(self, request: SubmitRequest):
        self.counters["submit_attempts"] += 1
        # frozen DTO 内有 dict；重新校验当前实际内容，不能信任调用方旧 validation。
        request = SubmitRequest.model_validate(request.model_dump(mode="json"))
        plan = self.workload.plan_for(request)
        try:
            result = await self.store.submit(
                request,
                plan,
                lose_response=request.remote_execution_business_key
                in self.faults.response_loss_keys,
                result_delay_seconds=self.faults.result_delay_seconds,
            )
        except ProviderError as exc:
            if exc.code == "RESPONSE_LOST_AFTER_COMMIT":
                self.counters["response_loss_injected"] += 1
            elif exc.code == "CONFLICT":
                self.counters["key_conflicts"] += 1
            raise
        self.counters["receipt_replays"] += int(result[1])
        return result

    def _temporary_fault(self, counter: Counter, key: str, name: str):
        if counter[key] > 0:
            counter[key] -= 1
            self.counters[name] += 1
            raise ProviderError("TEMPORARILY_UNAVAILABLE")

    async def lookup_by_business_key(self, request: LookupRequest):
        self.counters["lookup_requests"] += 1
        self._temporary_fault(
            self.faults.lookup_failures,
            request.remote_execution_business_key,
            "lookup_failures_injected",
        )
        try:
            return await self.store.lookup(
                request.remote_execution_business_key, request.request_digest
            )
        except ProviderError as exc:
            self.counters["key_conflicts"] += int(exc.code == "CONFLICT")
            raise

    async def lookup_by_remote_execution_id(self, request: RemoteRequest):
        self.counters["lookup_requests"] += 1
        self._temporary_fault(
            self.faults.lookup_failures,
            request.remote_execution_business_key,
            "lookup_failures_injected",
        )
        return await self.store.lookup(
            request.remote_execution_business_key,
            request.request_digest,
            str(request.remote_execution_id),
        )

    async def _read(self, request: RemoteRequest):
        value = await self.store.read_execution(
            request.remote_execution_business_key,
            request.request_digest,
            str(request.remote_execution_id),
        )
        return (
            value,
            SubmitRequest.model_validate(value["request"]),
            SubmitReceipt.model_validate(value["receipt"]),
            ExecutionPlan.restore(value["plan"]),
        )

    @staticmethod
    def _result_ready(value):
        return (
            value["state"] == "COMPLETED"
            and value["clock"]
            >= value["terminal_logical_time"] + value["result_delay_seconds"]
        )

    def _delivered(self, packet):
        self.counters["delivered_bytes"] += len(packet.content.encode("utf-8"))
        return packet

    async def get_ci_summary(self, request: RemoteRequest):
        self.counters["summary_reads"] += 1
        self._temporary_fault(
            self.faults.status_failures,
            request.remote_execution_business_key,
            "status_failures_injected",
        )
        value, original, receipt, plan = await self._read(request)
        ready = self._result_ready(value)
        counts = None
        failures = ()
        if ready:
            counts = CaseCounts(
                PASS=plan.case_count
                - len(plan.failed_cases)
                - len(plan.error_cases)
                - len(plan.skipped_cases),
                FAILED=len(plan.failed_cases),
                ERROR=len(plan.error_cases),
                SKIPPED=len(plan.skipped_cases),
            )
            failures = tuple(
                FailureIndex(**self.workload.case_result(plan, index))
                for index in sorted(
                    [index for index, _ in plan.failed_cases] + list(plan.error_cases)
                )
            )
        semantic = {
            "remote_state": value["state"],
            "status_revision": value["status_revision"],
            "result_revision": 1 if ready else 0,
            "result_available": ready,
            "case_counts": counts.model_dump(mode="json") if counts else None,
            "failure_index": [failure.model_dump(mode="json") for failure in failures],
        }
        body = CISummary(
            **semantic,
            remote_execution_id=receipt.remote_execution_id,
            environment_id=original.environment_id,
            channel_group=original.channel_group,
            automation_project_id=original.automation_project_id,
            suite_id=original.suite_id,
            ordinal=original.ordinal,
            product_version=original.product_version,
            visible_component_inventory=COMPONENTS,
            visible_change_inventory=CHANGES,
            environment_metadata={
                "config_revision": "synthetic-config-v1",
                "network": "isolated-synthetic",
                "channel_group": original.channel_group,
            },
            visible_semantic_digest=sha256(canonical_bytes(semantic)),
        )
        return self._delivered(evidence_packet("CI_SUMMARY", body, original, receipt))

    async def fetch_failure_detail(self, request: DetailRequest):
        self.counters["detail_reads"] += 1
        value, original, receipt, plan = await self._read(request)
        if not self._result_ready(value):
            raise ProviderError("RESULT_NOT_READY")
        if value["clock"] > value["evidence_retention_until"]:
            raise ProviderError("EVIDENCE_EXPIRED")
        indices = sorted(
            [index for index, _ in plan.failed_cases] + list(plan.error_cases)
        )
        if request.offset > len(indices):
            raise ProviderError("PAGE_OUT_OF_RANGE")
        selected = indices[request.offset : request.offset + request.page_size]
        next_offset = request.offset + len(selected)
        body = FailurePage(
            remote_execution_id=receipt.remote_execution_id,
            cases=tuple(
                FailureDetail(**self.workload.visible_failure(plan, index))
                for index in selected
            ),
            total_failure_cases=len(indices),
            next_offset=next_offset if next_offset < len(indices) else None,
        )
        return self._delivered(
            evidence_packet("FAILURE_DETAIL", body, original, receipt)
        )

    async def fetch_artifact(self, request: ArtifactRequest):
        self.counters["artifact_reads"] += 1
        value, original, receipt, plan = await self._read(request)
        if not self._result_ready(value):
            raise ProviderError("RESULT_NOT_READY")
        ref_prefix = "artifact:case-"
        if (
            not request.artifact_ref.startswith(ref_prefix)
            or not request.artifact_ref[len(ref_prefix) :].isdecimal()
        ):
            raise ProviderError("ARTIFACT_NOT_FOUND")
        case_index = int(request.artifact_ref[len(ref_prefix) :])
        if case_index not in dict(plan.failed_cases):
            raise ProviderError("ARTIFACT_NOT_FOUND")
        visible = self.workload.visible_failure(plan, case_index)
        if request.artifact_ref not in visible["artifact_refs"]:
            raise ProviderError("ARTIFACT_NOT_FOUND")
        # 受控保留 policy：terminal logical time 后七天，身份/receipt不删除。
        if value["clock"] > value["evidence_retention_until"]:
            availability = "EXPIRED"
        else:
            availability = self.workload.artifact_mode(plan.index, case_index)
            if (
                availability == "DELAYED"
                and value["clock"] >= value["terminal_logical_time"] + 300
            ):
                availability = "AVAILABLE"
        body = ArtifactContent(
            artifact_ref=request.artifact_ref,
            snippet=(
                f"[redacted synthetic log]\ncomponent={visible['component']}\n{visible['error_code']}\n{visible['error_excerpt']}\nchange refs={','.join(visible['visible_change_refs'])}"
                if availability == "AVAILABLE"
                else None
            ),
        )
        return self._delivered(
            evidence_packet(
                "AUTHORIZED_ARTIFACT",
                body,
                original,
                receipt,
                availability=availability,
                provider_case_id=visible["provider_case_id"],
            )
        )

    async def metrics(self):
        return {
            "counter_scope": "current provider process lifetime",
            **dict(self.counters),
            **await self.store.counts(),
        }


class ControlledProviderOperator:
    """仅受控 Python operator/harness 使用；没有 Agent tool 或 HTTP route。"""

    def __init__(self, store: ProviderStore):
        self.store = store

    async def seal_absent_key(self, request: LookupRequest):
        return await self.store.seal(
            request.remote_execution_business_key, request.request_digest
        )

    async def advance_clock(self, logical_time: int):
        await self.store.advance_clock(logical_time)

    async def advance_execution(self, business_key: str, state: str):
        await self.store.advance_execution(business_key, state)
