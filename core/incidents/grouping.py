"""仅根据可见证据形成 parser 兼容性候选；不判断最终根因。"""
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import select
from core.ci_simulation.service import CIError
from core.ci_simulation.models import CIExecutionRow, NightlyVersionExecutionRow
from core.incidents.models import IncidentRow, IncidentGroupRow as Group, IncidentGroupMembershipRow as Member
from core.runtime.snapshot_serialization import sha256_digest

RULE = "parser-compatibility.v1"


def signature(execution):
    manifest = execution.evidence_manifest
    if sha256_digest(sorted(manifest, key=lambda e: e["evidence_id"])) != execution.evidence_digest:
        return None, "INSUFFICIENT_EVIDENCE", []
    records = {e["kind"]: e for e in manifest}
    required = ["PARSER_LOG", "ENVIRONMENT_RECORD", "CASE_REQUIREMENTS", "COMPARISON_LOG", "CI_SUMMARY"]
    refs = []
    for kind in required:
        entry = records.get(kind)
        if not entry or entry["availability"] != "AVAILABLE" or not isinstance(entry["content"], dict) or sha256_digest(entry["content"]) != entry["digest"]:
            return None, "INSUFFICIENT_EVIDENCE", []
        if entry["content"].get("execution_id") != execution.execution_id:
            return None, "INSUFFICIENT_EVIDENCE", []
        refs.append(dict(evidence_id=entry["evidence_id"], digest=entry["digest"], kind=kind))
    parser, env, case, compare, summary = [records[k]["content"] for k in required]
    if parser.get("error") != "UNSUPPORTED_FIELD_REVISION":
        return None, "NOT_SUPPORTED_SIGNATURE", refs
    failure = next((s for s in summary.get("stages", []) if s.get("error")), {})
    if (parser.get("status") != "EXECUTED" or parser.get("skipped_reason") is not None
            or failure.get("stage") != "PARSER" or failure.get("error") != parser["error"]
            or compare.get("caller") != "COMPARE" or compare.get("callee") != "PARSER"
            or compare.get("error") != "PARSE_RESULT_EMPTY" or compare.get("skipped_reason") is not None
            or compare.get("call_id") != parser.get("call_id") or compare.get("artifact_id") != parser.get("artifact_id")):
        return None, "INSUFFICIENT_EVIDENCE", refs
    tools = ["parser_version", "decoder_table_version", "parser_supported_field_revision", "decoder_supported_field_revision"]
    if any(parser.get(k) is None or parser[k] != env.get(k) for k in tools):
        return None, "INSUFFICIENT_EVIDENCE", refs
    revision, required_revision = parser.get("field_revision"), case.get("required_field_revision")
    if (not isinstance(revision, int) or revision != required_revision
            or not all(isinstance(parser[k], int) for k in tools[2:])
            or revision <= min(parser[k] for k in tools[2:])
            or not all(case.get(k) for k in ("feature_id", "comparison_rule_id")) or not env.get("software_version")):
        return None, "INSUFFICIENT_EVIDENCE", refs
    return dict(rule_version=RULE, software_version=env["software_version"], feature_id=case["feature_id"],
        comparison_rule_id=case["comparison_rule_id"], field_revision=revision, required_field_revision=required_revision,
        **{k: parser[k] for k in tools}, stage="PARSER", error=parser["error"], caller="COMPARE", callee="PARSER"), None, refs


async def observe(session, batch, execution, incident):
    payload, _, refs = signature(execution)
    if payload is None:
        return
    digest = sha256_digest(payload)
    group = (await session.execute(select(Group).where(Group.batch_id == batch.batch_id, Group.kind == "AUTO",
        Group.rule_version == RULE, Group.signature_digest == digest).with_for_update())).scalar_one_or_none()
    if group is None:
        group = Group(group_id=str(uuid4()), batch_id=batch.batch_id, rule_version=RULE, signature_digest=digest,
            signature_payload=payload, kind="AUTO", revision=1, created_at=datetime.now(timezone.utc), history=[])
        session.add(group)
        await session.flush()
    else:
        group.revision += 1
    session.add(Member(group_id=group.group_id, incident_id=incident.incident_id, execution_id=execution.execution_id,
        evidence_refs=refs, decision="CANDIDATE", created_at=datetime.now(timezone.utc)))
    await session.flush()


async def members(session, group_id):
    return (await session.execute(select(Member, IncidentRow, CIExecutionRow).join(IncidentRow,
        Member.incident_id == IncidentRow.incident_id).join(CIExecutionRow, Member.execution_id == CIExecutionRow.execution_id)
        .where(Member.group_id == group_id).order_by(Member.incident_id))).all()


async def payload(session, principal, group, manager):
    all_members = await members(session, group.group_id)
    visible = [m for m in all_members if manager or m[1].owner_user_id == principal.user_id]
    if not visible:
        raise CIError("CI_NOT_FOUND", 404)
    ids = {m[1].incident_id for m in visible}
    current = [m for m in visible if m[0].decision != "EXCLUDED"]
    cross = len({m[2].environment_id for m in current}) >= 2
    state = "OWN_SCOPE_ONLY" if not manager else "INDEPENDENT"
    if manager and cross:
        state = "CONFIRMED_ASSOCIATION" if all(m[0].decision == "CONFIRMED" for m in current) else "CANDIDATE"
    history = []
    for event in group.history:
        selected = [i for i in event["incident_ids"] if i in ids]
        if not selected:
            continue
        history.append(dict(kind=event["kind"], at=event["at"], incident_ids=selected,
            actor_user_id=event["actor_user_id"] if manager or event["actor_user_id"] == str(principal.user_id) else None,
            reason=event["reason"] if manager or event["actor_user_id"] == str(principal.user_id) else None,
            before=[m for m in event["before"] if m["incident_id"] in ids],
            after=[m for m in event["after"] if m["incident_id"] in ids]))
    return dict(group_id=group.group_id, kind=group.kind, revision=group.revision, state=state,
        source="DETERMINISTIC_EVIDENCE_MATCH", scope="MANAGEMENT" if manager else "OWN_ONLY",
        signature=group.signature_payload, members=[dict(incident_id=i.incident_id, execution_id=e.execution_id,
            environment_id=e.environment_id, software_version=e.execution_snapshot["environment"]["software_version"],
            owner_user_id=str(i.owner_user_id), decision=m.decision, status=i.status, can_open=i.owner_user_id == principal.user_id,
            evidence_refs=m.evidence_refs if i.owner_user_id == principal.user_id else []) for m, i, e in visible], history=history)


async def list_groups(session, principal, batch, manager):
    groups = (await session.execute(select(Group).where(Group.batch_id == batch.batch_id).order_by(Group.created_at))).scalars().all()
    items = []
    for group in groups:
        if not manager and not any(i.owner_user_id == principal.user_id for _, i, _ in await members(session, group.group_id)):
            continue
        items.append(await payload(session, principal, group, manager))
    return dict(items=items)


async def get_group(session, principal, batch, group_id, manager, *, lock=False):
    query = select(Group).where(Group.group_id == group_id, Group.batch_id == batch.batch_id)
    group = (await session.execute(query.with_for_update() if lock else query)).scalar_one_or_none()
    if group is None:
        raise CIError("CI_NOT_FOUND", 404)
    if not manager and not any(i.owner_user_id == principal.user_id for _, i, _ in await members(session, group_id)):
        raise CIError("CI_NOT_FOUND", 404)
    return group


async def decide(session, principal, batch, group_id, manager, body):
    group = await get_group(session, principal, batch, group_id, manager, lock=True)
    digest = sha256_digest(body.model_dump(mode="json"))
    previous = next((h for h in group.history if h["request_id"] == str(body.request_id)), None)
    if previous:
        if previous["digest"] != digest or previous["actor_user_id"] != str(principal.user_id):
            raise CIError("CI_REQUEST_CONFLICT", 409)
        return previous["result"]
    if group.revision != body.expected_group_revision:
        raise CIError("CI_GROUP_REVISION_CONFLICT", 409)
    current = [m for m in await members(session, group_id) if m[0].decision != "EXCLUDED"]
    selected = [m for m in current if m[1].incident_id in body.incident_ids]
    if len(selected) != len(body.incident_ids) or any(not manager and i.owner_user_id != principal.user_id for _, i, _ in selected):
        raise CIError("CI_NOT_FOUND", 404)
    if body.kind == "SPLIT" and len(selected) == len(current):
        raise CIError("CI_GROUP_SPLIT_SUBSET_REQUIRED", 409)
    now, target = datetime.now(timezone.utc), None
    before = [dict(incident_id=i.incident_id, group_id=m.group_id, decision=m.decision) for m, i, _ in selected]
    if body.kind == "SPLIT":
        target = Group(group_id=str(uuid4()), batch_id=batch.batch_id, rule_version=group.rule_version,
            signature_digest=group.signature_digest, signature_payload=group.signature_payload, kind="MANUAL_SPLIT", revision=1,
            created_at=now, history=[])
        session.add(target)
        await session.flush()
    for member, _, _ in selected:
        member.decision = "CONFIRMED" if body.kind == "CONFIRM" else "EXCLUDED"
        member.decided_at, member.decided_by_user_id = now, principal.user_id
    await session.flush()
    if target:
        for member, incident, _ in selected:
            session.add(Member(group_id=target.group_id, incident_id=incident.incident_id, execution_id=member.execution_id,
                evidence_refs=member.evidence_refs, decision="CANDIDATE", created_at=now))
    group.revision += 1
    result = dict(group_id=group_id, revision=group.revision, target_group_id=target.group_id if target else None)
    event = dict(request_id=str(body.request_id), digest=digest, kind=body.kind, actor_user_id=str(principal.user_id),
        at=now.isoformat(), incident_ids=body.incident_ids, reason=body.reason, before=before,
        after=[dict(incident_id=m.incident_id, group_id=target.group_id if target else group_id,
            decision="CANDIDATE" if target else m.decision) for m, _, _ in selected], result=result)
    group.history = [*group.history, event]
    if target:
        target.history = [event]
    await session.flush()
    return result
