"""DurableExecutionRepository 的 v2 事务实现；共用原 Session、租约和状态 Owner。"""
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from core.persistence.models import (RuntimeLoopIterationRow, RuntimeLoopActionRow,
    RuntimeModelInvocationRow, DurableToolInvocationRow)
from core.runtime.execution_aggregate import canonical_payload_digest as digest


def identity(frame_id, iteration, suffix):
    return uuid5(NAMESPACE_URL, f"{frame_id}:{iteration}:{suffix}").hex


async def accept_batch(repo, lease, frame_id, iteration_no, *, resume_generation, row_version,
                       decision, model_attempt, recipes, assistant_record, delegation=None):
    async with repo.database.transaction() as session:
        root, frame = await repo._lock_loop(session, lease, frame_id, resume_generation=resume_generation)
        iteration = await session.get(RuntimeLoopIterationRow, (frame_id, iteration_no), with_for_update=True)
        if frame.schema_version != 2 or iteration is None or iteration.schema_version != 2:
            raise RuntimeError("LOOP_VERSION_UNSUPPORTED")
        if iteration.decision_id:
            if iteration.decision_digest != decision.digest:
                raise RuntimeError("LOOP_DECISION_CONFLICT")
            return iteration
        repo._check_loop_version(frame, row_version)
        if frame.phase != "NEED_DECISION" or iteration.state != "OPEN" or frame.current_iteration != iteration_no:
            raise RuntimeError("LOOP_STATE_INVALID")
        model = await session.get(RuntimeModelInvocationRow, (lease.run_id, frame.step_id, *model_attempt), with_for_update=True)
        if (model is None or model.state != "COMPLETED" or model.loop_frame_id != frame_id
                or model.loop_iteration_no != iteration_no or model.purpose != "DECISION"
                or model.context_snapshot_id != iteration.context_snapshot_id):
            raise RuntimeError("LOOP_MODEL_BINDING_INVALID")
        from core.runtime.model_invocation import restore_loop_model_response
        from core.runtime.agent_loop.contracts import LoopDecisionV2
        restored = LoopDecisionV2.parse(restore_loop_model_response(model), protocol_mode=frame.context_seed["protocol_mode"],
            synthetic_id=f"call_{frame_id}_{iteration_no}", handoff_targets=frame.pinned_config.get("delegation_targets", {}))
        if restored.digest != decision.digest:
            raise RuntimeError("LOOP_DECISION_CONFLICT")
        await repo._memory_admission(session, lease)
        earlier = tuple((await session.execute(select(RuntimeLoopIterationRow).where(RuntimeLoopIterationRow.frame_id==frame_id,
            RuntimeLoopIterationRow.decision_id.is_not(None)))).scalars())
        used_calls = {call["id"] for item in earlier for call in (item.assistant_record or {}).get("tool_calls", [])}
        candidate_calls = [call["call_id"] for call in decision.calls] if decision.kind=="TOOL_BATCH" else ([decision.provider_tool_call_id] if decision.kind=="HANDOFF" else [])
        if used_calls.intersection(candidate_calls):
            raise RuntimeError("LOOP_DECISION_INVALID")
        if decision.kind == "TOOL_BATCH":
            if not 1 <= len(recipes) == len(decision.calls) <= frame.loop_policy["max_batch_size"]:
                raise RuntimeError("LOOP_DECISION_INVALID")
            if sum(r["read_only"] is not True for r in recipes) > 1:
                raise RuntimeError("LOOP_BATCH_MULTIPLE_WRITES")
            old_actions = tuple((await session.execute(select(RuntimeLoopActionRow).where(
                RuntimeLoopActionRow.run_id == lease.run_id).order_by(RuntimeLoopActionRow.action_id).with_for_update())).scalars())
            ids = set()
            for ordinal, (recipe, call) in enumerate(zip(recipes, decision.calls)):
                if recipe["tool_name"] != call["tool_name"] or digest(recipe["arguments"]) != digest(call["arguments"]):
                    raise RuntimeError("LOOP_DECISION_CONFLICT")
                if recipe["bounded_polling"] and sum(a.recipe["tool_name"] == recipe["tool_name"]
                    and a.recipe["arguments_digest"] == recipe["arguments_digest"] for a in old_actions) + sum(
                        r["tool_name"] == recipe["tool_name"] and r["arguments_digest"] == recipe["arguments_digest"] for r in recipes[:ordinal]) >= recipe["max_poll_attempts"]:
                    raise RuntimeError("LOOP_REPEATED_ACTION")
                if call["call_id"] in ids or any(a.frame_id == frame_id and a.protocol_call_id == call["call_id"] for a in old_actions):
                    raise RuntimeError("LOOP_DECISION_INVALID")
                ids.add(call["call_id"])
                if recipe["read_only"] is not True and any(a.recipe["read_only"] is not True and (
                        a.recipe["tool_name"] == recipe["tool_name"] and (a.recipe["idempotency_key"] == recipe["idempotency_key"]
                        or a.recipe["arguments_digest"] == recipe["arguments_digest"])) for a in old_actions):
                    raise RuntimeError("LOOP_REPEATED_ACTION")
                session.add(RuntimeLoopActionRow(action_id=identity(frame_id, iteration_no, f"action:{ordinal}"),
                    frame_id=frame_id, run_id=lease.run_id, step_id=frame.step_id, iteration_no=iteration_no,
                    ordinal=ordinal, protocol_call_id=call["call_id"], recipe=recipe, recipe_digest=digest(recipe)))
            iteration.assistant_record = assistant_record
            iteration.state, frame.phase = "ACCEPTED", "WAIT_ACTIONS"
        elif decision.kind in {"FINAL", "REFUSAL"}:
            if recipes or assistant_record:
                raise RuntimeError("LOOP_DECISION_INVALID")
            iteration.state, frame.phase = "CLOSED", "RESULT_READY"
            frame.result_decision_id, frame.stop_code = identity(frame_id, iteration_no, "decision"), decision.kind
        elif decision.kind == "HANDOFF" and delegation is not None:
            from .agent_delegation import create_child
            await create_child(repo, session, lease, root, frame, iteration, decision, delegation)
        else:
            raise RuntimeError("LOOP_HANDOFF_DISABLED")
        iteration.decision_id, iteration.decision_kind, iteration.decision_digest = identity(frame_id, iteration_no, "decision"), decision.kind, decision.digest
        iteration.accepted_model_attempt, iteration.accepted_model_attempt_number = model_attempt
        iteration.accepted_generation = resume_generation
        repo._touch_loop(frame, lease)
        await session.flush()
        return iteration


async def load_actions(repo, lease, frame_id, iteration_no):
    async with repo.database.transaction() as session:
        await repo._lock_loop(session, lease, frame_id, allow_control_stop=True)
        return tuple((await session.execute(select(RuntimeLoopActionRow).where(
            RuntimeLoopActionRow.frame_id == frame_id, RuntimeLoopActionRow.iteration_no == iteration_no)
            .order_by(RuntimeLoopActionRow.ordinal))).scalars())


async def record_action(repo, lease, frame_id, iteration_no, *, resume_generation, action_id,
                        projection=None, source_digest=None, not_executed=None):
    async with repo.database.transaction() as session:
        _, frame = await repo._lock_loop(session, lease, frame_id, resume_generation=resume_generation)
        row = await session.get(RuntimeLoopActionRow, action_id, with_for_update=True)
        if row is None or (row.frame_id, row.iteration_no) != (frame_id, iteration_no) or frame.phase != "WAIT_ACTIONS":
            raise RuntimeError("LOOP_ACTION_BINDING_INVALID")
        tool = await session.get(DurableToolInvocationRow, action_id, with_for_update=True)
        if not_executed is not None:
            if tool is not None and (tool.state != "PREPARED" or tool.started_at is not None) or projection is not None or not_executed.get("reason") not in {"BATCH_SKIP", "READ_PRECONDITION_FAILED"}:
                raise RuntimeError("LOOP_NOT_EXECUTED_UNPROVEN")
            cause = await session.get(RuntimeLoopActionRow, not_executed.get("cause_action_id"))
            cause_tool = await session.get(DurableToolInvocationRow, cause.action_id) if cause is not None else None
            confirmed_failure = cause is not None and (cause.result_projection is not None and cause.result_projection["outcome"] != "SUCCEEDED"
                or cause_tool is not None and cause_tool.state == "COMMITTED" and cause_tool.committed_result and cause_tool.committed_result["status"] != "SUCCEEDED")
            if (cause is None or (cause.frame_id, cause.iteration_no) != (frame_id, iteration_no)
                    or not confirmed_failure
                    or not_executed["reason"] == "BATCH_SKIP" and frame.loop_policy["failure_mode"] != "FAIL_FAST"
                    or not_executed["reason"] == "READ_PRECONDITION_FAILED" and (row.recipe["read_only"] or not cause.recipe["read_only"])):
                raise RuntimeError("LOOP_NOT_EXECUTED_UNPROVEN")
            if row.not_executed and row.not_executed != not_executed:
                raise RuntimeError("LOOP_ACTION_CONFLICT")
            row.not_executed = not_executed
        else:
            if (row.not_executed or tool is None or tool.state != "COMMITTED" or not tool.committed_result
                    or tool.committed_result_digest != source_digest or row.invocation_id != tool.invocation_id):
                raise RuntimeError("LOOP_RESULT_UNAVAILABLE")
            if projection != {"outcome": tool.committed_result["status"], "content": tool.committed_result["output"]["content"]}:
                raise RuntimeError("LOOP_RESULT_UNAVAILABLE")
            if row.result_projection is not None and (row.result_projection != projection or row.source_digest != source_digest):
                raise RuntimeError("LOOP_ACTION_CONFLICT")
            row.result_projection, row.result_digest, row.source_digest = projection, digest(projection), source_digest
        repo._touch_loop(frame, lease)
        await session.flush()
        return row


async def commit_batch(repo, lease, frame_id, iteration_no, *, resume_generation, row_version):
    async with repo.database.transaction() as session:
        _, frame = await repo._lock_loop(session, lease, frame_id, resume_generation=resume_generation)
        iteration = await session.get(RuntimeLoopIterationRow, (frame_id, iteration_no), with_for_update=True)
        if iteration.state == "OBSERVED":
            return iteration
        repo._check_loop_version(frame, row_version)
        if frame.phase != "WAIT_ACTIONS" or iteration.state != "ACCEPTED":
            raise RuntimeError("LOOP_STATE_INVALID")
        actions = tuple((await session.execute(select(RuntimeLoopActionRow).where(
            RuntimeLoopActionRow.frame_id == frame_id, RuntimeLoopActionRow.iteration_no == iteration_no)
            .order_by(RuntimeLoopActionRow.ordinal).with_for_update())).scalars())
        results, sources = [], []
        if not actions or len(actions) != len(iteration.assistant_record["tool_calls"]):
            raise RuntimeError("LOOP_BATCH_INCOMPLETE")
        for action in actions:
            if action.not_executed:
                tool = await session.get(DurableToolInvocationRow, action.action_id)
                if tool is not None and (tool.state != "PREPARED" or tool.started_at is not None):
                    raise RuntimeError("LOOP_NOT_EXECUTED_UNPROVEN")
                result = {"outcome": "NOT_EXECUTED", **action.not_executed}
                source = digest(action.not_executed)
            else:
                tool = await session.get(DurableToolInvocationRow, action.invocation_id)
                if (tool is None or tool.state != "COMMITTED" or tool.committed_result_digest != action.source_digest
                        or action.result_projection is None or digest(action.result_projection) != action.result_digest):
                    raise RuntimeError("LOOP_BATCH_INCOMPLETE")
                result, source = action.result_projection, action.source_digest
            results.append({"call_id": action.protocol_call_id, "action_id": action.action_id, "result": result})
            sources.append({"action_id": action.action_id, "source_digest": source})
        iteration.observation = {"results": results, "all_read_only": all(a.recipe["read_only"] for a in actions),
            "has_bounded_polling": any(a.recipe["bounded_polling"] for a in actions)}
        iteration.observation_digest = digest(iteration.observation)
        iteration.observation_source_digest = digest(sources)
        frame.committed_observation_cursor += 1
        iteration.observation_cursor, iteration.observed_generation = frame.committed_observation_cursor, resume_generation
        iteration.state, frame.phase = "OBSERVED", "NEED_DECISION"
        repo._touch_loop(frame, lease)
        await session.flush()
        return iteration


async def admit_action(repo, session, lease, frame_id, invocation_id, resume_generation):
    from core.persistence.models import DurableApprovalRow
    from core.runtime.agent_loop.contracts import LoopSuspended
    _,frame=await repo._lock_loop(session,lease,frame_id,resume_generation=resume_generation)
    if frame.schema_version != 2: return
    action=await session.get(RuntimeLoopActionRow,invocation_id,with_for_update=True)
    if action is None or action.not_executed or frame.phase!="WAIT_ACTIONS":
        raise RuntimeError("LOOP_ACTION_BINDING_INVALID")
    siblings=tuple((await session.execute(select(RuntimeLoopActionRow).where(RuntimeLoopActionRow.frame_id==frame_id,
        RuntimeLoopActionRow.iteration_no==action.iteration_no).order_by(RuntimeLoopActionRow.ordinal).with_for_update())).scalars())
    pending=(await session.execute(select(DurableApprovalRow).where(DurableApprovalRow.run_id==lease.run_id,
        DurableApprovalRow.invocation_id.in_([a.action_id for a in siblings if a.action_id!=invocation_id]),
        DurableApprovalRow.state=="PENDING"))).scalars().first()
    if pending is not None: raise LoopSuspended("APPROVAL_PENDING")
    for sibling in siblings:
        if sibling.action_id==invocation_id: continue
        tool=await session.get(DurableToolInvocationRow,sibling.action_id)
        if tool is not None and tool.state=="UNKNOWN": raise LoopSuspended("TOOL_OUTCOME_UNKNOWN")
        failed = sibling.result_projection and sibling.result_projection["outcome"]!="SUCCEEDED"
        failed = failed or tool is not None and tool.state=="COMMITTED" and tool.committed_result and tool.committed_result["status"]!="SUCCEEDED"
        if failed and frame.loop_policy["failure_mode"]=="FAIL_FAST":
            raise LoopSuspended("BATCH_SKIP")


async def recover_v2(repo, session, lease, root):
    from core.persistence.models import RuntimeLoopFrameRow, RuntimeAgentDelegationRow
    frames = tuple((await session.execute(select(RuntimeLoopFrameRow).where(
        RuntimeLoopFrameRow.run_id == lease.run_id).order_by(RuntimeLoopFrameRow.frame_id).with_for_update())).scalars())
    roots = [f for f in frames if f.frame_role == "ROOT"]
    if len(roots) != 1 or len(frames) > 3:
        raise RuntimeError("LOOP_FRAME_MISSING")
    delegations = tuple((await session.execute(select(RuntimeAgentDelegationRow).where(
        RuntimeAgentDelegationRow.run_id == lease.run_id).with_for_update())).scalars())
    children = [f for f in frames if f.frame_role == "CHILD"]
    if len(children) != len(delegations) or any(not any(d.child_frame_id==f.frame_id and d.parent_frame_id==roots[0].frame_id
        and d.delegation_id==f.delegation_id for d in delegations) for f in children):
        raise RuntimeError("DELEGATION_SOURCE_INVALID")
    if roots[0].last_committed_fencing_token == lease.fencing_token:
        return await repo._load_in_session(session, lease.run_id)
    generation = roots[0].resume_generation + 1
    for frame in frames:
        repo._validate_loop_frame(frame)
        frame.resume_generation = generation
        repo._touch_loop(frame, lease)
    repo._settle_recovery_budget(root, lease)
    models = tuple((await session.execute(select(RuntimeModelInvocationRow).where(
        RuntimeModelInvocationRow.run_id == lease.run_id).with_for_update())).scalars())
    abandoned = set()
    for model in models:
        if model.state == "STARTED":
            model.state,model.last_committed_fencing_token="UNKNOWN",lease.fencing_token
            model.version += 1
            if model.purpose == "DECISION": abandoned.add((model.loop_frame_id,model.loop_iteration_no))
    for frame_id, iteration_no in abandoned:
        iteration = await session.get(RuntimeLoopIterationRow,(frame_id,iteration_no),with_for_update=True)
        iteration.model_recovery_count += 1
        if iteration.state == "OPEN" and iteration.model_recovery_count >= 2:
            frame = next(f for f in frames if f.frame_id == frame_id)
            frame.phase,frame.stop_code="STOPPED","LOOP_MODEL_RECOVERY_EXHAUSTED"
    await session.flush()
    return await repo._load_in_session(session, lease.run_id)
