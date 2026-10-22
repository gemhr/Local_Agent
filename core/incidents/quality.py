"""本人模拟事实导出与冻结观察重放；不修改业务状态，不执行模型。"""
import base64
import hashlib
import inspect
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, text
from core.ci_simulation.models import CIExecutionRow, ScenarioInstanceRow, NightlyVersionExecutionRow
from core.ci_simulation.execution import case_catalog
from core.incidents.models import IncidentRow, IncidentActionRow, IncidentVerificationRow
from core.incidents.service import require_human
from core.incidents.contracts import CIError, QualityRecord, QualityRepair
from pydantic import ValidationError
from core.incidents import grouping
from core.runtime.snapshot_serialization import canonical_json, sha256_digest, to_primitive

ALGORITHM = "localagent-canonical-json.v1"
RULES = (grouping.RULE, grouping.OFFLINE_RULE)


def definition_digest(version):
    if version not in RULES:
        raise CIError("CI_QUALITY_RULE_UNSUPPORTED", 422)
    return sha256_digest(dict(version=version, supported_products=["product-2.0", "product-2.1", "product-2.1.1"],
        signature=inspect.getsource(grouping.signature), pair=inspect.getsource(grouping.pair_candidate),
        records=inspect.getsource(check_records), repair=inspect.getsource(check_repair)))


def freeze_fact(body):
    raw = canonical_json(jsonable_encoder(body)).encode("utf-8")
    return dict(algorithm=ALGORITHM, canonical_bytes_b64=base64.b64encode(raw).decode("ascii"), sha256=hashlib.sha256(raw).hexdigest())


def unpack(fact, allowed):
    try:
        raw = base64.b64decode(fact.canonical_bytes_b64, validate=True)
        body = json.loads(raw)
        if (hashlib.sha256(raw).hexdigest() != fact.sha256 or canonical_json(body).encode("utf-8") != raw
                or not isinstance(body, dict) or set(body) != allowed or has_private_fields(body)):
            raise ValueError()
        (QualityRecord if allowed == RECORD_KEYS else QualityRepair).model_validate(body)
        return body
    except (ValueError, TypeError, UnicodeError, ValidationError):
        raise CIError("CI_QUALITY_FROZEN_FACT_INVALID", 422) from None


def has_private_fields(value):
    """来源字节也不能成为夹带参考标签或内部状态的通道。"""
    forbidden = {"groundtruth", "ground_truth", "expectedoutput", "expected_output", "assertions", "reference", "reference_label",
        "scenario_key", "profile_key", "current_state", "expected_root_cause", "jwt", "token", "api_key", "model_parameters"}
    if isinstance(value, dict):
        return any(k.lower() in forbidden or has_private_fields(v) for k, v in value.items())
    return isinstance(value, list) and any(has_private_fields(v) for v in value)


RECORD_KEYS = {"execution_id", "instance_id", "owner_user_id", "tenant_id", "environment_id", "case_id", "feature_id",
    "state_revision", "outcome", "started_at", "completed_at", "summary", "evidence_manifest", "evidence_digest",
    "nightly_version_execution_id", "business_completed_at", "incident_id"}
REPAIR_KEYS = {"incident_id", "execution_id", "instance_id", "owner_user_id", "status", "state_revision", "required_case_ids",
    "actions", "verifications", "closed_at", "closed_by_user_id", "closure"}


async def export_facts(service, principal, body):
    require_human(principal)
    async with service.database.session() as session:
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        executions = set(body.execution_ids)
        incidents, repair = [], None
        for incident_id in body.incident_ids:
            incident, origin, instance = await service.resolution_scope(session, principal, incident_id)
            incidents.append(incident)
            executions.add(origin.execution_id)
            if body.include_repair:
                if repair is not None:
                    raise CIError("CI_QUALITY_ONE_REPAIR_REQUIRED", 422)
                actions = (await session.execute(select(IncidentActionRow).where(IncidentActionRow.incident_id == incident_id).order_by(IncidentActionRow.created_at))).scalars().all()
                verifications = (await session.execute(select(IncidentVerificationRow).where(IncidentVerificationRow.incident_id == incident_id).order_by(IncidentVerificationRow.completed_at))).scalars().all()
                action_keys = ("action_id", "incident_id", "source_kind", "status", "created_by_user_id", "created_at", "base_state_revision", "confirmed_by_user_id", "confirmed_at", "applied_state_revision")
                verification_keys = ("verification_id", "incident_id", "instance_id", "mode", "state_revision", "requested_case_ids", "items", "created_by_user_id", "completed_at")
                for verification in verifications:
                    executions.update(i["execution_id"] for i in verification.items)
                closure = incident.closure_payload
                if closure:
                    closure = {k: closure[k] for k in ("source_kind", "state_revision", "action_id", "single_verification_id", "group_verification_id")}
                    closure["regression_confirmed"] = incident.closure_payload["confirmation"]["regression_confirmed"]
                repair = freeze_fact(dict(incident_id=incident_id, execution_id=origin.execution_id, instance_id=instance.instance_id,
                    owner_user_id=str(incident.owner_user_id), status=incident.status, state_revision=instance.state_revision,
                    required_case_ids=[c["case_id"] for c in case_catalog(instance.current_state)],
                    actions=[{k: getattr(a, k) for k in action_keys} for a in actions],
                    verifications=[{k: getattr(v, k) for k in verification_keys} for v in verifications],
                    closed_at=incident.closed_at, closed_by_user_id=incident.closed_by_user_id, closure=closure))
        if not executions or len(executions) > 36 or (body.include_repair and repair is None):
            raise CIError("CI_QUALITY_EXPORT_SELECTION_INVALID", 422)
        records, sources = [], []
        for execution_id in sorted(executions):
            execution = await session.get(CIExecutionRow, execution_id)
            instance = await session.get(ScenarioInstanceRow, execution.instance_id) if execution else None
            if instance is None or instance.tenant_id != principal.tenant_id or instance.owner_user_id != principal.user_id:
                raise CIError("CI_NOT_FOUND", 404)
            version = await session.get(NightlyVersionExecutionRow, execution.nightly_version_execution_id) if execution.nightly_version_execution_id else None
            if body.batch_id is not None and (version is None or version.batch_id != body.batch_id):
                raise CIError("CI_NOT_FOUND", 404)
            incident = (await session.execute(select(IncidentRow).where(IncidentRow.execution_id == execution_id))).scalar_one_or_none()
            record = {k: getattr(execution, k) for k in RECORD_KEYS - {"owner_user_id", "tenant_id", "incident_id"}}
            record.update(owner_user_id=str(instance.owner_user_id), tenant_id=instance.tenant_id, incident_id=incident.incident_id if incident else None)
            if has_private_fields(record):
                raise CIError("CI_QUALITY_SOURCE_UNAVAILABLE", 422)
            # 仅包含既有模拟执行的已采集证据；不导出 current_state、template/profile 或自由文本。
            records.append(freeze_fact(record))
            sources.append(dict(execution_id=execution_id, instance_id=instance.instance_id, incident_id=record["incident_id"],
                batch_id=version.batch_id if version else None, original_evidence_digest=execution.evidence_digest,
                manifest=freeze_fact(execution.evidence_manifest)))
        observation = dict(protocol="ci-business-observations.v1", kind="REPAIR" if repair else "PAIR", records=records, repair=repair)
        result = dict(protocol="ci-business-export.v1", source_deployment="localagent-simulated-ci", exported_by_user_id=str(principal.user_id),
            tenant_id=principal.tenant_id, exported_at=datetime.now(timezone.utc).isoformat(), trust="AUTHORIZED_USER_SIMULATION_IMPORT",
            observations=observation, observations_digest=sha256_digest(observation), sources=sources)
        result["export_digest"] = sha256_digest(result)
        return result


def check_records(records):
    violations, refs = [], []
    ids = set()
    for r in records:
        identity = r.get("execution_id")
        if not identity or identity in ids or r.get("outcome") not in {"PASS", "FAIL"}:
            violations.append("EXECUTION_IDENTITY_OR_OUTCOME")
        ids.add(identity)
        summary, manifest = r.get("summary", {}), r.get("evidence_manifest", [])
        if any(summary.get(k) != r.get(k) for k in ("execution_id", "environment_id", "case_id", "outcome")):
            violations.append("SUMMARY_BINDING")
        if not manifest or sha256_digest(sorted(manifest, key=lambda e: e["evidence_id"])) != r.get("evidence_digest"):
            violations.append("MANIFEST_DIGEST")
        for e in manifest:
            if e.get("execution_id") != identity:
                violations.append("EVIDENCE_IDENTITY")
            if e.get("availability") == "AVAILABLE":
                if sha256_digest(e.get("content")) != e.get("digest") or e.get("content", {}).get("execution_id") != identity:
                    violations.append("EVIDENCE_DIGEST_OR_BINDING")
                refs.append(dict(evidence_id=e["evidence_id"], digest=e["digest"]))
        if r.get("outcome") == "FAIL" and not r.get("incident_id") and r.get("state_revision") == 1:
            violations.append("ORIGINAL_FAILURE_INCIDENT_MISSING")
    return dict(status="CHECKED", violations=sorted(set(violations)), evidence_refs=refs)


def check_repair(repair, records):
    if repair is None:
        return dict(status="NOT_APPLICABLE", violations=[], eligible=None)
    violations = []
    by_id = {r["execution_id"]: r for r in records}
    origin = by_id.get(repair.get("execution_id"))
    if origin is None or origin.get("outcome") != "FAIL" or origin.get("instance_id") != repair.get("instance_id") or origin.get("incident_id") != repair.get("incident_id"):
        return dict(status="MISSING_EVIDENCE", violations=["ORIGINAL_FAILURE_MISSING"], eligible=None)
    applied = [a for a in repair["actions"] if a["status"] == "APPLIED" and a["applied_state_revision"] == repair["state_revision"]]
    action = applied[-1] if applied else None
    if action and (not action["confirmed_at"] or action["source_kind"] != "HUMAN_MANUAL" or action["confirmed_by_user_id"] != repair["owner_user_id"] or action["base_state_revision"] >= action["applied_state_revision"] or action["incident_id"] != repair["incident_id"]):
        violations.append("ACTION_AUDIT_INVALID")
    valid = {}
    for v in repair["verifications"]:
        if not action or v["state_revision"] != repair["state_revision"] or v["completed_at"] < action["confirmed_at"]:
            continue
        expected = repair["required_case_ids"][:1] if v["mode"] == "SINGLE" else repair["required_case_ids"]
        items = [by_id.get(i["execution_id"]) for i in v["items"]]
        if (v["instance_id"] == repair["instance_id"] and v["incident_id"] == repair["incident_id"] and v["created_by_user_id"] == repair["owner_user_id"]
                and len(items) == len(expected) and len({i["execution_id"] for i in v["items"]}) == len(expected)
                and [i["case_id"] for i in v["items"]] == expected and v["requested_case_ids"] == expected
                and all(r and r["instance_id"] == repair["instance_id"] and r["state_revision"] == repair["state_revision"] and r["outcome"] == "PASS"
                    and r["completed_at"] >= action["confirmed_at"] and r["case_id"] == i["case_id"] for r, i in zip(items, v["items"]))):
            valid[v["mode"]] = v
    eligible = not violations and action is not None and {"SINGLE", "GROUP"} <= valid.keys()
    if repair["status"] == "CLOSED":
        c = repair.get("closure") or {}
        if (not eligible or not repair["closed_at"] or repair["closed_by_user_id"] != repair["owner_user_id"] or c.get("source_kind") != "HUMAN_MANUAL"
                or c.get("state_revision") != repair["state_revision"] or c.get("action_id") != (action or {}).get("action_id")
                or c.get("single_verification_id") != valid.get("SINGLE", {}).get("verification_id")
                or c.get("group_verification_id") != valid.get("GROUP", {}).get("verification_id") or c.get("regression_confirmed") is not True
                or any(repair["closed_at"] < v["completed_at"] for v in valid.values())):
            violations.append("CLOSURE_WITHOUT_CURRENT_VERIFICATION")
    elif repair["closed_at"] is not None or repair["closure"] is not None:
        violations.append("OPEN_HAS_CLOSURE")
    return dict(status="CHECKED", violations=sorted(set(violations)), eligible=eligible, incident_status=repair["status"])


def evaluate(body):
    records = [unpack(fact, RECORD_KEYS) for fact in body.observations.records]
    repair = unpack(body.observations.repair, REPAIR_KEYS) if body.observations.repair else None
    if (body.observations.kind == "PAIR" and (len(records) != 2 or repair is not None)) or (body.observations.kind == "REPAIR" and repair is None):
        raise CIError("CI_QUALITY_OBSERVATIONS_INVALID", 422)
    record_check = check_records(records)
    pair = dict(candidate=None, reason="NOT_APPLICABLE", evidence_refs=[])
    if body.observations.kind == "PAIR":
        pair = grouping.pair_candidate(*(SimpleNamespace(**r) for r in records), body.rule_version)
        if record_check["violations"]:
            pair = dict(candidate=False, reason="INSUFFICIENT_EVIDENCE", evidence_refs=[])
    return dict(protocol="ci-business-quality-output.v1", **{k: getattr(body, k) for k in ("project_id", "run_id", "attempt_id", "request_id", "case_id", "frozen_input_digest", "rule_version")},
        definition_digest=definition_digest(body.rule_version), observations_digest=sha256_digest(body.observations.model_dump(mode="json")),
        record_consistency=record_check, pair_candidate=pair, repair_consistency=check_repair(repair, records))
