"""agent-execution-evidence.v1：只投影已持久化来源，不输出任务、参数或正文。"""
from core.runtime.execution_aggregate import canonical_payload_digest as digest
from core.runtime.request_counting_admission import persisted_eligible, persisted_validation


def build_execution_evidence(image, actions, delegations, binding=None):
    if image.root.execution_policy != "durable-agent-loop.v2":
        raise ValueError("UNSUPPORTED_EXECUTION_POLICY")
    gaps, frames, decisions, batches, children = set(), [], [], [], []
    if binding is None:
        gaps.add("MEMORY_BINDING_MISSING")
    roots = [f for f in image.loop_frames if f.frame_role == "ROOT"]
    if len(roots) != 1: gaps.add("ROOT_IDENTITY_MISSING")
    tools = {t.invocation_id:t for t in image.tool_invocations}
    models = {(m.attempt_number,m.model_attempt_number):m for m in image.models}
    frame_by_id = {f.frame_id: f for f in image.loop_frames}
    operations = image.root.budget_operations
    for frame in image.loop_frames:
        if (frame.schema_version!=2 or digest(frame.context_seed)!=frame.seed_digest or digest(frame.pinned_config)!=frame.config_digest):
            gaps.add("FRAME_PIN_INVALID")
        frames.append({"ref":digest(frame.frame_id),"role":frame.frame_role,"agent_ref":digest(frame.execution_agent_id),
            "parent_ref":digest(frame.parent_frame_id) if frame.parent_frame_id else None,
            "seed_digest":frame.seed_digest,"config_digest":frame.config_digest,
            "generation":frame.resume_generation,"fence":frame.last_committed_fencing_token,
            "final_delivered":frame.final_delivery_receipt is not None})
        if frame.frame_role=="CHILD" and frame.final_delivery_receipt: gaps.add("CHILD_FINAL_INVALID")
    for iteration in image.loop_iterations:
        if not iteration.decision_id:
            gaps.add("DECISION_UNCONFIRMED"); continue
        model = models.get((iteration.accepted_model_attempt,iteration.accepted_model_attempt_number))
        if (model is None or model.state!="COMPLETED" or model.loop_frame_id!=iteration.frame_id
                or model.loop_iteration_no!=iteration.iteration_no or model.purpose!="DECISION"
                or model.context_snapshot_id!=iteration.context_snapshot_id or digest(model.result_binding)!=model.result_digest
                or not model.request_assessment or (model.request_assessment.get("admissible") is not True
                    and not persisted_eligible(model.request_assessment,image.root,frame_by_id.get(iteration.frame_id)))
                or model.request_assessment.get("request_digest")!=model.request_digest):
            gaps.add("MODEL_BINDING_MISSING")
        if not iteration.context_snapshot_id: gaps.add("CONTEXT_BINDING_MISSING")
        decisions.append({"ref":digest(iteration.decision_id),"frame_ref":digest(iteration.frame_id),
            "iteration":iteration.iteration_no,"kind":iteration.decision_kind,"decision_digest":iteration.decision_digest,
            "model_ref":digest([iteration.run_id,iteration.step_id,iteration.accepted_model_attempt,iteration.accepted_model_attempt_number]),
            "snapshot_ref":digest(iteration.context_snapshot_id),"observation_digest":iteration.observation_digest})
    for action in actions:
        tool = tools.get(action.invocation_id)
        if action.not_executed:
            state="NOT_EXECUTED"
            if tool is not None and (tool.state != "PREPARED" or tool.started_at is not None): gaps.add("SKIP_PROOF_INVALID")
        elif (tool is not None and tool.state=="COMMITTED" and tool.committed_result_digest==action.source_digest
                and action.result_projection is not None and digest(action.result_projection)==action.result_digest):
            state="COMMITTED"
        else:
            state="UNKNOWN"; gaps.add("ACTION_UNCONFIRMED")
        if digest(action.recipe)!=action.recipe_digest: gaps.add("ACTION_RECIPE_INVALID")
        batches.append({"ref":digest(action.action_id),"frame_ref":digest(action.frame_id),"iteration":action.iteration_no,
            "ordinal":action.ordinal,"call_ref":digest(action.protocol_call_id),"invocation_ref":digest(action.invocation_id) if action.invocation_id else None,
            "recipe_digest":action.recipe_digest,"state":state,"source_digest":action.source_digest,"projection_digest":action.result_digest})
    for delegation in delegations:
        allocation = operations.get(delegation.envelope_id)
        child_ops = [o for o in operations.values() if o.get("delegation_id")==delegation.delegation_id]
        conserved = allocation is not None
        if allocation:
            for dimension, amount in allocation["reserved"].items():
                consumed=sum(o["actual"].get(dimension,0) for o in child_ops if o["state"]=="SETTLED")
                inflight=sum(o["reserved"].get(dimension,0) for o in child_ops if o["state"]=="RESERVED")
                conserved &= amount==consumed+inflight+allocation["unspent"].get(dimension,0)+allocation.get("released",{}).get(dimension,0)
            conserved &= not allocation.get("overrun",False)
        if not conserved: gaps.add("DELEGATION_BUDGET_UNPROVEN")
        if (not delegation.observed or delegation.result is None or digest(delegation.result)!=delegation.result_digest
                or digest(delegation.frozen)!=delegation.frozen_digest): gaps.add("CHILD_RESULT_UNCONFIRMED")
        children.append({"ref":digest(delegation.delegation_id),"parent_ref":digest(delegation.parent_frame_id),
            "child_ref":digest(delegation.child_frame_id),"permission_digest":digest(delegation.frozen["permission_pin"]),
            "frozen_digest":delegation.frozen_digest,"result_digest":delegation.result_digest,"parent_accepted":delegation.observed,
            "envelope_ref":digest(delegation.envelope_id),"conserved":bool(conserved)})
    snapshots=[]
    for snapshot in image.context_snapshots:
        if (digest(snapshot.payload)!=snapshot.snapshot_digest or snapshot.validation.get("version")!="context-validator.v2"
                or not all(snapshot.validation.get(k) is True for k in ("source","mandatory","roles","pairs"))
                or (snapshot.validation.get("budget") is not True
                    and not persisted_validation(snapshot.validation,image.root,frame_by_id.get(snapshot.frame_id)))):
            gaps.add("SNAPSHOT_VALIDATION_MISSING")
        snapshots.append({"ref":digest(snapshot.snapshot_id),"frame_ref":digest(snapshot.frame_id),"version":snapshot.version,
            "digest":snapshot.snapshot_digest,"source_digest":snapshot.payload.get("source_digest"),
            "mandatory_digest":snapshot.payload.get("mandatory_digest"),"validator":snapshot.validation.get("version")})
    for model in image.models:
        if model.state in {"STARTED","UNKNOWN"}: gaps.add("MODEL_UNCONFIRMED")
    if any(o["state"]=="RESERVED" for o in operations.values()): gaps.add("BUDGET_UNCONFIRMED")
    for dimension, amount in image.root.budget_consumed.items():
        if amount!=sum(o["actual"].get(dimension,0) for o in operations.values() if o["kind"]!="DELEGATION_ALLOCATION" and o["state"]=="SETTLED"):
            gaps.add("ROOT_BUDGET_UNPROVEN")
    if image.root.status not in {"SUCCEEDED","FAILED","CANCELLED"}: gaps.add("ROOT_NONTERMINAL")
    if image.root.status=="SUCCEEDED" and (len(roots)!=1 or not roots[0].final_delivery_receipt): gaps.add("FINAL_RECEIPT_MISSING")
    return {"schema_version":"agent-execution-evidence.v1","execution_policy":"durable-agent-loop.v2",
        "coverage":"PARTIAL" if gaps else "COMPLETE","gaps":sorted(gaps),"run_ref":digest(image.root.run_id),
        "outcome":image.root.status,"frames":frames,"decisions":decisions,"actions":batches,"delegations":children,
        "snapshots":snapshots,"memory_binding": {"mode":binding.mode,"status":binding.status,
            "manifest_digest":binding.manifest_digest,"admissions_digest":digest(binding.admissions)} if binding is not None else None,
        "budget":{"consumed":image.root.budget_consumed,"reserved":image.root.budget_reserved},
        "final_receipt_digest":digest(roots[0].final_delivery_receipt) if len(roots)==1 and roots[0].final_delivery_receipt else None}
