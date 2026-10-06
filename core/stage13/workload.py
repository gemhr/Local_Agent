"""紧凑确定性 workload；逐 case 重建，隐藏 GT 只用于离线导出。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import random
from typing import Literal

from pydantic import Field

from core.stage13.contracts import (
    SubmitRequest,
    Digest,
    WireModel,
    WorkloadConfig,
    business_key,
    canonical_bytes,
    sha256,
)

COMPONENTS = (
    "checkout",
    "catalog",
    "messaging",
    "identity",
    "storage",
    "search",
    "gateway",
    "telemetry",
)
CHANGES = tuple(
    f"change-{component}-{revision}"
    for component in COMPONENTS
    for revision in range(1, 4)
)
MECHANISMS = (
    "PRODUCT_BEHAVIOR",
    "TEST_LOGIC",
    "TEST_DATA",
    "ENVIRONMENT_CONFIG",
    "TOOL_EXECUTION",
    "INFRASTRUCTURE_SERVICE",
)
CATEGORIES = (
    "PRODUCT",
    "TEST_CASE",
    "TEST_DATA",
    "ENVIRONMENT",
    "TOOL_CHAIN",
    "INFRASTRUCTURE",
)
ACTIONS = (
    "INVESTIGATE_PRODUCT",
    "FIX_TEST",
    "FIX_TEST_DATA",
    "RETRY_ENVIRONMENT",
    "REPAIR_TOOL_CHAIN",
    "RESTORE_INFRASTRUCTURE",
)
ERROR_CODES = (
    "ASSERTION_MISMATCH",
    "ASSERTION_INVALID",
    "FIXTURE_SCHEMA",
    "CONFIG_PREREQUISITE",
    "RUNNER_ADAPTER",
    "SERVICE_UNAVAILABLE",
)
SYMPTOMS = (
    "输入符合公开业务约束，返回值与变更前公开行为不一致",
    "测试断言仍引用已移除的响应路径，服务返回符合公开文档",
    "fixture 输入缺少契约必需字段，校验器拒绝该输入",
    "环境配置缺少必要依赖，准备阶段健康检查失败",
    "runner 适配器无法解析有效的执行参数，目标请求尚未送达",
    "共享基础服务连接失败，独立健康探针同样失败",
)


class CaseCountDistribution(WireModel):
    count: int
    sum: int
    mean: int
    min: int
    max: int
    histogram: dict[str, int]


class RootBandAllocation(WireModel):
    top3: int
    next7: int
    long_tail: int


class StormProfile(WireModel):
    affected_percent: Literal[10, 30, 50]
    shared_hidden_root_count: int


class WorkloadManifest(WorkloadConfig):
    """离线受控 manifest schema；没有 Agent-visible endpoint。"""

    schema_version: Literal["stage13.workload-manifest.v1"]
    identity_encoding: Literal["stage13.v1/sha256-ordered-compact-json-array"]
    digest_algorithm: Literal["catalog-json-v1"]
    version_execution_count: int
    case_execution_count: int
    failing_execution_count: int
    failed_case_count: int
    error_case_count: int
    hidden_root_count: int
    hidden_root_distribution: dict[str, int]
    root_band_allocation: RootBandAllocation
    channel_distribution: dict[str, int]
    case_count_distribution: CaseCountDistribution
    failed_cases_per_failing_execution: dict[str, int]
    affected_environment_count: int
    storm_profile: StormProfile | None
    execution_plan_digest: Digest
    replay_policy: str
    clock_mode: Literal["LOGICAL_SIMULATION_TIME"]
    evidence_retention_policy: str
    manifest_digest: Digest


class HiddenGTManifest(WireModel):
    """evaluator-only source truth schema；不是 EvalOps Dataset/评分结果。"""

    schema_version: Literal["stage13.hidden-gt-manifest.v1"]
    source: Literal["SYNTHETIC_HIDDEN_GT"]
    provider_namespace_id: str
    workload_manifest_digest: Digest
    roots: dict[str, dict]
    failure_assignment_digest: Digest
    assignment_count: int = Field(ge=0)
    gt_manifest_digest: Digest


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    index: int
    environment_index: int
    ordinal: int
    case_count: int
    failed_cases: tuple[tuple[int, int], ...]  # case index → private root index
    error_cases: tuple[int, ...] = ()
    skipped_cases: tuple[int, ...] = ()
    duration_seconds: int = 7200

    def compact(self) -> dict:
        return {
            "index": self.index,
            "environment_index": self.environment_index,
            "ordinal": self.ordinal,
            "case_count": self.case_count,
            "failed_cases": [list(pair) for pair in self.failed_cases],
            "error_cases": list(self.error_cases),
            "skipped_cases": list(self.skipped_cases),
            "duration_seconds": self.duration_seconds,
        }

    @classmethod
    def restore(cls, value: dict) -> ExecutionPlan:
        return cls(
            **{
                **value,
                "failed_cases": tuple(tuple(pair) for pair in value["failed_cases"]),
                "error_cases": tuple(value["error_cases"]),
                "skipped_cases": tuple(value["skipped_cases"]),
            }
        )


def _allocate(total: int, count: int) -> list[int]:
    return [total // count + (index < total % count) for index in range(count)]


class Stage13Workload:
    """仅保留 execution 级小对象和异常 case 分配，不驻留全部 PASS case。"""

    def __init__(self, config: WorkloadConfig):
        self.config = WorkloadConfig.model_validate(config.model_dump(mode="json"))
        rng = random.Random(config.seed)
        total = config.environment_count * 3
        counts = []
        for index in range(total // 3):
            first, second = (
                (50, 50) if index == 0 else (rng.randint(50, 125), rng.randint(50, 125))
            )
            counts.extend((first, second, 300 - first - second))
        rng.shuffle(counts)
        assignments: dict[int, list[tuple[int, int]]] = {}
        storm_percent = {"storm-10": 10, "storm-30": 30, "storm-50": 50}.get(
            config.profile_id
        )
        if storm_percent is not None:
            affected = rng.sample(
                range(config.environment_count),
                config.environment_count * storm_percent // 100,
            )
            # 三个共享主因；一个 affected 环境的三个版本均包含有限失败。
            for environment_index in sorted(affected):
                for ordinal in range(3):
                    index = environment_index * 3 + ordinal
                    case_indices = rng.sample(range(counts[index]), 2 + ordinal)
                    assignments[index] = [
                        (case, environment_index % 3) for case in sorted(case_indices)
                    ]
            self.root_indices = (0, 1, 2)
        else:
            failing_count = total // 10
            # 各 channel 都保留 failure 槽，避免小 profile 的随机抽样失去长尾原因。
            candidates = [
                [
                    index
                    for index in range(total)
                    if (index // 3) % config.channel_group_count == group
                ]
                for group in range(config.channel_group_count)
            ]
            for group in candidates:
                rng.shuffle(group)
            selected = [
                candidates[position % config.channel_group_count].pop()
                for position in range(failing_count)
            ]
            failure_counts = [
                count for _ in range(failing_count // 5) for count in (1, 2, 3, 4, 15)
            ]
            group_slots = [
                [
                    index
                    for index in selected
                    if (index // 3) % config.channel_group_count == group
                ]
                for group in range(config.channel_group_count)
            ]
            group_totals = [0] * config.channel_group_count
            assigned_counts = {}
            for count in sorted(failure_counts, reverse=True):
                group = min(
                    (
                        group
                        for group in range(config.channel_group_count)
                        if group_slots[group]
                    ),
                    key=lambda group: (group_totals[group], group),
                )
                assigned_counts[group_slots[group].pop()] = count
                group_totals[group] += count
            slots = []
            for index in selected:
                count = assigned_counts[index]
                for case in sorted(rng.sample(range(counts[index]), count)):
                    slots.append((index, case))
            rng.shuffle(slots)
            failed_count = len(slots)
            top = failed_count * 55 // 100
            middle = failed_count * 28 // 100
            root_counts = (
                _allocate(top, 3)
                + _allocate(middle, 7)
                + _allocate(failed_count - top - middle, 14)
            )
            remaining = list(slots)
            # 长尾是 channel-specific；先分配，剩余槽给跨 channel 和 global 原因。
            for root_index in range(10, 24):
                group_index = root_index % config.channel_group_count
                matching = [
                    (index, case)
                    for index, case in remaining
                    if (index // 3) % config.channel_group_count == group_index
                ]
                chosen = matching[: root_counts[root_index]]
                if len(chosen) != root_counts[root_index]:
                    raise ValueError(
                        "所选 seed/config 没有足够 channel-specific 槽；请使用另一 seed"
                    )
                chosen_set = set(chosen)
                remaining = [slot for slot in remaining if slot not in chosen_set]
                for index, case in chosen:
                    assignments.setdefault(index, []).append((case, root_index))
            # cross-channel 原因覆盖一对 channel；global 原因覆盖全部 channel。
            for root_index in range(3, 10):
                first_group = (root_index - 3) * 2 % config.channel_group_count
                groups = {first_group, (first_group + 1) % config.channel_group_count}
                chosen = [
                    slot
                    for slot in remaining
                    if (slot[0] // 3) % config.channel_group_count in groups
                ][: root_counts[root_index]]
                if len(chosen) != root_counts[root_index]:
                    raise ValueError("cross-channel 槽不足")
                chosen_set = set(chosen)
                remaining = [slot for slot in remaining if slot not in chosen_set]
                for index, case in chosen:
                    assignments.setdefault(index, []).append((case, root_index))
            rng.shuffle(remaining)
            cursor = 0
            for root_index in range(3):
                for index, case in remaining[cursor : cursor + root_counts[root_index]]:
                    assignments.setdefault(index, []).append((case, root_index))
                cursor += root_counts[root_index]
            assert cursor == len(remaining)
            self.root_indices = tuple(range(24))
        self.plans = tuple(
            ExecutionPlan(
                index,
                index // 3,
                index % 3 + 1,
                counts[index],
                tuple(sorted(assignments.get(index, []))),
                duration_seconds=6600 + rng.randrange(1201),
            )
            for index in range(total)
        )
        self.manifest = self._build_manifest(storm_percent)

    def environment(self, index: int) -> dict:
        if not 0 <= index < self.config.environment_count:
            raise ValueError("environment index 越界")
        return {
            "environment_id": f"env-{index:04d}",
            "channel_group": f"channel-{index % self.config.channel_group_count:02d}",
            "automation_project_id": self.config.automation_project_id,
            "suite_id": self.config.suite_id,
        }

    def request(self, index: int, *, parameters: dict | None = None) -> SubmitRequest:
        plan = self.plans[index]
        env = self.environment(plan.environment_index)
        config = self.config
        guardian = business_key(
            "guardian",
            config.owner_scope_id,
            config.automation_project_id,
            config.suite_id,
            env["environment_id"],
        )
        cycle = business_key("cycle", guardian, config.business_date)
        version = config.product_versions[plan.ordinal - 1]
        intent = {
            "provider_namespace_id": config.provider_namespace_id,
            "owner_scope_id": config.owner_scope_id,
            **env,
            "cycle_key": cycle,
            "version_execution_key": business_key(
                "version", cycle, plan.ordinal, version
            ),
            "ordinal": plan.ordinal,
            "product_version": version,
            "parameters": parameters or {},
            "remote_execution_business_key": business_key(
                "remote",
                config.provider_namespace_id,
                config.owner_scope_id,
                config.automation_project_id,
                config.suite_id,
                env["environment_id"],
                cycle,
                plan.ordinal,
                version,
            ),
        }
        return SubmitRequest(**intent, request_digest=sha256(canonical_bytes(intent)))

    def plan_for(self, request: SubmitRequest) -> ExecutionPlan:
        prefix, separator, index_text = request.environment_id.partition("-")
        if prefix != "env" or not separator or not index_text.isdecimal():
            raise ValueError("受控环境 ID 无效")
        environment_index = int(index_text)
        if not 0 <= environment_index < self.config.environment_count:
            raise ValueError("受控环境超出 namespace 范围")
        index = environment_index * 3 + request.ordinal - 1
        expected = self.request(index, parameters=request.parameters)
        # cycle 是 consumer 的意图；Provider 不拥有 DailyCycle，也不替 caller 推进业务日。
        # 同 namespace 的另一合法 cycle 可以复用相同 frozen 环境/版本计划。
        cycle_fields = {
            "cycle_key",
            "version_execution_key",
            "remote_execution_business_key",
            "request_digest",
        }
        if expected.model_dump(exclude=cycle_fields) != request.model_dump(
            exclude=cycle_fields
        ):
            raise ValueError("请求不属于 namespace 的 frozen plan")
        return self.plans[index]

    @staticmethod
    def case_id(index: int) -> str:
        return f"case-{index:03d}"

    def case_result(self, plan: ExecutionPlan, index: int) -> dict:
        """可核查任意一个普通 CI case 身份/outcome；不输出隐藏标签。"""
        if not 0 <= index < plan.case_count:
            raise ValueError("case index 越界")
        failed = dict(plan.failed_cases)
        outcome = (
            "FAILED"
            if index in failed
            else (
                "ERROR"
                if index in plan.error_cases
                else "SKIPPED" if index in plan.skipped_cases else "PASS"
            )
        )
        return {"provider_case_id": self.case_id(index), "outcome": outcome}

    def visible_failure(self, plan: ExecutionPlan, case_index: int) -> dict:
        root_index = dict(plan.failed_cases).get(case_index)
        if root_index is None:
            if case_index not in plan.error_cases:
                raise ValueError("详情只能取 FAILED/ERROR case")
            return {
                **self.case_result(plan, case_index),
                "title": "合成执行阶段错误",
                "error_code": "CASE_EXECUTION_ERROR",
                "error_excerpt": "case harness 无法读取合成输入",
                "component": COMPONENTS[0],
                "visible_change_refs": [],
                "artifact_refs": [],
            }
        mechanism = root_index % len(MECHANISMS)
        insufficient = (
            self.config.profile_id == "insufficient-evidence" and root_index == 23
        )
        component = COMPONENTS[root_index // 3]
        artifact_mode = self.artifact_mode(plan.index, case_index)
        return {
            **self.case_result(plan, case_index),
            "title": f"{component} 合成业务契约检查",
            "error_code": (
                "OBSERVATION_INCOMPLETE" if insufficient else ERROR_CODES[mechanism]
            ),
            "error_excerpt": (
                "诊断摘录未生成，需要进一步取证"
                if insufficient
                else f"{SYMPTOMS[mechanism]}；组件 {component}；变更 {CHANGES[root_index]}"
            ),
            "component": component,
            "visible_change_refs": [] if insufficient else [CHANGES[root_index]],
            "artifact_refs": (
                []
                if artifact_mode == "ABSENT"
                else [f"artifact:{self.case_id(case_index)}"]
            ),
        }

    def artifact_mode(self, execution_index: int, case_index: int) -> str:
        number = int(
            business_key(
                "artifact-mode", self.config.seed, execution_index, case_index
            )[:8],
            16,
        )
        return ("AVAILABLE", "ABSENT", "UNAVAILABLE", "DELAYED")[number % 4]

    def root_truth(self, root_index: int) -> dict:
        mechanism = root_index % 6
        unknown = self.config.profile_id == "insufficient-evidence" and root_index == 23
        return {
            "schema_version": "stage13.triage-ground-truth.v1",
            "ExpectedFailureCategory": "UNKNOWN" if unknown else CATEGORIES[mechanism],
            "RelevantRootCauses": [
                {
                    "root_cause_id": f"hidden-root-{root_index + 1:02d}",
                    "relevance": 3,
                    "acceptable_descriptors": [
                        {
                            "component_id": COMPONENTS[root_index // 3],
                            "mechanism_code": (
                                "UNKNOWN" if unknown else MECHANISMS[mechanism]
                            ),
                            "change_ref": None if unknown else CHANGES[root_index],
                        }
                    ],
                }
            ],
            "AcceptableActions": [
                "REQUEST_MORE_EVIDENCE" if unknown else ACTIONS[mechanism]
            ],
            "ExpectedTicketDecision": (
                "REQUEST_MORE_EVIDENCE"
                if unknown
                else "CREATE_PRODUCT_TICKET" if mechanism == 0 else "IGNORE"
            ),
            "Criticality": "CRITICAL" if mechanism in (0, 3, 4) else "NORMAL",
            "EvidencePolicy": {
                "expected_decidable": not unknown,
                "required_evidence_types": ["FAILURE_DETAIL", "CHANGE_METADATA"],
                "missing_behavior": "BLOCKED",
            },
            "correlation_scope": (
                "GLOBAL"
                if root_index < 3
                else "CROSS_CHANNEL" if root_index < 10 else "CHANNEL_SPECIFIC"
            ),
        }

    def _build_manifest(self, storm_percent) -> dict:
        roots = Counter(root for plan in self.plans for _, root in plan.failed_cases)
        counts = Counter(plan.case_count for plan in self.plans)
        failed_distribution = Counter(
            len(plan.failed_cases) for plan in self.plans if plan.failed_cases
        )
        body = {
            "schema_version": "stage13.workload-manifest.v1",
            **self.config.model_dump(mode="json"),
            "identity_encoding": "stage13.v1/sha256-ordered-compact-json-array",
            "digest_algorithm": "catalog-json-v1",
            "environment_count": self.config.environment_count,
            "version_execution_count": len(self.plans),
            "case_execution_count": sum(plan.case_count for plan in self.plans),
            "failing_execution_count": sum(
                bool(plan.failed_cases) for plan in self.plans
            ),
            "failed_case_count": sum(roots.values()),
            "error_case_count": 0,
            "hidden_root_count": len(roots),
            "hidden_root_distribution": {
                f"hidden-root-{root + 1:02d}": count
                for root, count in sorted(roots.items())
            },
            "root_band_allocation": {
                "top3": sum(roots[root] for root in range(3)),
                "next7": sum(roots[root] for root in range(3, 10)),
                "long_tail": sum(roots[root] for root in range(10, 24)),
            },
            "channel_distribution": dict(
                Counter(
                    self.environment(index)["channel_group"]
                    for index in range(self.config.environment_count)
                )
            ),
            "case_count_distribution": {
                "count": len(self.plans),
                "sum": sum(plan.case_count for plan in self.plans),
                "mean": sum(plan.case_count for plan in self.plans) // len(self.plans),
                "min": min(counts),
                "max": max(counts),
                "histogram": {
                    str(count): number for count, number in sorted(counts.items())
                },
            },
            "failed_cases_per_failing_execution": {
                str(count): number
                for count, number in sorted(failed_distribution.items())
            },
            "affected_environment_count": len(
                {plan.environment_index for plan in self.plans if plan.failed_cases}
            ),
            "storm_profile": (
                None
                if storm_percent is None
                else {
                    "affected_percent": storm_percent,
                    "shared_hidden_root_count": len(roots),
                }
            ),
            "execution_plan_digest": sha256(
                canonical_bytes([plan.compact() for plan in self.plans])
            ),
            "replay_policy": "same intent reuses namespace; new seed/profile/controlled run requires a new namespace",
            "clock_mode": "LOGICAL_SIMULATION_TIME",
            "evidence_retention_policy": "namespace last remote completion + 7 logical days; access policy, no physical purge",
        }
        return WorkloadManifest.model_validate(
            {**body, "manifest_digest": sha256(canonical_bytes(body))}
        ).model_dump(mode="json")

    def hidden_gt_manifest(self) -> dict:
        body = {
            "schema_version": "stage13.hidden-gt-manifest.v1",
            "source": "SYNTHETIC_HIDDEN_GT",
            "provider_namespace_id": self.config.provider_namespace_id,
            "workload_manifest_digest": self.manifest["manifest_digest"],
            "roots": {
                f"hidden-root-{index + 1:02d}": self.root_truth(index)
                for index in self.root_indices
            },
            "failure_assignment_digest": sha256(
                canonical_bytes(
                    [
                        [plan.index, list(pair)]
                        for plan in self.plans
                        for pair in plan.failed_cases
                    ]
                )
            ),
            "assignment_count": self.manifest["failed_case_count"],
        }
        return HiddenGTManifest.model_validate(
            {**body, "gt_manifest_digest": sha256(canonical_bytes(body))}
        ).model_dump(mode="json")

    def export_hidden_gt(self, directory: Path) -> dict:
        """离线 operator 命令入口；绝不注册到 HTTP/Agent Tool。"""
        directory.mkdir(parents=True, exist_ok=True)
        gt = self.hidden_gt_manifest()
        (directory / "workload-manifest.json").write_bytes(
            canonical_bytes(self.manifest)
        )
        (directory / "hidden-gt-manifest.json").write_bytes(canonical_bytes(gt))
        with (directory / "hidden-failure-assignments.jsonl").open("wb") as stream:
            for plan in self.plans:
                if not plan.failed_cases:
                    continue
                request = self.request(plan.index)
                for case_index, root_index in plan.failed_cases:
                    stream.write(
                        canonical_bytes(
                            {
                                "remote_execution_business_key": request.remote_execution_business_key,
                                "provider_case_id": self.case_id(case_index),
                                "root_cause_id": f"hidden-root-{root_index + 1:02d}",
                            }
                        )
                        + b"\n"
                    )
        return gt
