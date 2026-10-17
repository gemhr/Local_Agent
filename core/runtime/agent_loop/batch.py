"""已接受批次的有限推进；工具执行、许可、审批和结果仍归 ToolExecutionService。"""
import asyncio
from copy import copy
from datetime import UTC, datetime, timedelta
import json

from core.runtime.execution_aggregate import canonical_payload_digest as digest
from core.runtime.tool_contract import ToolInvocation, ToolSideEffectKind
from core.runtime.tool_execution import _validate_invocation_against_spec
from core.runtime.business_output_schema import schema_matches
from core.runtime.snapshot_serialization import to_primitive
from .contracts import LoopStopped, LoopSuspended
from core.persistence.repositories.agent_loop import identity


def prepare_batch(frame, iteration, decision, context):
    recipes, calls = [], []
    for ordinal, call in enumerate(decision.calls):
        registration = context.tool_resolution_snapshot.resolve(call["tool_name"])
        if registration is None:
            raise LoopStopped("LOOP_DECISION_INVALID")
        try:
            built = registration.adapter.build_invocation(json.dumps(call["arguments"], ensure_ascii=False, allow_nan=False))
            spec = registration.adapter.spec_for(built)
            if built.tool_name != call["tool_name"] or not schema_matches(registration.adapter.llm_input_schema(), call["arguments"]):
                raise ValueError("tool identity")
        except (ValueError, TypeError, RuntimeError):
            raise LoopStopped("LOOP_DECISION_INVALID") from None
        action_id = identity(frame.frame_id, iteration.iteration_no, f"action:{ordinal}")
        try:
            _validate_invocation_against_spec(ToolInvocation.create(tool_name=built.tool_name, arguments=built.arguments,
                invocation_id=action_id, idempotency_key=built.idempotency_key or action_id, resource_key=built.resource_key), spec)
        except ValueError:
            raise LoopStopped("LOOP_DECISION_INVALID") from None
        recipes.append({"tool_name": built.tool_name, "arguments": call["arguments"], "arguments_digest": built.arguments_digest,
            "read_only": spec.side_effect_kind is ToolSideEffectKind.NONE,
            "concurrent_safe": spec.side_effect_kind is ToolSideEffectKind.NONE and spec.max_concurrency >= 2,
            "spec_digest": digest(to_primitive(spec)), "bounded_polling": spec.bounded_polling,
            "max_poll_attempts": spec.max_poll_attempts, "min_poll_interval_seconds": spec.min_poll_interval_seconds,
            "idempotency_key": built.idempotency_key or action_id, "resource_key": built.resource_key,
            "requested_timeout_seconds": built.requested_timeout_seconds})
        calls.append({"id": call["call_id"], "type": "function", "function": {
            "name": built.tool_name, "arguments": json.dumps(call["arguments"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))}})
    if len(recipes) > frame.loop_policy["max_batch_size"] or sum(not r["read_only"] for r in recipes) > 1:
        raise LoopStopped("LOOP_DECISION_INVALID")
    return recipes, {"role": "assistant", "content": None, "tool_calls": calls}


def advance_batch(router, coordinator, claim, context, frame, iteration, submit):
    repo, lease = context.execution_repository, context.durable_lease
    actions = submit(repo.load_loop_actions(lease, frame.frame_id, iteration.iteration_no))
    image = submit(repo.load(context.run_id))
    tools = {t.invocation_id: t for t in image.tool_invocations}
    for action in actions:
        tool = tools.get(action.action_id)
        if tool is not None and tool.state in {"STARTED", "UNKNOWN"}:
            raise LoopSuspended("TOOL_OUTCOME_UNKNOWN")
    failed = next((a.action_id for a in actions if a.result_projection and a.result_projection["outcome"] != "SUCCEEDED"), None)
    readonly_failed = failed

    def execute(action):
        local = copy(context)
        local.bind_loop_tool_invocation(tools.get(action.action_id))
        recipe = action.recipe
        if digest(recipe) != action.recipe_digest:
            raise LoopStopped("LOOP_RESULT_UNAVAILABLE")
        registration = local.tool_resolution_snapshot.resolve(recipe["tool_name"])
        if registration is None:
            raise LoopStopped("LOOP_PINNED_CONFIG_UNAVAILABLE")
        built = registration.adapter.build_invocation(json.dumps(recipe["arguments"], ensure_ascii=False))
        if built.arguments_digest != recipe["arguments_digest"]:
            raise LoopStopped("LOOP_RESULT_UNAVAILABLE")
        if digest(to_primitive(registration.adapter.spec_for(built))) != recipe["spec_digest"]:
            raise LoopStopped("LOOP_PINNED_CONFIG_UNAVAILABLE")
        if recipe["bounded_polling"] and (action.action_id not in tools or tools[action.action_id].state != "COMMITTED"):
            latest = submit(repo.load(context.run_id))
            polls = [t.committed_at for t in latest.tool_invocations if t.invocation_id != action.action_id
                and t.tool_name == recipe["tool_name"] and t.arguments_digest == recipe["arguments_digest"] and t.state == "COMMITTED" and t.committed_at]
            if polls:
                next_poll = max(polls) + timedelta(seconds=recipe["min_poll_interval_seconds"])
                if datetime.now(UTC) < next_poll:
                    raise LoopSuspended("BOUNDED_POLL_WAIT", next_recovery_at=next_poll)
        invocation = ToolInvocation.create(tool_name=built.tool_name, arguments=built.arguments, invocation_id=action.action_id,
            idempotency_key=recipe["idempotency_key"], resource_key=recipe["resource_key"],
            requested_timeout_seconds=recipe["requested_timeout_seconds"])
        # 只传当前调用的 assistant，防止旧单工具适配器读取批次的第一个 call。
        call = iteration.assistant_record["tool_calls"][action.ordinal]
        error = None
        try:
            router._prepare_answer_messages(claim.preferred_agent, "", run_context=local,
                event_emitter=coordinator.event_emitter.for_step(claim.step_id), base_messages=[],
                tool_call=(built.tool_name, call["function"]["arguments"]),
                native_assistant_message={"role": "assistant", "content": None, "tool_calls": [call]},
                validated_invocation=invocation, approval_controller=coordinator.tool_approval_controller,
                business_definition=router.agent_registry.resolve(claim.preferred_agent).definition)
        except Exception as exc:
            error = exc
        latest = submit(repo.load(context.run_id))
        tool = next((t for t in latest.tool_invocations if t.invocation_id == action.action_id), None)
        if tool is not None and tool.state in {"STARTED", "UNKNOWN"}:
            raise LoopSuspended("TOOL_OUTCOME_UNKNOWN") from None
        if tool is None or tool.state != "COMMITTED" or not tool.committed_result:
            if isinstance(error, LoopSuspended) and error.reason == "BATCH_SKIP":
                rows = submit(repo.load_loop_actions(lease, frame.frame_id, iteration.iteration_no))
                failures = {t.invocation_id for t in latest.tool_invocations if t.state == "COMMITTED"
                    and t.committed_result and t.committed_result["status"] != "SUCCEEDED"}
                cause = next(a.action_id for a in rows if a.action_id in failures)
                submit(repo.record_loop_action(lease, frame.frame_id, iteration.iteration_no,
                    resume_generation=frame.resume_generation, action_id=action.action_id,
                    not_executed={"reason": "BATCH_SKIP", "cause_action_id": cause}))
                return cause
            if error is not None:
                raise error
            raise LoopSuspended("WAIT_TOOL")
        payload = tool.committed_result
        projection = {"outcome": payload["status"], "content": payload["output"]["content"]}
        submit(repo.record_loop_action(lease, frame.frame_id, iteration.iteration_no,
            resume_generation=frame.resume_generation, action_id=action.action_id,
            projection=projection, source_digest=tool.committed_result_digest))
        return action.action_id if projection["outcome"] != "SUCCEEDED" else None

    pending = [a for a in actions if not a.result_projection and not a.not_executed]
    ordered = [a for a in pending if a.recipe["read_only"]] + [a for a in pending if not a.recipe["read_only"]]
    while ordered:
        action = ordered.pop(0)
        cause = failed if frame.loop_policy["failure_mode"] == "FAIL_FAST" else None
        if not action.recipe["read_only"]:
            cause = cause or readonly_failed
        if cause:
            submit(repo.record_loop_action(lease, frame.frame_id, iteration.iteration_no,
                resume_generation=frame.resume_generation, action_id=action.action_id,
                not_executed={"reason": "BATCH_SKIP" if frame.loop_policy["failure_mode"] == "FAIL_FAST" else "READ_PRECONDITION_FAILED", "cause_action_id": cause}))
            continue
        wave = [action]
        if ordered and frame.loop_policy["max_batch_concurrency"] >= 2 and action.recipe["concurrent_safe"]:
            other = ordered[0]
            if other.recipe["concurrent_safe"] and (not action.recipe["resource_key"] or action.recipe["resource_key"] != other.recipe["resource_key"]):
                wave.append(ordered.pop(0))
        if len(wave) == 1:
            outcomes = [execute(action)]
        else:
            # 只协调两个调用；底层线程、资源许可、超时与实际 I/O 仍由原 Tool Runtime 管理。
            async def run_wave():
                return await asyncio.gather(*(asyncio.to_thread(execute, a) for a in wave), return_exceptions=True)
            results = submit(run_wave())
            errors = [r for r in results if isinstance(r, BaseException)]
            if errors:
                raise next((e for e in errors if isinstance(e, LoopSuspended)), errors[0])
            outcomes = results
        failed = failed or next((o for o in outcomes if o), None)
        if action.recipe["read_only"]:
            readonly_failed = readonly_failed or failed
    latest, _ = submit(repo.load_loop(lease, claim.step_id, frame.frame_id))
    submit(repo.commit_loop_batch(lease, frame.frame_id, iteration.iteration_no,
        resume_generation=frame.resume_generation, row_version=latest.row_version))
