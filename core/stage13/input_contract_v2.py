"""责任输入v2：严格结构、授权来源及纯显式关系投影，不消费GT值。"""

from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re

from jsonschema import Draft202012Validator, ValidationError

INPUT_V2 = "stage13.triage-input.v2"
RESPONSIBILITY_V2 = "stage13.root-responsibility.v2"
PROJECTION_V1 = "stage13.triage-relation-projection.v1"
EXECUTION_V2 = "stage13.attempt-execution.v2"
RECEIPT_V2 = "localagent-ci-triage-receipt.v2"
COMPARABILITY_V3 = "stage13.model-comparability.v3"
FILES = Path(__file__).with_name("contracts_v2")
SCHEMA = json.loads((FILES / "triage-input-v2.schema.json").read_text(encoding="utf-8"))
IDENTITIES = json.loads((FILES / "identities.json").read_text(encoding="utf-8"))
DOMAIN = json.loads((FILES / "comparison-domain.json").read_text(encoding="utf-8"))
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,127}$")
FORBIDDEN = {
    "expected_component",
    "root_component",
    "ground_truth_owner",
    "ground_truth",
    "relevantrootcauses",
    "expectedfailurecategory",
    "acceptableactions",
    "expectedticketdecision",
    "criticality",
    "owner_component_id",
    "acceptable_owner_alternatives",
    "root_asset_or_operation",
    "review_decision",
    "responsibility_review",
    "hidden_root_id",
    "expected_descriptor",
    "selected_root",
    "selected_root_target",
    "faulty",
}


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _verify_assets():
    expected = {
        "responsibility-semantics.json": "2a458f1ee3038f2c807fb514568e877eee63d32436032717dffb6323c03f7be7",
        "business-relation-contract.json": "10267ebd263e40f1da4bb9e2417c0239e29dc9bdf3c94a75ed5e61fdbd924455",
        "input-contract-v2.json": "3d880f9a6d3ffcf35ee6d84143f704161a516a22591fc3d94d950e5dd3ba682c",
        "projection-contract.json": "0caf84bffd1cbdffa48a0bc7facb84375a2ace6a16b164316422f4a87555dd4e",
    }
    definitions = {
        n: json.loads((FILES / n).read_text(encoding="utf-8")) for n in expected
    }
    if any(digest(definitions[n]) != d for n, d in expected.items()):
        raise ValueError("FROZEN_CONTRACT_DRIFT")
    if definitions["input-contract-v2.json"]["schema"] != SCHEMA or definitions[
        "business-relation-contract.json"
    ]["schema"] != json.loads(
        (FILES / "business-relations.schema.json").read_text(encoding="utf-8")
    ):
        raise ValueError("FROZEN_SCHEMA_DRIFT")
    if (
        IDENTITIES
        != {
            "input_contract_version": INPUT_V2,
            "input_contract_digest": expected["input-contract-v2.json"],
            "responsibility_contract_version": RESPONSIBILITY_V2,
            "responsibility_contract_digest": expected["responsibility-semantics.json"],
            "projection_version": PROJECTION_V1,
            "projection_digest": expected["projection-contract.json"],
        }
        or any(DOMAIN.get(k) != v for k, v in IDENTITIES.items())
        or digest(DOMAIN)
        != "1093a3aaf0636a18b7d0604751b2546230289d9eaf39e11702a18f367a56c92f"
    ):
        raise ValueError("FROZEN_IDENTITY_DRIFT")


_verify_assets()


def contract_identities():
    return deepcopy(IDENTITIES)


def comparison_domain():
    return deepcopy(DOMAIN)


def strict_load(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    return json.loads(
        raw,
        object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("NON_FINITE_JSON")),
    )


def reject_gt(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in FORBIDDEN:
                raise ValueError("GT_INPUT_ISOLATION_VIOLATION")
            reject_gt(child)
    elif isinstance(value, list):
        for child in value:
            reject_gt(child)
    elif isinstance(value, str):
        if (
            "hidden-root-" in value
            or "hidden-eval-root-" in value
            or any(
                re.search(r'"' + re.escape(key) + r'"\s*:', value, re.IGNORECASE)
                for key in FORBIDDEN
            )
        ):
            raise ValueError("GT_INPUT_ISOLATION_VIOLATION")
        try:
            parsed = strict_load(value)
        except (ValueError, TypeError):
            return
        if isinstance(parsed, (dict, list)):
            reject_gt(parsed)


def component_order(component_ids):
    """排序只接收授权组件集合；无owner/fault/GT参数。"""
    if not all(isinstance(v, str) and SAFE_ID.fullmatch(v) for v in component_ids):
        raise ValueError("COMPONENT_ID_INVALID")
    return sorted(set(component_ids))


def _source_sets(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {
                "component_ids",
                "component_scope",
                "visible_component_inventory",
            }:
                if not isinstance(child, list) or child != component_order(child):
                    raise ValueError("SOURCE_COMPONENT_ORDER_INVALID")
            _source_sets(child)
    elif isinstance(value, list):
        for child in value:
            _source_sets(child)


def _entity(value):
    return value["kind"], value["id"]


def _relation_order(value):
    return (
        value["relation_type"],
        *_entity(value["subject"]),
        *_entity(value["object"]),
        value["relation_id"],
    )


def _pointer(value, pointer):
    for token in pointer[1:].split("/"):
        if re.search(r"~(?![01])", token):
            raise ValueError("RELATION_POINTER_INVALID")
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            value = value[token]
        elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", token):
            value = value[int(token)]
        else:
            raise ValueError("RELATION_POINTER_INVALID")
    return value


def validate_input_v2(payload, *, scope_id=None, canonical_order=True):
    """Source校验不能用模型声明或文件存在替代；缺边保持缺边。"""
    try:
        Draft202012Validator(SCHEMA).validate(payload)
    except ValidationError as exc:
        raise ValueError("INPUT_CONTRACT_INVALID") from exc
    if len(canonical(payload)) > 131072:
        raise ValueError("INPUT_BUDGET_EXCEEDED")
    reject_gt(payload)
    evidence = {}
    seen = set()
    for source in payload["visible_evidence"]:
        reference = {k: source[k] for k in ("evidence_id", "digest", "type")}
        if (
            not isinstance(reference["evidence_id"], str)
            or not SAFE_ID.fullmatch(reference["evidence_id"])
            or not isinstance(reference["digest"], str)
            or not re.fullmatch("[0-9a-f]{64}", reference["digest"])
            or reference["type"]
            not in SCHEMA["properties"]["evidence_policy"]["properties"][
                "allowed_types"
            ]["items"]["enum"]
            or source["evidence_id"] in seen
            or source["availability"] not in {"AVAILABLE", "MISSING", "TRUNCATED"}
        ):
            raise ValueError("EVIDENCE_METADATA_INVALID")
        seen.add(source["evidence_id"])
        if (
            "owner_scope_id" in source
            and scope_id is not None
            and source["owner_scope_id"] != scope_id
        ):
            raise ValueError("EVIDENCE_SCOPE_DENIED")
        if source["availability"] != "AVAILABLE":
            continue
        content = source.get("content")
        if content is None:
            raise ValueError("EVIDENCE_NOT_AVAILABLE")
        raw = (
            content.encode("utf-8") if isinstance(content, str) else canonical(content)
        )
        if hashlib.sha256(raw).hexdigest() != source["digest"]:
            raise ValueError("EVIDENCE_DIGEST_MISMATCH")
        if isinstance(content, str):
            try:
                content = strict_load(content)
            except ValueError:
                pass
        _source_sets(content)
        if (
            isinstance(content, dict)
            and "scope" in content
            and content["scope"] != payload["scope"]
        ):
            raise ValueError("EVIDENCE_SCOPE_DENIED")
        if (
            isinstance(content, dict)
            and content.get("schema_version") == "stage13.business-world.v1"
            and (
                content.get("source_id")
                != payload["business_context"]["snapshot_ref"]["source_id"]
                or content.get("revision")
                != payload["business_context"]["snapshot_ref"]["revision"]
                or source["digest"]
                != payload["business_context"]["snapshot_ref"]["digest"]
            )
        ):
            raise ValueError("BUSINESS_SNAPSHOT_MISMATCH")
        evidence[source["evidence_id"]] = (reference, content)
    for reference in payload.get("EvidenceRefs", []):
        if reference != evidence.get(reference["evidence_id"], (None,))[0]:
            raise ValueError("EVIDENCE_REFERENCE_MISMATCH")
    business = payload["business_context"]
    entities = {_entity(e) for e in business["entities"]}
    if len(entities) != len(business["entities"]):
        raise ValueError("DUPLICATE_ENTITY")
    components = payload["failure_summary"]["component_scope"]
    changes = payload["visible_change_inventory"]
    for kind, entity_id in entities:
        if (
            kind == "COMPONENT"
            and entity_id not in components
            or kind == "CHANGE"
            and entity_id not in changes
        ):
            raise ValueError("ENTITY_SCOPE_DENIED")
    ids, edges, ownership = set(), set(), {}
    for relation in business["relations"]:
        edge = (
            relation["relation_type"],
            _entity(relation["subject"]),
            _entity(relation["object"]),
        )
        if (
            relation["relation_id"] in ids
            or edge in edges
            or not all(_entity(relation[k]) in entities for k in ("subject", "object"))
        ):
            raise ValueError("RELATION_ENDPOINT_OR_DUPLICATE_INVALID")
        ids.add(relation["relation_id"])
        edges.add(edge)
        reference = relation["source_evidence_ref"]
        supplied, content = evidence.get(reference["evidence_id"], (None, None))
        if supplied != reference:
            raise ValueError("RELATION_SOURCE_INVALID")
        try:
            fact = _pointer(content, relation["source_pointer"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValueError("RELATION_POINTER_INVALID") from exc
        fields = {"relation_type", "subject", "object", "ownership_mode"}
        if (
            not isinstance(fact, dict)
            or set(fact) - (fields | {"relation_id"})
            or {k: fact[k] for k in fields if k in fact}
            != {k: relation[k] for k in fields if k in relation}
            or "relation_id" in fact
            and fact["relation_id"] != relation["relation_id"]
        ):
            raise ValueError("RELATION_PROVENANCE_MISMATCH")
        if relation["relation_type"] == "OWNS":
            ownership.setdefault(_entity(relation["object"]), []).append(relation)
    for owners in ownership.values():
        if len(owners) > 1 and any(r["ownership_mode"] != "EQUIVALENT" for r in owners):
            raise ValueError("CONFLICTING_EXCLUSIVE_OWNERS")
    if canonical_order and (
        components != component_order(components)
        or changes != component_order(changes)
        or business["entities"] != sorted(business["entities"], key=_entity)
        or business["relations"] != sorted(business["relations"], key=_relation_order)
    ):
        raise ValueError("INPUT_CANONICAL_ORDER_INVALID")
    return payload


def project_input_v2(source):
    validate_input_v2(source, canonical_order=False)
    result = deepcopy(source)
    result["failure_summary"]["component_scope"] = component_order(
        result["failure_summary"]["component_scope"]
    )
    result["visible_change_inventory"] = component_order(
        result["visible_change_inventory"]
    )
    business = result["business_context"]
    business["entities"] = sorted(business["entities"], key=_entity)
    business["relations"] = sorted(business["relations"], key=_relation_order)
    return result


def materialize_input_v2(source):
    """仅物化已显式提供的新v2输入；不迁移旧Case或补维护事实。"""
    return canonical(project_input_v2(source)).decode("utf-8")


def validate_contract_identity(value):
    if any(value.get(k) != v for k, v in IDENTITIES.items()):
        raise ValueError("INPUT_CONTRACT_IDENTITY_MISMATCH")


def receipt_contract_fields(manifest, policy):
    validate_contract_identity(manifest)
    validate_contract_identity(policy)
    keys = (
        "source_input_digest",
        "source_semantic_input_digest",
        "projected_input_digest",
        "semantic_input_digest",
        "comparison_domain_digest",
    )
    if (
        policy.get("version") != EXECUTION_V2
        or policy.get("comparison_domain_digest") != digest(DOMAIN)
        or policy.get("semantic_input_digest") != policy.get("projected_input_digest")
        or policy.get("source_semantic_input_digest")
        != policy.get("projected_input_digest")
        or any(
            not isinstance(policy.get(k), str)
            or not re.fullmatch("[0-9a-f]{64}", policy[k])
            for k in keys
        )
    ):
        raise ValueError("RECEIPT_CONTRACT_BINDING_MISMATCH")
    return {
        "receipt_version": RECEIPT_V2,
        **contract_identities(),
        **{k: policy[k] for k in keys},
    }


def validate_receipt_v2(receipt):
    if not isinstance(receipt, dict) or receipt.get("receipt_version") != RECEIPT_V2:
        raise ValueError("RECEIPT_VERSION_MISMATCH")
    expected = receipt_contract_fields(
        receipt["actual_subject_manifest"], receipt["execution_policy"]
    )
    for key in (
        "actual_input_digest",
        "effective_payload_digest",
        "prompt_template_digest",
        "resolved_toolset_identity",
        "final_answer_digest",
    ):
        if not isinstance(receipt.get(key), str) or not re.fullmatch(
            "[0-9a-f]{64}", receipt[key]
        ):
            raise ValueError("RECEIPT_ACTUAL_BINDING_MISSING")
    calls = receipt.get("model_call_receipts")
    if (
        not isinstance(calls, list)
        or len(calls) > 1
        or any(
            not re.fullmatch("[0-9a-f]{64}", call.get(key, ""))
            for call in calls
            for key in ("effective_messages_digest", "effective_system_prompt_digest")
        )
    ):
        raise ValueError("RECEIPT_MESSAGES_BINDING_INVALID")
    policy = receipt["execution_policy"]
    start = datetime.fromisoformat(policy["execution_started_at"])
    deadline = datetime.fromisoformat(policy["execution_deadline_at"])
    if (
        policy["timeout_seconds"] != 180
        or start.tzinfo is None
        or deadline.tzinfo is None
        or (deadline - start).total_seconds() != 180
    ):
        raise ValueError("RECEIPT_EXECUTION_BUDGET_INVALID")
    if (
        any(receipt.get(k) != v for k, v in expected.items())
        or digest({k: v for k, v in receipt.items() if k != "receipt_digest"})
        != receipt.get("receipt_digest")
        or not re.fullmatch("[0-9a-f]{64}", receipt.get("execution_request_digest", ""))
    ):
        raise ValueError("RECEIPT_CONTRACT_BINDING_MISMATCH")
    return receipt
