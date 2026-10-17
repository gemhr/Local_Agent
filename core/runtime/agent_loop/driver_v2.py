"""Batch/Handoff 的 Step 内驱动；根调度、工具与终态保持原 Owner。"""
import json

from core.runtime.business_output_schema import schema_matches
from core.runtime.execution_aggregate import canonical_payload_digest as digest
from core.runtime.model_invocation import restore_loop_model_response, ModelInvocationChainError
from core.runtime.model_routing import ModelFailureCategory
from core.runtime.step_result import StepResult, ResultContentType
from .contracts import LoopDecisionV2, LoopStopped, LoopPolicyV2
from .context import pinned_loop_config
from .compaction import prepare_context
from .batch import prepare_batch, advance_batch


class AgentLoopDriverV2:
    def __init__(self, router, coordinator):
        self.router, self.coordinator = router, coordinator

    def execute(self, claim, context, *, frame_id=None):
        repo, lease = context.execution_repository, context.durable_lease
        emitter = self.coordinator.event_emitter.for_step(claim.step_id)
        submit = emitter.parent.submit_coroutine_from_worker
        load = lambda: submit(repo.load_loop(lease, claim.step_id, frame_id))
        frame, iterations = load()
        if frame.schema_version != 2 or digest(pinned_loop_config(self.router, claim.preferred_agent, context)) != frame.config_digest:
            raise LoopStopped("LOOP_PINNED_CONFIG_UNAVAILABLE")
        policy = LoopPolicyV2(**frame.loop_policy)
        definition = self.router.agent_registry.resolve(claim.preferred_agent).definition
        context.loop_frame_id, context.loop_resume_generation = frame.frame_id, frame.resume_generation
        while True:
            context.raise_if_inactive()
            frame, iterations = load()
            if frame.phase == "STOPPED":
                raise LoopStopped(frame.stop_code)
            current = iterations[-1] if iterations else None
            if frame.phase == "NEED_DECISION":
                if frame.current_iteration >= policy.max_iterations and (current is None or current.state != "OPEN"):
                    raise LoopStopped("LOOP_ITERATION_LIMIT")
                if context.memory_guard is not None:
                    context.memory_guard.check(submit, seed_digest=frame.seed_digest if frame.frame_role == "ROOT" else None)
                observed = [i for i in iterations if i.state == "OBSERVED"]
                if len(observed) >= 2:
                    left, right = observed[-2:]
                    if (not right.observation.get("has_bounded_polling") and left.observation.get("all_read_only") and right.observation.get("all_read_only")
                            and [r["result"] for r in left.observation["results"]] == [r["result"] for r in right.observation["results"]]
                            and [c["function"] for c in left.assistant_record["tool_calls"]] == [c["function"] for c in right.assistant_record["tool_calls"]]):
                        raise LoopStopped("LOOP_NO_PROGRESS")
                payload = prepare_context(self.router, frame, iterations, context, claim, emitter)
                frame, iterations = load()
                current = iterations[-1]
                try:
                    result = self.router._invoke_model_contract(agent_id=claim.preferred_agent, user_query="",
                        messages=payload["messages"], context_requirements=None, run_context=context,
                        capability_requirements=claim.capability_requirements, max_tokens=self.router.max_tokens,
                        event_emitter=emitter, generation_options=payload["options"], business_definition=definition)
                except ModelInvocationChainError as exc:
                    if exc.failure_category is not ModelFailureCategory.CONTEXT_LIMIT_EXCEEDED:
                        raise
                    frame, iterations = load()
                    prepare_context(self.router, frame, iterations, context, claim, emitter, force_compaction=True)
                    continue
                decision = LoopDecisionV2.parse(result.response, protocol_mode=frame.context_seed["protocol_mode"],
                    synthetic_id=f"call_{frame.frame_id}_{current.iteration_no}", handoff_targets=frame.pinned_config.get("delegation_targets", {}))
                recipes, assistant = prepare_batch(frame, current, decision, context) if decision.kind == "TOOL_BATCH" else ([], None)
                if decision.kind in {"FINAL", "REFUSAL"} and definition.output_schema is not None:
                    try:
                        if not schema_matches(definition.output_schema, json.loads(decision.content)):
                            raise ValueError("schema")
                    except (TypeError, ValueError):
                        raise LoopStopped("LOOP_DECISION_INVALID") from None
                ref = result.model_attempt_ref or context.loop_model_attempt_ref
                _, attempt, model_attempt = ref.rsplit(":", 2)
                context.raise_if_inactive()
                delegation = None
                if decision.kind == "HANDOFF":
                    from .handoff import prepare_handoff
                    delegation = prepare_handoff(self.router, frame, decision, context)
                submit(repo.accept_loop_batch(lease, frame.frame_id, current.iteration_no,
                    resume_generation=frame.resume_generation, row_version=frame.row_version, decision=decision,
                    model_attempt=(int(attempt), int(model_attempt)), recipes=recipes, assistant_record=assistant, delegation=delegation))
                if delegation is not None:
                    row, _ = submit(repo.load_delegation(lease, frame.frame_id, current.iteration_no))
                    from core.runtime.budget import BudgetUsage
                    context.budget_ledger.allocate_delegation(row.delegation_id, BudgetUsage(**row.frozen["allocation"], _allow_independent_total=True))
                continue
            if current is None or current.decision_id is None:
                raise LoopStopped("LOOP_RESULT_UNAVAILABLE")
            context.loop_iteration_no = current.iteration_no
            if frame.phase == "WAIT_ACTIONS":
                advance_batch(self.router, self.coordinator, claim, context, frame, current, submit)
                continue
            if frame.phase == "WAIT_CHILD":
                from .handoff import advance_handoff
                advance_handoff(self, claim, context, frame, current, submit)
                continue
            if frame.phase == "RESULT_READY":
                row = submit(repo.get_loop_model_response(lease, frame.frame_id, current.iteration_no))
                decision = LoopDecisionV2.parse(restore_loop_model_response(row), protocol_mode=frame.context_seed["protocol_mode"], synthetic_id="unused")
                if decision.digest != current.decision_digest or decision.kind not in {"FINAL", "REFUSAL"}:
                    raise LoopStopped("LOOP_RESULT_UNAVAILABLE")
                content_type = ResultContentType.JSON if definition.output_schema is not None else ResultContentType.TEXT
                return StepResult(claim.step_id, claim.preferred_agent, content_type, decision.content, True)
            raise LoopStopped("LOOP_STATE_INVALID")
