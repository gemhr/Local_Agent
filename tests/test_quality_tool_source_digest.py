"""公开合成持久记录；不连接数据库、不改写历史 Receipt。"""
from copy import deepcopy
from types import SimpleNamespace as NS

import pytest

from core.runtime.agent_loop.quality_evidence import observations
from core.runtime.execution_aggregate import canonical_payload_digest
from core.runtime.tool_contract import canonical_json_digest


@pytest.mark.parametrize("mutation", [None, "result", "arguments", "missing"])
def test_tool_owner_digest_and_source_gaps(mutation):
    result = {"status": "SUCCEEDED", "output": {"content": "公开合成：中文结果 ✓"}}
    arguments = {"query": "合成查询"}
    digest = canonical_json_digest(arguments)
    tool = NS(invocation_id="tool", state="COMMITTED", arguments_digest=digest,
              committed_result=deepcopy(result), committed_result_digest=canonical_json_digest(result),
              resource_key_digest=None)
    recipe = dict(tool_name="synthetic_read", arguments=arguments, arguments_digest=digest, read_only=True)
    if mutation == "result":
        tool.committed_result["output"]["content"] = "已篡改"
    if mutation == "arguments":
        recipe["arguments"] = {"query": "已篡改"}
        recipe["arguments_digest"] = canonical_json_digest(recipe["arguments"])
    image = NS(loop_frames=[NS(frame_id="frame", pinned_config={})], context_snapshots=[], models=[],
               tool_invocations=[] if mutation == "missing" else [tool],
               loop_iterations=[NS(frame_id="frame", iteration_no=1, decision_kind="TOOL_CALL",
                   assistant_record={}, action_id="action", tool_invocation_id="tool",
                   action_recipe=recipe, observation_digest=None)],
               root=NS(budget_consumed={}, budget_reserved={}, budget_operations={}))
    receipt = NS(capture_profile={"tool_aliases": {"synthetic_read": "read"},
        "parameter_paths": {}, "fact_keys": []}, evaluation_capture={})
    actual = observations(image, [], [], None, receipt)
    assert canonical_payload_digest(result) != canonical_json_digest(result)
    assert ("TOOL_SOURCE_INVALID" in actual["gaps"]) == (mutation in {"result", "arguments"})
    assert "MEMORY_BINDING_MISSING" in actual["gaps"]
    assert actual["coverage"] == "PARTIAL"
    if mutation == "missing":
        assert "TOOL_OUTCOME_UNCONFIRMED" in actual["gaps"]
