"""原 execution repository 内部的委派事务与预算分配操作。"""
from datetime import UTC, datetime, timedelta
import json
from sqlalchemy import select
from sqlalchemy import func

from core.persistence.models import (RuntimeAgentDelegationRow, RuntimeLoopFrameRow,
    RuntimeLoopIterationRow, DurableToolInvocationRow, RuntimeLoopActionRow)
from core.runtime.execution_aggregate import canonical_payload_digest as digest
from .agent_loop import identity


async def create_child(repo, session, lease, root, frame, iteration, decision, prepared):
    if frame.frame_role != "ROOT" or frame.schema_version != 2:
        raise RuntimeError("DELEGATION_DEPTH_LIMIT")
    target = frame.pinned_config.get("delegation_targets", {}).get(decision.target_agent_id)
    if target is None or prepared["permission_pin"] != target or digest(prepared["config"]) != target["config_digest"]:
        raise RuntimeError("DELEGATION_TARGET_DENIED")
    rows = tuple((await session.execute(select(RuntimeAgentDelegationRow).where(
        RuntimeAgentDelegationRow.run_id == lease.run_id).order_by(RuntimeAgentDelegationRow.delegation_id).with_for_update())).scalars())
    if len(rows) >= frame.loop_policy["max_delegations"] or any(not r.observed for r in rows):
        raise RuntimeError("DELEGATION_COUNT_LIMIT")
    did = identity(frame.frame_id, iteration.iteration_no, "delegation")
    child_id = identity(frame.frame_id, iteration.iteration_no, "child")
    available = {name.removeprefix("max_"): max(0, n-root.budget_consumed.get(name.removeprefix("max_"),0)-root.budget_reserved.get(name.removeprefix("max_"),0))
        for name,n in root.budget_totals.items() if isinstance(n,int)}
    from core.runtime.agent_loop.contracts import LoopPolicyV2
    target_limits = LoopPolicyV2(**prepared["config"]["agent"]["business_options"].get("loop_policy", {})).to_dict()
    total = min(available["total_tokens"] // 2, target_limits["total_tokens"])
    models = min(8, available["model_calls"] // 2, target_limits["max_model_calls"])
    tools = min(4, available["tool_calls"] // 2, target_limits["max_tool_calls"])
    costs = min(available["cost_units"] // 2, target_limits["cost_units"])
    if min(total,models,tools,costs) <= 0:
        raise RuntimeError("DELEGATION_BUDGET_EXHAUSTED")
    allocation = {"model_calls": models, "remote_model_calls": min(models,available.get("remote_model_calls",models)),
        "tool_calls": tools, "input_tokens": min(total*3//4,available.get("input_tokens",total)),
        "output_tokens": min(total-total*3//4,available.get("output_tokens",total)), "total_tokens": total,
        "cost_units": costs, "retries": min(target_limits["max_retries"],available.get("retries",2))}
    envelope = f"delegation:{did}"
    operations = dict(root.budget_operations)
    operations[envelope] = {"kind": "DELEGATION_ALLOCATION", "state": "RESERVED", "reserved": allocation,
        "unspent": allocation, "consumed": {}, "actual": None, "source": None, "generation": frame.resume_generation}
    root.budget_operations, root.budget_reserved = operations, repo._budget_add(root.budget_reserved, allocation)
    root.execution_version += 1
    root.last_committed_fencing_token, root.updated_at = lease.fencing_token, func.now()
    deadline = min(root.absolute_deadline, datetime.now(UTC)+timedelta(seconds=min(120,frame.loop_policy["max_elapsed_seconds"],target_limits["max_elapsed_seconds"])))
    frozen = {**prepared, "allocation": allocation, "child_frame_id": child_id}
    child_policy = dict(frame.loop_policy)
    child_policy.update(max_iterations=min(8,child_policy["max_iterations"]), max_model_calls=models,
        max_tool_calls=tools, total_tokens=total,cost_units=costs)
    for key in ("max_iterations","max_model_calls","max_tool_calls","max_retries","max_elapsed_seconds"):
        child_policy[key] = min(child_policy[key], target_limits[key])
    child = RuntimeLoopFrameRow(frame_id=child_id,run_id=lease.run_id,step_id=frame.step_id,schema_version=2,
        decision_schema_version=2,context_seed_version=2,execution_policy="durable-agent-loop.v2",frame_role="CHILD",
        execution_agent_id=decision.target_agent_id,parent_frame_id=frame.frame_id,delegation_id=did,
        resume_generation=frame.resume_generation,last_committed_fencing_token=lease.fencing_token,
        context_seed=prepared["seed"],seed_digest=digest(prepared["seed"]),pinned_config=prepared["config"],
        config_digest=digest(prepared["config"]),loop_policy=child_policy,context_policy=prepared["config"]["context_policy"])
    session.add(child)
    session.add(RuntimeAgentDelegationRow(delegation_id=did,run_id=lease.run_id,step_id=frame.step_id,
        parent_frame_id=frame.frame_id,parent_iteration=iteration.iteration_no,child_frame_id=child_id,
        frozen=frozen,frozen_digest=digest(frozen),absolute_deadline=deadline,envelope_id=envelope,
        accepted_generation=frame.resume_generation))
    iteration.state, frame.phase = "ACCEPTED", "WAIT_CHILD"
    iteration.assistant_record = {"role": "assistant", "content": None, "tool_calls": [{"id": decision.provider_tool_call_id,
        "type": "function", "function": {"name": "__agent_handoff_v1", "arguments": json.dumps(
            {"target_agent_id":decision.target_agent_id,"task":decision.task},ensure_ascii=False,sort_keys=True)}}]}


async def load_delegation(repo, lease, parent_frame_id, iteration_no):
    async with repo.database.transaction() as session:
        await repo._lock_loop(session,lease,parent_frame_id,allow_control_stop=True)
        row = (await session.execute(select(RuntimeAgentDelegationRow).where(
            RuntimeAgentDelegationRow.parent_frame_id==parent_frame_id,RuntimeAgentDelegationRow.parent_iteration==iteration_no))).scalar_one()
        if digest(row.frozen)!=row.frozen_digest:
            raise RuntimeError("DELEGATION_SOURCE_INVALID")
        return row, await session.get(RuntimeLoopFrameRow,row.child_frame_id)


async def commit_child_result(repo,lease,frame_id,*,resume_generation,outcome,content,stop_proof=None):
    async with repo.database.transaction() as session:
        root,child = await repo._lock_loop(session,lease,frame_id,resume_generation=resume_generation)
        delegation = await session.get(RuntimeAgentDelegationRow,child.delegation_id,with_for_update=True)
        if child.frame_role!="CHILD" or delegation.child_frame_id!=frame_id:
            raise RuntimeError("DELEGATION_SOURCE_INVALID")
        if delegation.result is not None:
            return delegation.result
        from core.persistence.models import RuntimeModelInvocationRow
        pending = (await session.execute(select(RuntimeModelInvocationRow).where(
            RuntimeModelInvocationRow.loop_frame_id==frame_id,RuntimeModelInvocationRow.state.in_(("STARTED","UNKNOWN"))))).scalars().first()
        if pending is not None:
            from core.runtime.agent_loop.contracts import LoopSuspended
            raise LoopSuspended("DELEGATION_OUTCOME_UNKNOWN")
        actions = tuple((await session.execute(select(RuntimeLoopActionRow).where(RuntimeLoopActionRow.frame_id==frame_id)
            .order_by(RuntimeLoopActionRow.iteration_no,RuntimeLoopActionRow.ordinal).with_for_update())).scalars())
        tools = tuple((await session.execute(select(DurableToolInvocationRow).where(
            DurableToolInvocationRow.invocation_id.in_([a.action_id for a in actions])))).scalars())
        if any(t.state in {"STARTED","UNKNOWN"} for t in tools):
            from core.runtime.agent_loop.contracts import LoopSuspended
            raise LoopSuspended("DELEGATION_OUTCOME_UNKNOWN")
        if outcome == "FAILED":
            allocation = root.budget_operations[delegation.envelope_id]
            if not stop_proof or stop_proof.get("kind") not in {"BUDGET","DEADLINE","LOOP_STOP","MODEL_FAILURE"}:
                raise RuntimeError("DELEGATION_STOP_UNPROVEN")
            if stop_proof["kind"]=="DEADLINE" and datetime.now(UTC)<delegation.absolute_deadline:
                raise RuntimeError("DELEGATION_STOP_UNPROVEN")
            if stop_proof["kind"]=="LOOP_STOP" and stop_proof.get("code") not in {"LOOP_ITERATION_LIMIT","LOOP_NO_PROGRESS","LOOP_CONTEXT_EXHAUSTED","DELEGATION_LIMIT"}:
                raise RuntimeError("DELEGATION_STOP_UNPROVEN")
            if stop_proof["kind"]=="BUDGET" and (type(stop_proof.get("requested")) is not int
                or stop_proof["requested"]<=allocation["unspent"].get(stop_proof.get("dimension"),0)):
                raise RuntimeError("DELEGATION_STOP_UNPROVEN")
            by_id={t.invocation_id:t for t in tools}
            for action in actions:
                if action.result_projection is not None or action.not_executed is not None: continue
                tool=by_id.get(action.action_id)
                if tool is not None and tool.state=="COMMITTED" and tool.committed_result:
                    action.result_projection={"outcome":tool.committed_result["status"],"content":tool.committed_result["output"]["content"]}
                    action.result_digest,action.source_digest=digest(action.result_projection),tool.committed_result_digest
                elif tool is None or tool.state=="PREPARED" and tool.started_at is None:
                    action.not_executed={"reason":"CHILD_STOP","delegation_id":delegation.delegation_id,"proof_digest":digest(stop_proof)}
                else:
                    raise RuntimeError("DELEGATION_OUTCOME_UNKNOWN")
        if any(not a.result_projection and not a.not_executed for a in actions):
            raise RuntimeError("DELEGATION_OUTCOME_UNKNOWN")
        if outcome not in {"SUCCEEDED","REFUSAL","FAILED","RESULT_TOO_LARGE"} or content is not None and len(content.encode("utf-8"))>16384:
            raise RuntimeError("DELEGATION_RESULT_INVALID")
        if outcome in {"SUCCEEDED","REFUSAL"} and child.phase!="RESULT_READY":
            raise RuntimeError("DELEGATION_RESULT_INVALID")
        if child.phase == "RESULT_READY":
            from core.persistence.models import RuntimeModelInvocationRow
            from core.runtime.model_invocation import restore_loop_model_response
            from core.runtime.agent_loop.contracts import LoopDecisionV2
            iteration = await session.get(RuntimeLoopIterationRow, (frame_id, child.current_iteration))
            model = await session.get(RuntimeModelInvocationRow, (lease.run_id, child.step_id,
                iteration.accepted_model_attempt, iteration.accepted_model_attempt_number))
            decision = LoopDecisionV2.parse(restore_loop_model_response(model),
                protocol_mode=child.context_seed["protocol_mode"], synthetic_id="unused")
            if (decision.digest != iteration.decision_digest or decision.kind not in {"FINAL","REFUSAL"}
                    or outcome != "RESULT_TOO_LARGE" and content != decision.content):
                raise RuntimeError("DELEGATION_RESULT_INVALID")
            outcome = "REFUSAL" if decision.kind == "REFUSAL" else outcome
        await repo._memory_admission(session,lease)
        operations=dict(root.budget_operations)
        allocation=operations[delegation.envelope_id]
        if any(o.get("delegation_id")==delegation.delegation_id and o["state"]=="RESERVED" for o in operations.values()):
            raise RuntimeError("DELEGATION_BUDGET_INFLIGHT")
        root.budget_reserved=repo._budget_add(root.budget_reserved,allocation["unspent"],-1)
        operations[delegation.envelope_id]={**allocation,"state":"RELEASED","released":allocation["unspent"],"unspent":{}}
        root.budget_operations=operations
        root.execution_version += 1
        root.last_committed_fencing_token, root.updated_at = lease.fencing_token, func.now()
        iteration = await session.get(RuntimeLoopIterationRow, (frame_id, child.current_iteration)) if child.current_iteration else None
        model_ref = [lease.run_id, child.step_id, iteration.accepted_model_attempt, iteration.accepted_model_attempt_number] if iteration and iteration.decision_id else None
        delegation.result={"delegation_id":delegation.delegation_id,"child_frame_id":frame_id,"outcome":outcome,
            "content":content,"trust":"AGENT_DERIVED_DATA","decision_id":child.result_decision_id,
            "accepted_decision_id":iteration.decision_id if iteration else None,
            "accepted_decision_digest":iteration.decision_digest if iteration else None,"model_ref":model_ref,
            "stop_proof":stop_proof,
            "source_digest":digest([{ "action_id":a.action_id,"digest":a.source_digest or digest(a.not_executed)} for a in actions])}
        delegation.result_digest=digest(delegation.result)
        if child.phase!="RESULT_READY": child.phase,child.stop_code="STOPPED","DELEGATION_FAILED"
        repo._touch_loop(child,lease)
        await session.flush()
        return delegation.result


async def accept_child(repo,lease,parent_frame_id,iteration_no,*,resume_generation,row_version):
    async with repo.database.transaction() as session:
        _,parent=await repo._lock_loop(session,lease,parent_frame_id,resume_generation=resume_generation)
        iteration=await session.get(RuntimeLoopIterationRow,(parent_frame_id,iteration_no),with_for_update=True)
        delegation=(await session.execute(select(RuntimeAgentDelegationRow).where(
            RuntimeAgentDelegationRow.parent_frame_id==parent_frame_id,RuntimeAgentDelegationRow.parent_iteration==iteration_no).with_for_update())).scalar_one()
        if delegation.observed: return iteration
        repo._check_loop_version(parent,row_version)
        if parent.phase!="WAIT_CHILD" or delegation.result is None or digest(delegation.result)!=delegation.result_digest:
            raise RuntimeError("DELEGATION_RESULT_UNAVAILABLE")
        await repo._memory_admission(session,lease)
        iteration.observation={"results":[{"call_id":iteration.assistant_record["tool_calls"][0]["id"],
            "delegation_id":delegation.delegation_id,"result":delegation.result}],"all_read_only":False}
        iteration.observation_digest=digest(iteration.observation)
        iteration.observation_source_digest=delegation.result_digest
        parent.committed_observation_cursor+=1
        iteration.observation_cursor,iteration.observed_generation=parent.committed_observation_cursor,resume_generation
        iteration.state,parent.phase,delegation.observed="OBSERVED","NEED_DECISION",True
        repo._touch_loop(parent,lease)
        await session.flush()
        return iteration
