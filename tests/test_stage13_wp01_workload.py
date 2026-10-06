"""WP01 合同、完整 900k 模型与 hidden/visible 隔离验证。"""

from collections import Counter
import hashlib
import json

import pytest
from pydantic import ValidationError

from core.stage13.contracts import (
    DetailRequest,
    SubmitRequest,
    WorkloadConfig,
    business_key,
    canonical_bytes,
    sha256,
)
from core.stage13.workload import CHANGES, COMPONENTS, Stage13Workload

FORBIDDEN_FIELDS = frozenset(
    {
        "hidden_root_id",
        "root_cause_id",
        "ExpectedFailureCategory",
        "RelevantRootCauses",
        "AcceptableActions",
        "ExpectedTicketDecision",
        "Criticality",
        "expected_action",
        "criticality",
        "hidden_root_distribution",
    }
)


def assert_agent_visible(value):
    if isinstance(value, dict):
        assert not (set(value) & FORBIDDEN_FIELDS)
        for child in value.values():
            assert_agent_visible(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            assert_agent_visible(child)
    elif isinstance(value, str):
        assert "hidden-root-" not in value
        assert value not in {"CRITICAL", "NORMAL", "SYNTHETIC_HIDDEN_GT"}


def test_wp00_key_encoding_and_strict_binding():
    expected = hashlib.sha256(
        '["stage13.v1","remote","Name",1,null,["症状"]]'.encode("utf-8")
    ).hexdigest()
    assert business_key("remote", "Name", 1, None, ["症状"]) == expected
    assert business_key("remote", "Name") != business_key("remote", "name")
    with pytest.raises(ValueError):
        business_key("remote", True)
    # catalog-json-v1 的整数资产子集与 Go writer 同样不 escape Unicode/HTML/U+2028。
    assert canonical_bytes(
        {"z": "<&\u2028症状", "a": [1, None, True]}
    ) == '{"a":[1,null,true],"z":"<&\u2028症状"}'.encode("utf-8")
    with pytest.raises(ValueError):
        canonical_bytes({"float": 1.0})
    workload = Stage13Workload(
        WorkloadConfig(provider_namespace_id="binding", environment_count=100)
    )
    valid = workload.request(0).model_dump(mode="json")
    for field, replacement in (
        ("request_digest", "0" * 64),
        ("remote_execution_business_key", "0" * 64),
        ("version_execution_key", "0" * 64),
    ):
        with pytest.raises(ValidationError):
            SubmitRequest.model_validate({**valid, field: replacement})
    with pytest.raises(ValidationError):
        DetailRequest(
            remote_execution_id="00000000-0000-0000-0000-000000000000",
            remote_execution_business_key="a" * 64,
            request_digest="b" * 64,
            page_size=51,
        )


def test_normal_full_profile_counts_and_every_case_reconstruction():
    workload = Stage13Workload(WorkloadConfig(provider_namespace_id="normal-full"))
    manifest = workload.manifest
    assert [
        manifest[field]
        for field in (
            "environment_count",
            "version_execution_count",
            "case_execution_count",
            "failing_execution_count",
            "failed_case_count",
            "hidden_root_count",
        )
    ] == [3000, 9000, 900000, 900, 4500, 24]
    assert manifest["root_band_allocation"] == {
        "top3": 2475,
        "next7": 1260,
        "long_tail": 765,
    }
    counts, roots, identities = Counter(), Counter(), set()
    for plan in workload.plans:
        request = workload.request(plan.index)
        identities.add(request.remote_execution_business_key)
        assert 50 <= plan.case_count <= 200
        case_ids = set()
        for index in range(plan.case_count):
            result = workload.case_result(plan, index)
            case_ids.add(result["provider_case_id"])
            counts[result["outcome"]] += 1
        assert len(case_ids) == plan.case_count
        roots.update(root for _, root in plan.failed_cases)
    assert len(identities) == 9000
    assert counts == {"PASS": 895500, "FAILED": 4500}
    assert len(roots) == 24 and sum(roots.values()) == 4500
    assert manifest["case_count_distribution"]["mean"] == 100
    assert manifest["case_count_distribution"]["min"] == 50
    assert manifest["case_count_distribution"]["max"] == 200
    assert len(manifest["failed_cases_per_failing_execution"]) > 1
    assert len(manifest["channel_distribution"]) == 12
    assert sum(manifest["channel_distribution"].values()) == 3000


def test_deterministic_manifest_truth_evidence_and_new_seed(tmp_path):
    config = WorkloadConfig(provider_namespace_id="replay")
    first, second = Stage13Workload(config), Stage13Workload(config)
    assert first.manifest == second.manifest
    assert first.plans == second.plans
    assert first.hidden_gt_manifest() == second.hidden_gt_manifest()
    for plan in first.plans:
        assert first.request(plan.index) == second.request(plan.index)
        for case_index, _ in plan.failed_cases:
            assert first.visible_failure(plan, case_index) == second.visible_failure(
                second.plans[plan.index], case_index
            )
    other = Stage13Workload(
        config.model_copy(
            update={"seed": config.seed + 1, "provider_namespace_id": "new-seed"}
        )
    )
    assert first.manifest["manifest_digest"] != other.manifest["manifest_digest"]
    assert set(first.manifest) == set(other.manifest)
    gt = first.export_hidden_gt(tmp_path)
    assert (
        sha256(
            canonical_bytes({k: v for k, v in gt.items() if k != "gt_manifest_digest"})
        )
        == gt["gt_manifest_digest"]
    )
    assert (
        sum(
            1
            for _ in (tmp_path / "hidden-failure-assignments.jsonl").open(
                encoding="utf-8"
            )
        )
        == 4500
    )


def test_visible_schemas_and_all_failure_values_exclude_hidden_gt():
    from core.stage13.contracts import (
        ArtifactContent,
        CISummary,
        EvidencePacket,
        FailurePage,
    )

    for model in (CISummary, FailurePage, ArtifactContent, EvidencePacket):
        assert_agent_visible(model.model_json_schema())
        assert model.model_config["extra"] == "forbid"
    workload = Stage13Workload(WorkloadConfig(provider_namespace_id="boundary"))
    root_channels = {}
    for plan in workload.plans:
        for case_index, root in plan.failed_cases:
            visible = workload.visible_failure(plan, case_index)
            assert_agent_visible(json.loads(canonical_bytes(visible)))
            assert visible["component"] in COMPONENTS
            assert set(visible["visible_change_refs"]) <= set(CHANGES)
            root_channels.setdefault(root, set()).add(
                workload.environment(plan.environment_index)["channel_group"]
            )
    assert all(len(root_channels[root]) == 1 for root in range(10, 24))
    assert all(len(root_channels[root]) == 12 for root in range(3))
    assert all(len(root_channels[root]) == 2 for root in range(3, 10))
    truths = [workload.root_truth(root) for root in workload.root_indices]
    assert all(truth["EvidencePolicy"]["expected_decidable"] for truth in truths)
    assert {
        truth["ExpectedFailureCategory"]: truth["ExpectedTicketDecision"]
        for truth in truths
    }["ENVIRONMENT"] == "IGNORE"
    assert {
        truth["ExpectedFailureCategory"]: truth["ExpectedTicketDecision"]
        for truth in truths
    }["TOOL_CHAIN"] == "IGNORE"
    assert {
        truth["ExpectedFailureCategory"]: truth["ExpectedTicketDecision"]
        for truth in truths
    }["PRODUCT"] == "CREATE_PRODUCT_TICKET"
    aliases = [
        tuple(descriptor.values())
        for truth in truths
        for root in truth["RelevantRootCauses"]
        for descriptor in root["acceptable_descriptors"]
    ]
    assert len(aliases) == len(set(aliases)) == 24


@pytest.mark.parametrize(
    "profile,affected", [("storm-10", 300), ("storm-30", 900), ("storm-50", 1500)]
)
def test_storm_shared_causes_and_repeated_visible_signature(profile, affected):
    config = WorkloadConfig(provider_namespace_id=profile, profile_id=profile)
    first, replay = Stage13Workload(config), Stage13Workload(config)
    assert first.manifest == replay.manifest
    assert first.manifest["affected_environment_count"] == affected
    assert first.manifest["hidden_root_count"] == 3
    assert first.manifest["failing_execution_count"] == affected * 3
    assert first.manifest["case_execution_count"] == 900000
    signatures = Counter()
    for plan in first.plans:
        assert len(plan.failed_cases) < plan.case_count
        for case, _ in plan.failed_cases:
            visible = first.visible_failure(plan, case)
            signatures[
                (visible["error_code"], visible["error_excerpt"], visible["component"])
            ] += 1
    assert len(signatures) == 3
    assert min(signatures.values()) > 1


def test_insufficient_evidence_is_an_explicit_separate_profile():
    workload = Stage13Workload(
        WorkloadConfig(
            provider_namespace_id="insufficient",
            profile_id="insufficient-evidence",
            environment_count=100,
        )
    )
    truth = workload.root_truth(23)
    assert truth["ExpectedFailureCategory"] == "UNKNOWN"
    assert truth["EvidencePolicy"]["expected_decidable"] is False
    assert truth["AcceptableActions"] == ["REQUEST_MORE_EVIDENCE"]
    plan = next(
        plan
        for plan in workload.plans
        if any(root == 23 for _, root in plan.failed_cases)
    )
    case_index = next(index for index, root in plan.failed_cases if root == 23)
    assert workload.visible_failure(plan, case_index)["visible_change_refs"] == []
