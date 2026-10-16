"""公开合成 aggregate 的真实 producer 向量；不包含模型答案或受控数据。"""

from hashlib import sha256
import json
from pathlib import Path
import sys
from types import SimpleNamespace as Row

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.runtime.agent_loop.evidence import build_loop_evidence, evidence_digest


def public_response(run_id="11111111-1111-4111-8111-111111111111",
                    evaluation_run_id="22222222-2222-4222-8222-222222222222",
                    request_id="33333333-3333-4333-8333-333333333333"):
    digest = lambda text: sha256(text.encode()).hexdigest()
    frame = Row(frame_id="public-loop-frame", step_id="answer", current_iteration=3,
        context_seed={"goal_digest": digest("公开合成多轮主链")}, config_digest=digest("public-config"),
        loop_policy={"max_iterations": 16, "max_model_calls": 32, "max_tool_calls": 16},
        stop_code="FINAL", result_decision_id="decision-3",
        final_delivery_receipt={"event_id": "public-output-event", "cursor": 9, "content_digest": digest("public final")})
    root = Row(run_id=run_id, status="SUCCEEDED", execution_policy="durable-agent-loop.v1",
        stop_reason="COMPLETED", budget_consumed={"model_calls": 3, "tool_calls": 2},
        resume_input={"agent_loop": {"correlation": {"evaluation_run_id": evaluation_run_id,
            "attempt_id": run_id, "request_id": request_id}}})
    iterations, models, tools = [], [], []
    for number in (1, 2, 3):
        action = number < 3
        iterations.append(Row(frame_id=frame.frame_id, iteration_no=number, state="OBSERVED" if action else "CLOSED",
            decision_id=f"decision-{number}", decision_kind="TOOL_CALL" if action else "FINAL",
            accepted_model_attempt=1, accepted_model_attempt_number=number, accepted_generation=0,
            observed_generation=0 if action else None, action_id=f"action-{number}" if action else None,
            action_recipe={"read_only": number == 1} if action else None,
            tool_invocation_id=f"action-{number}" if action else None,
            observation_source_digest=digest(f"result-{number}") if action else None,
            observation_digest=digest(f"observation-{number}") if action else None,
            observation_cursor=number if action else None))
        models.append(Row(attempt_number=1, model_attempt_number=number, state="COMPLETED",
                          loop_frame_id=frame.frame_id, loop_iteration_no=number))
        if action:
            tools.append(Row(invocation_id=f"action-{number}", tool_name=f"public_tool_{number}",
                state="COMMITTED", arguments_digest=digest(f"arguments-{number}"),
                idempotency_key_digest=digest(f"business-key-{number}"),
                committed_result_digest=digest(f"result-{number}"),
                committed_result={"attempt_id": f"tool-attempt-{number}",
                    "provider_receipt": {"provider_receipt_digest": digest(f"receipt-{number}"),
                        "business_operation_key_digest": digest(f"business-key-{number}"), "side_effect_count": 1}}))
    body = build_loop_evidence(Row(root=root, loop_frames=(frame,), loop_iterations=tuple(iterations),
        tool_invocations=tuple(tools), models=tuple(models), terminal_sequence=12))
    return {"protocol_version": "localagent-agent-loop-evaluation-execute.v1", "run_id": run_id,
        "status": "SUCCEEDED", "stop_reason": "COMPLETED", "error_code": None, "safe_message": "public synthetic",
        "capture_status": body["coverage"], "capture_error_code": None, "loop_evidence": body,
        "loop_evidence_digest": evidence_digest(body), "final_answer_capture_status": "COMPLETE",
        "final_answer_capture_error_code": None, "final_answer_evidence": {"schema_version": "final-answer-evidence.v1",
            "evidence_id": f"final-answer://{run_id}", "run_id": run_id, "attempt_id": run_id,
            "media_type": "text/plain; charset=utf-8", "content_sha256": digest("public final"), "content": "public final"}}


if __name__ == "__main__":
    print(json.dumps(public_response(*sys.argv[1:]), ensure_ascii=False, separators=(",", ":")))
