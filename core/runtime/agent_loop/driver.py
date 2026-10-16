"""既有 Step 内的有界推进；所有执行事实先由既有 Repository 提交。"""

import json
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from core.runtime.business_output_schema import schema_matches
from core.runtime.execution_aggregate import canonical_payload_digest
from core.runtime.model_context import ContextBudgetExceededError
from core.runtime.model_invocation import DurableModelLifecycleError, ModelInvocationChainError, restore_loop_model_response
from core.runtime.model_routing import ModelFailureCategory
from core.runtime.step_result import StepResult, ResultContentType
from core.runtime.tool_contract import ToolInvocation, ToolSideEffectKind, ToolSideEffectState
from .context import LoopContextInputV1, pinned_loop_config
from .contracts import LoopDecisionV1, LoopPolicyV1, LoopStopped, LoopSuspended


class AgentLoopDriver:
    """不拥有 Scheduler、Tool/Model 执行或 Run 终态。"""

    def __init__(self, router, coordinator):
        self._router = router
        self._coordinator = coordinator

    def execute(self, claim, run_context):
        repo = run_context.execution_repository
        lease = run_context.durable_lease
        emitter = self._coordinator.event_emitter.for_step(claim.step_id)
        submit = emitter.parent.submit_coroutine_from_worker

        def load():
            return submit(repo.load_loop(lease, claim.step_id))

        def mutate(method, frame, **kw):
            return submit(method(lease, frame.frame_id, resume_generation=frame.resume_generation,
                                 row_version=frame.row_version, **kw))

        def stop(frame, code):
            mutate(repo.stop_loop, frame, stop_code=code)
            raise LoopStopped(code)

        frame, iterations = load()
        if canonical_payload_digest(pinned_loop_config(self._router, claim.preferred_agent, run_context)) != frame.config_digest:
            stop(frame, "LOOP_PINNED_CONFIG_UNAVAILABLE")
        policy = LoopPolicyV1(**frame.loop_policy)
        definition = self._router.agent_registry.resolve(claim.preferred_agent).definition
        run_context.loop_frame_id = frame.frame_id
        run_context.loop_resume_generation = frame.resume_generation
        while True:
            run_context.raise_if_inactive()
            frame, iterations = load()
            if canonical_payload_digest(frame.context_seed) != frame.seed_digest:
                stop(frame, "LOOP_RESULT_UNAVAILABLE")
            if frame.phase == "STOPPED":
                raise LoopStopped(frame.stop_code)
            current = iterations[-1] if iterations else None
            if frame.phase == "NEED_DECISION":
                messages, request_digest = LoopContextInputV1(
                    frame.context_seed, tuple(i for i in iterations if i.decision_id),
                    tuple(i for i in iterations if i.state == "OBSERVED"),
                    frame.committed_observation_cursor,
                ).build()
                observed = tuple(item for item in iterations if item.state == "OBSERVED")
                if len(observed) >= 2:
                    previous, last = observed[-2:]
                    recipe = last.action_recipe
                    registration = run_context.tool_resolution_snapshot.resolve(recipe["tool_name"])
                    if (recipe["read_only"] and previous.action_recipe["read_only"]
                            and registration is not None and not registration.adapter.spec.bounded_polling
                            and previous.action_recipe["tool_name"] == recipe["tool_name"]
                            and previous.action_recipe["arguments_digest"] == recipe["arguments_digest"]
                            and previous.observation_digest == last.observation_digest):
                        stop(frame, "LOOP_NO_PROGRESS")
                if frame.context_policy:
                    from .compaction import prepare_context
                    payload = prepare_context(self._router, frame, iterations, run_context, claim, emitter)
                    messages, request_digest = payload["messages"], payload["messages_digest"]
                    options = payload["options"]
                    frame, iterations = load()
                    current = iterations[-1]
                else:
                    if current is None or current.state != "OPEN":
                        if frame.current_iteration >= policy.max_iterations:
                            stop(frame, "LOOP_ITERATION_LIMIT")
                        mutate(repo.open_loop_iteration, frame, context_input_digest=request_digest,
                               context_cursor=frame.committed_observation_cursor)
                        frame, iterations = load()
                        current = iterations[-1]
                    elif current.context_input_digest != request_digest:
                        stop(frame, "LOOP_RESULT_UNAVAILABLE")
                    run_context.loop_iteration_no = current.iteration_no
                    options = {}
                    if frame.context_seed["protocol_mode"] == "native":
                        options = {"tools": [r.native_function_definition() for r in run_context.tool_resolution_snapshot.registrations()], "tool_choice": "auto"}
                    # 完整 tools schema 与 transcript 一起计入估算，禁止自动截断。
                    estimate = self._router._estimate_messages_tokens(messages)
                    estimate += self._router.context_builder.estimator.estimate(json.dumps(options, ensure_ascii=False))
                    if estimate + self._router.max_tokens > min(p.context_window for p in self._router.model_profiles):
                        stop(frame, "LOOP_CONTEXT_EXHAUSTED")
                try:
                    result = self._router._invoke_model_contract(
                        agent_id=claim.preferred_agent, user_query="", messages=messages,
                        context_requirements=None, run_context=run_context,
                        capability_requirements=claim.capability_requirements,
                        max_tokens=self._router.max_tokens, event_emitter=emitter,
                        generation_options=options, business_definition=definition,
                    )
                except ContextBudgetExceededError:
                    frame, _ = load()
                    stop(frame, "LOOP_CONTEXT_EXHAUSTED")
                except ModelInvocationChainError as exc:
                    if exc.failure_category is not ModelFailureCategory.CONTEXT_LIMIT_EXCEEDED:
                        raise
                    frame, iterations = load()
                    if frame.context_policy:
                        from .compaction import prepare_context
                        prepare_context(self._router, frame, iterations, run_context, claim, emitter, force_compaction=True)
                        continue
                    stop(frame, "LOOP_CONTEXT_EXHAUSTED")
                except DurableModelLifecycleError as exc:
                    run_context.raise_if_inactive()
                    frame, _ = load()
                    code = "LOOP_PINNED_CONFIG_UNAVAILABLE" if "CONFIG" in str(exc) else "LOOP_RESULT_UNAVAILABLE"
                    stop(frame, code)
                try:
                    decision = LoopDecisionV1.parse(result.response, protocol_mode=frame.context_seed["protocol_mode"],
                        synthetic_id=f"call_{frame.frame_id}_{current.iteration_no}")
                    self._accept(repo, lease, frame, current, decision, result, claim, run_context, submit, iterations, definition)
                except LoopStopped as exc:
                    frame, _ = load()
                    stop(frame, exc.stop_code)
                continue
            if current is None or not current.decision_id:
                stop(frame, "LOOP_RESULT_UNAVAILABLE")
            run_context.loop_iteration_no = current.iteration_no
            if frame.phase == "RESULT_READY":
                row = submit(repo.get_loop_model_response(lease, frame.frame_id, current.iteration_no))
                decision = LoopDecisionV1.parse(restore_loop_model_response(row),
                    protocol_mode=frame.context_seed["protocol_mode"], synthetic_id="unused")
                if decision.digest != current.decision_digest or decision.kind != current.decision_kind:
                    stop(frame, "LOOP_RESULT_UNAVAILABLE")
                content_type = ResultContentType.JSON if definition.output_schema is not None else ResultContentType.TEXT
                return StepResult(claim.step_id, claim.preferred_agent, content_type, decision.content, True)
            if frame.phase not in {"DECISION_ACCEPTED", "WAIT_TOOL"}:
                stop(frame, "LOOP_RESULT_UNAVAILABLE")
            image = submit(repo.load(run_context.run_id))
            durable = next((i for i in image.tool_invocations if i.invocation_id == current.action_id), None)
            run_context.bind_loop_tool_invocation(durable)
            if durable is not None and str(durable.state) in {"STARTED", "UNKNOWN"}:
                raise LoopSuspended("TOOL_OUTCOME_UNKNOWN")
            if durable is not None and str(durable.state) == "COMMITTED" and not durable.committed_result:
                stop(frame, "LOOP_RESULT_UNAVAILABLE")
            row = submit(repo.get_loop_model_response(lease, frame.frame_id, current.iteration_no))
            decision = LoopDecisionV1.parse(restore_loop_model_response(row),
                protocol_mode=frame.context_seed["protocol_mode"],
                synthetic_id=f"call_{frame.frame_id}_{current.iteration_no}")
            if decision.kind != "TOOL_CALL" or decision.digest != current.decision_digest:
                stop(frame, "LOOP_RESULT_UNAVAILABLE")
            call = current.assistant_record["tool_calls"][0]
            if (call["id"] != decision.provider_tool_call_id or call["function"]["name"] != decision.tool_name
                    or canonical_payload_digest(json.loads(call["function"]["arguments"])) != canonical_payload_digest(decision.arguments)):
                stop(frame, "LOOP_RESULT_UNAVAILABLE")
            registration = run_context.tool_resolution_snapshot.resolve(decision.tool_name)
            if registration is None:
                stop(frame, "LOOP_PINNED_CONFIG_UNAVAILABLE")
            built = registration.adapter.build_invocation(json.dumps(decision.arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            recipe = current.action_recipe
            if built.arguments_digest != recipe["arguments_digest"]:
                stop(frame, "LOOP_RESULT_UNAVAILABLE")
            spec = registration.adapter.spec_for(built)
            if spec.bounded_polling:
                previous = next((item for item in reversed(iterations[:-1])
                    if item.state == "OBSERVED" and item.action_recipe["tool_name"] == recipe["tool_name"]
                    and item.action_recipe["arguments_digest"] == recipe["arguments_digest"]), None)
                if previous is not None:
                    previous_tool = next((tool for tool in image.tool_invocations if tool.invocation_id == previous.action_id), None)
                    if previous_tool is None or previous_tool.committed_at is None:
                        stop(frame, "LOOP_RESULT_UNAVAILABLE")
                    next_poll = previous_tool.committed_at + timedelta(seconds=spec.min_poll_interval_seconds)
                    if datetime.now(UTC) < next_poll:
                        raise LoopSuspended("BOUNDED_POLL_WAIT", next_recovery_at=next_poll)
            invocation = ToolInvocation.create(tool_name=built.tool_name, arguments=built.arguments,
                invocation_id=current.action_id, idempotency_key=recipe["idempotency_key"],
                resource_key=recipe["resource_key"], requested_timeout_seconds=recipe["requested_timeout_seconds"])
            try:
                transcript = self._router._prepare_answer_messages(
                    claim.preferred_agent, "", run_context=run_context, event_emitter=emitter,
                    base_messages=[], tool_call=(built.tool_name, call["function"]["arguments"]),
                    native_assistant_message=current.assistant_record, validated_invocation=invocation,
                    approval_controller=self._coordinator.tool_approval_controller, business_definition=definition,
                )
            except Exception as exc:
                from core.runtime.tool_execution import ToolExecutionFailed
                if isinstance(exc, ToolExecutionFailed) and exc.error.side_effect_state is ToolSideEffectState.UNKNOWN:
                    raise LoopSuspended("TOOL_OUTCOME_UNKNOWN") from None
                raise
            frame, iterations = load()
            current = iterations[-1]
            image = submit(repo.load(run_context.run_id))
            durable = next((i for i in image.tool_invocations if i.invocation_id == current.action_id), None)
            if durable is None or str(durable.state) != "COMMITTED" or not durable.committed_result_digest:
                stop(frame, "LOOP_RESULT_UNAVAILABLE")
            observation = {"content": transcript[-1]["content"]}
            mutate(repo.commit_loop_observation, frame, iteration_no=current.iteration_no,
                   invocation_id=current.action_id, source_digest=durable.committed_result_digest,
                   observation=observation, observation_digest=canonical_payload_digest(observation))

    def _accept(self, repo, lease, frame, iteration, decision, result, claim, context, submit, iterations, definition):
        recipe = assistant = action_id = None
        if decision.kind == "TOOL_CALL":
            registration = context.tool_resolution_snapshot.resolve(decision.tool_name)
            if registration is None:
                raise LoopStopped("LOOP_DECISION_INVALID")
            try:
                invocation = registration.adapter.build_invocation(json.dumps(decision.arguments, ensure_ascii=False))
                spec = registration.adapter.spec_for(invocation)
            except (ValueError, TypeError, RuntimeError):
                raise LoopStopped("LOOP_DECISION_INVALID") from None
            if any(i.assistant_record and i.assistant_record.get("tool_calls", [{}])[0].get("id") == decision.provider_tool_call_id for i in iterations if i.decision_id):
                raise LoopStopped("LOOP_DECISION_INVALID")
            read_only = spec.side_effect_kind is ToolSideEffectKind.NONE
            matches = [i for i in iterations if i.action_recipe and i.action_recipe["tool_name"] == decision.tool_name and i.action_recipe["arguments_digest"] == invocation.arguments_digest]
            if matches and not read_only:
                raise LoopStopped("LOOP_REPEATED_ACTION")
            if spec.bounded_polling and len(matches) >= spec.max_poll_attempts:
                raise LoopStopped("LOOP_REPEATED_ACTION")
            action_id = uuid5(NAMESPACE_URL, f"{frame.frame_id}:{iteration.iteration_no}:action").hex
            recipe = {"tool_name": decision.tool_name, "arguments_digest": invocation.arguments_digest,
                      "idempotency_key": invocation.idempotency_key or action_id,
                      "resource_key": invocation.resource_key, "requested_timeout_seconds": invocation.requested_timeout_seconds,
                      "read_only": read_only}
            assistant = {"role": "assistant", "content": None, "tool_calls": [{"id": decision.provider_tool_call_id, "type": "function",
                "function": {"name": decision.tool_name, "arguments": json.dumps(decision.arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))}}]}
        elif definition.output_schema is not None:
            try:
                if not schema_matches(definition.output_schema, json.loads(decision.content)):
                    raise ValueError("output schema")
            except (ValueError, TypeError):
                raise LoopStopped("LOOP_DECISION_INVALID") from None
        ref = result.model_attempt_ref or context.loop_model_attempt_ref
        _, attempt, model_attempt = ref.rsplit(":", 2)
        context.raise_if_inactive()
        submit(repo.accept_loop_decision(lease, frame.frame_id, iteration.iteration_no,
            resume_generation=frame.resume_generation, row_version=frame.row_version,
            decision_id=uuid5(NAMESPACE_URL, f"{frame.frame_id}:{iteration.iteration_no}:decision").hex,
            decision_kind=decision.kind, decision_digest=decision.digest,
            model_attempt=(int(attempt), int(model_attempt)), action_id=action_id,
            action_recipe=recipe, assistant_record=assistant))
