"""Loop 模型接缝单测；真实 PG fencing/恢复由 Loop 集成验收验证。"""

from types import SimpleNamespace

from core.runtime.budget import BudgetUsage
from core.runtime.context import create_run_context
from core.runtime.execution_aggregate import canonical_payload_digest
from core.runtime.model_invocation import ModelAdapterResponse
from core.runtime.agent_loop.contracts import LoopDecisionV1, LoopStopped
import pytest
from core.runtime.model_routing import ModelFailureCategory
from tests.test_model_invocation import RecordingAdapter, provider_error
from tests.test_model_invocation_durable import _DurableRepository, _invoke


class LoopRepository(_DurableRepository):
    async def start_model_attempt(self, lease, **kwargs):
        await super().start_model_attempt(lease, **kwargs)
        row = self.rows[-1]
        for name in ("loop_frame_id", "loop_iteration_no", "invocation_group", "resume_generation", "provider_kind", "profile_identity"):
            setattr(row, name, kwargs[name])
        row.reserved_usage = kwargs["reserved_usage"]
        row.operation_id = kwargs["operation_id"]

    async def finish_model_attempt(self, lease, **kwargs):
        await super().finish_model_attempt(lease, **kwargs)
        row = next(row for row in self.rows if row.model_attempt_number == kwargs["model_attempt"])
        row.result_digest = canonical_payload_digest(row.result_binding) if row.result_binding else None
        row.actual_usage = kwargs["actual_usage"]
        row.usage_source = kwargs["usage_source"]
        assert kwargs["operation_id"] == row.operation_id


def test_structured_decision_union_is_strict_and_does_not_guess_refusal():
    for raw, kind in (( '{"kind":"FINAL","content":"done"}', "FINAL"),
                      ('{"kind":"TOOL_CALL","tool_name":"lookup","arguments":{}}', "TOOL_CALL"),
                      ('{"kind":"REFUSAL","reason_code":"SAFETY","content":"declined"}', "REFUSAL")):
        decision = LoopDecisionV1.parse(ModelAdapterResponse(raw, finish_reason="stop", tool_call_count=0),
            protocol_mode="structured", synthetic_id="stable-call")
        assert decision.kind == kind
    for raw in ('{"kind":"FINAL","kind":"TOOL_CALL","content":"done"}',
                '{"kind":"FINAL","content":"done","tool_name":"lookup"}',
                '{"kind":"TOOL_CALL_BATCH","calls":[]}'):
        with pytest.raises(LoopStopped, match="LOOP_DECISION_INVALID"):
            LoopDecisionV1.parse(ModelAdapterResponse(raw, finish_reason="stop", tool_call_count=0),
                protocol_mode="structured", synthetic_id="stable-call")


def test_loop_attempt_binding_budget_metadata_and_completed_winner_recovery():
    repository = LoopRepository()
    context, _ = create_run_context(entry_agent_id="test")
    context.execution_policy = "durable-agent-loop.v1"
    context.loop_frame_id = "frame-model"
    context.loop_iteration_no = 1
    context.loop_resume_generation = 0
    adapter = RecordingAdapter([
        provider_error(ModelFailureCategory.TRANSIENT_PROVIDER_FAILURE),
        ModelAdapterResponse("refused", actual_usage=BudgetUsage(input_tokens=1, output_tokens=2, total_tokens=3), finish_reason="stop", refusal="refused", tool_call_count=0),
    ])
    result = _invoke(context, repository, adapter, retry=True)
    assert result.model_attempt_ref == "answer:1:2"
    assert {row.invocation_group for row in repository.rows} == {"frame-model:1"}
    assert [row.actual_usage["model_calls"] for row in repository.rows] == [1, 1]
    assert repository.rows[-1].actual_usage["retries"] == 1
    assert context.budget_ledger.snapshot().committed_usage.model_calls == 2
    assert context.budget_ledger.snapshot().committed_usage.retries == 1

    recovered, _ = create_run_context(entry_agent_id="test", run_id=context.run_id)
    recovered.execution_policy = "durable-agent-loop.v1"
    recovered.loop_frame_id = "frame-model"
    recovered.loop_iteration_no = 1
    recovered.loop_resume_generation = 1
    restored = _invoke(recovered, repository, RecordingAdapter([]))
    assert restored.model_attempt_ref == result.model_attempt_ref
    assert restored.response.refusal == "refused"
    assert restored.response.finish_reason == "stop"
    assert restored.response.tool_call_count == 0
    assert len(repository.rows) == 2
    assert recovered.budget_ledger.snapshot().committed_usage.model_calls == 0
