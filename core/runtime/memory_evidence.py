"""本地安全投影；不裁决授权、Runtime 终态或记忆真实性。"""
from datetime import UTC, datetime
from core.runtime.execution_aggregate import canonical_payload_digest as digest


def build_memory_evidence(binding, scopes, records, operations, snapshot_refs=()):
    gaps=[]
    if binding is None:
        return {"schema":"memory-lifecycle-evidence.v1","policy_version":1,"coverage":"PARTIAL",
            "gaps":["BINDING_MISSING"],"sealed":True}
    if binding.invalid_code == "MEMORY_CONTEXT_UNVERIFIABLE": gaps.append("BINDING_UNVERIFIABLE")
    if binding.manifest_digest != digest(binding.manifest): gaps.append("MANIFEST_UNVERIFIABLE")
    if binding.mode == "SCOPED" and (not binding.access or not binding.scopes): gaps.append("AUTHORIZATION_MISSING")
    if binding.mode == "SCOPED" and not binding.seed_digest: gaps.append("SEED_BINDING_MISSING")
    if any(o.state in {"PREPARED","UNKNOWN"} for o in operations): gaps.append("OPERATION_UNCONFIRMED")
    if any(a["id"].startswith("preplan:") and a.get("state") != "COMPLETED" for a in binding.admissions):
        gaps.append("MODEL_ADMISSION_UNCONFIRMED")
    facts=[]
    for record in records:
        if record.record_version < 1 or not record.source_identity or not record.body_digest:
            gaps.append("RECORD_PROOF_MISSING")
        facts.append({"record_ref":record.memory_id,"scope_ref":record.access_scope_id,
            "record_version":record.record_version,"epistemic_kind":record.epistemic_kind,
            "lifecycle_status":record.lifecycle_status,"body_state":record.body_state,
            "ttl":"EXPIRED" if record.expires_at and record.expires_at <= datetime.now(UTC) else "VALID",
            "source_ref":record.source_identity,"dependency_digest":digest(record.source_dependencies)})
    operation_facts=[]
    for op in operations:
        if op.kind in {"DELETE","REVOKE","PROMOTE"} and (op.expected_version is None or op.result_version is None):
            gaps.append("CAS_PROOF_MISSING")
        if op.kind == "DELETE" and not all(op.receipt.get(k) is True for k in ("authority_committed","retrieval_blocked","derived_reuse_blocked")):
            gaps.append("PROPAGATION_UNCONFIRMED")
        if op.kind=="FORMATION" and op.receipt.get("direct_user_only") and (
                not op.receipt.get("direct_source_manifest") or not op.receipt.get("user_message_digest")):
            gaps.append("SOURCE_POLICY_UNVERIFIABLE")
        operation_facts.append({"operation_ref":op.operation_id,"scope_ref":op.scope_id,"kind":op.kind,
            "state":op.state,"source_ref":op.source_identity,"request_digest":op.request_digest,
            "candidate_digest":op.candidate_digest,"expected_version":op.expected_version,"result_version":op.result_version,
            "dependency_digest":digest(op.dependencies),"outcome":op.receipt.get("outcome","UNKNOWN"),
            "source_mode":"DIRECT_USER_ONLY" if op.receipt.get("direct_user_only") else "RUN_DEPENDENCIES",
            "direct_source_digest":digest(op.receipt["direct_source_manifest"]) if op.receipt.get("direct_source_manifest") else None,
            "user_message_digest":op.receipt.get("user_message_digest"),
            "deletion_scope":op.receipt.get("scope_of_deletion"),"all_copies_erased":op.receipt.get("all_copies_erased"),
            "historical_payloads":op.receipt.get("historical_payloads"),"backup_erasure":op.receipt.get("backup_erasure")})
    return {"schema":"memory-lifecycle-evidence.v1","policy_version":1,"run_ref":"run:"+digest(binding.run_id),
        "mode":binding.mode,"status":binding.status,"reason":binding.invalid_code,
        "manifest_digest":binding.manifest_digest,"seed_digest":binding.seed_digest,
        "earliest_expiry":binding.earliest_expiry.isoformat() if binding.earliest_expiry else None,
        "snapshot_refs":list(snapshot_refs),"fence":binding.last_fencing_token,"binding_version":binding.row_version,
        "scopes":[{"scope_ref":s.scope_id,"kind":s.scope_kind,"epoch":s.authz_epoch,"status":s.status,
            "bound_epoch":next((b["epoch"] for b in binding.scopes if b["scope_id"]==s.scope_id),None),
            "authorized_action":"READ" if any(b["scope_id"]==s.scope_id for b in binding.scopes) else "DEPENDENCY_REFERENCE",
            "outcome":"ALLOW_AT_BINDING" if any(b["scope_id"]==s.scope_id for b in binding.scopes) else "SOURCE_VALIDATED_AT_BINDING"} for s in scopes],
        "records":facts,"operations":operation_facts,
        "admissions":[{"identity_digest":digest(a["id"]),"fence":a["fence"],"manifest_digest":a["manifest_digest"],"outcome":a["outcome"],"state":a.get("state"),"result_digest":a.get("result_digest")} for a in binding.admissions],
        "coverage":"PARTIAL" if gaps else "COMPLETE","gaps":sorted(set(gaps)),"sealed":True}
