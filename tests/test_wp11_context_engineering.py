"""WP11 focused context engineering / structured invocation tests."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from core.agent_router import AgentRouter
from core.runtime.model_context import (
    CANONICAL_SECURITY_INSTRUCTION,
    ContextBudgetExceededError,
    ContextBuildRequest,
    ContextBuilder,
    ContextItem,
    ContextSourceType,
    ContextTrustLevel,
    ConversationTurnGroup,
    build_prompt_identity,
)
from core.runtime.multi_agent_planning import PlanningError
from core.runtime.model_invocation import ModelInvocationResult
from core.runtime.model_selection import ModelProfile, ModelProfileId
from core.runtime.planning import TaskCapabilityRequirements
from core.runtime.trace_contract import set_span_attributes
from core.runtime.tracing import InMemorySpanRecorder
from tests._wp3_fixtures import direct_json


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FakeEstimator:
    def estimate(self, text: str) -> int:
        return len(text.split())


def item(item_id, source, trust, content, priority=10, **kwargs):
    return ContextItem(item_id, source, trust, content, priority, NOW, **kwargs)


def test_final_provider_budget_counts_canonical_security_instruction():
    builder = ContextBuilder()
    with pytest.raises(ContextBudgetExceededError) as raised:
        builder.prepare_final_provider_messages(
            [{"role": "user", "content": "hello"}],
            max_input_tokens=20,
            reserved_output_tokens=1,
        )
    assert raised.value.reason in {"final_provider_messages_exceed_budget", "reserved_output_tokens_invalid"}

    budget = builder.prepare_final_provider_messages(
        [{"role": "user", "content": "hello"}],
        max_input_tokens=1000,
        reserved_output_tokens=10,
    )
    assert budget.messages[0]["role"] == "system"
    assert CANONICAL_SECURITY_INSTRUCTION in budget.messages[0]["content"]
    assert budget.estimated_input_tokens == builder.estimate_messages_tokens(budget.messages)


def test_current_user_request_is_mandatory_and_not_truncated():
    builder = ContextBuilder(FakeEstimator())
    request = ContextBuildRequest(
        "run",
        "agent",
        [
            item("system", ContextSourceType.SYSTEM_INSTRUCTION, ContextTrustLevel.TRUSTED_INSTRUCTION, "sys"),
            item("request", ContextSourceType.CURRENT_USER_REQUEST, ContextTrustLevel.USER_CONTENT, "current request words"),
            item("optional", ContextSourceType.MEMORY_RETRIEVAL, ContextTrustLevel.USER_CONTENT, "optional optional optional"),
        ],
        max_input_tokens=100,
        reserved_output_tokens=1,
    )
    result = builder.build(request)
    request_item = next(x for x in result.included_items if x.item_id == "request")
    assert request_item.content == "current request words"

    overflow = ContextBuildRequest(
        "run",
        "agent",
        [item("request", ContextSourceType.CURRENT_USER_REQUEST, ContextTrustLevel.USER_CONTENT, "one two three four")],
        max_input_tokens=4,
        reserved_output_tokens=1,
    )
    with pytest.raises(ContextBudgetExceededError):
        builder.build(overflow)


def test_history_recent_first_and_oldest_turn_atomic_drop():
    groups = (
        ConversationTurnGroup("g1", ({"role": "user", "content": "u1"}, {"role": "assistant", "content": "a1"}), 2),
        ConversationTurnGroup("g2", ({"role": "user", "content": "u2"}, {"role": "assistant", "content": "a2"}), 2),
        ConversationTurnGroup("g3", ({"role": "user", "content": "u3"}, {"role": "assistant", "content": "a3"}), 2),
    )
    result = ContextBuilder(FakeEstimator()).build(
        ContextBuildRequest(
            "run",
            "agent",
            (),
            max_input_tokens=6,
            reserved_output_tokens=1,
            history_groups=groups,
        )
    )
    assert [m["content"] for m in result.included_history_messages()] == ["u2", "a2", "u3", "a3"]
    assert [row.item_id for row in result.dropped_history_groups] == ["g1"]
    history_drops = [
        record
        for record in result.selection_records
        if record.source_type is ContextSourceType.CHAT_HISTORY
        and record.decision == "drop"
    ]
    assert len(history_drops) == 1
    assert history_drops[0].item_or_group_id.startswith("group:sha256:")


def test_rag_whole_chunk_drop_preserves_citation_and_hash():
    first_text = "one two"
    second_text = "three four"
    first_hash = hashlib.sha256(first_text.encode("utf-8")).hexdigest()
    second_hash = hashlib.sha256(second_text.encode("utf-8")).hexdigest()
    rag1 = item(
        "rag-1",
        ContextSourceType.RAG_DOCUMENT,
        ContextTrustLevel.UNTRUSTED_EXTERNAL,
        first_text,
        600,
        source_ref="source.md",
        citation_id="R1",
        mandatory=False,
        preserve_content=True,
        payload_content_hash=first_hash,
    )
    rag2 = item(
        "rag-2",
        ContextSourceType.RAG_DOCUMENT,
        ContextTrustLevel.UNTRUSTED_EXTERNAL,
        second_text,
        599,
        source_ref="source.md",
        citation_id="R2",
        mandatory=False,
        preserve_content=True,
        payload_content_hash=second_hash,
    )
    result = ContextBuilder(FakeEstimator()).build(
        ContextBuildRequest("run", "agent", (rag1, rag2), max_input_tokens=14, reserved_output_tokens=1)
    )
    included_ids = [x.item_id for x in result.included_items]
    dropped_ids = [x.item_id for x in result.dropped_items]
    assert included_ids == ["rag-1"]
    assert dropped_ids == ["rag-2"]
    assert result.included_items[0].content == first_text
    assert result.included_items[0].citation_id == "R1"
    assert hashlib.sha256(result.included_items[0].content.encode("utf-8")).hexdigest() == first_hash
    assert result.stats.retrieval_selected_count == 2
    assert result.stats.context_accepted_rag_count == 1


def test_memory_whole_record_drop_is_atomic():
    memory = item(
        "mem-1",
        ContextSourceType.MEMORY_RETRIEVAL,
        ContextTrustLevel.USER_CONTENT,
        "one two three four five",
        700,
        source_ref="memory_id",
    )
    result = ContextBuilder(FakeEstimator()).build(
        ContextBuildRequest("run", "agent", (memory,), max_input_tokens=3, reserved_output_tokens=1)
    )
    assert result.included_items == ()
    assert result.dropped_items[0].item_id == "mem-1"
    assert result.stats.memory_supplied_count == 1
    assert result.stats.memory_accepted_count == 0


def test_memory_selection_preserves_upstream_rank_instead_of_item_id_order():
    higher_ranked = item(
        "mem-z",
        ContextSourceType.MEMORY_RETRIEVAL,
        ContextTrustLevel.USER_CONTENT,
        "higher ranked memory",
        700,
        source_ref="memory-z",
    )
    lower_ranked = item(
        "mem-a",
        ContextSourceType.MEMORY_RETRIEVAL,
        ContextTrustLevel.USER_CONTENT,
        "lower ranked memory",
        700,
        source_ref="memory-a",
    )
    result = ContextBuilder(FakeEstimator()).build(
        ContextBuildRequest(
            "run",
            "agent",
            (higher_ranked, lower_ranked),
            max_input_tokens=16,
            reserved_output_tokens=1,
        )
    )

    assert [record.item_id for record in result.included_items] == ["mem-z"]
    assert [record.item_id for record in result.dropped_items] == ["mem-a"]


def test_native_tool_protocol_group_is_not_split_by_final_budget_gate():
    builder = ContextBuilder(FakeEstimator())
    assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "echo", "arguments": "{}"}}],
    }
    tool = {"role": "tool", "tool_call_id": "call-1", "content": "result"}
    with pytest.raises(ContextBudgetExceededError):
        builder.prepare_final_provider_messages(
            [assistant, tool],
            max_input_tokens=8,
            reserved_output_tokens=1,
        )
    budget = builder.prepare_final_provider_messages(
        [assistant, tool],
        max_input_tokens=1000,
        reserved_output_tokens=10,
    )
    assert budget.messages[1] == assistant
    assert budget.messages[2] == tool
    assert budget.estimated_input_tokens >= builder.estimator.estimate(
        json.dumps(assistant["tool_calls"], ensure_ascii=False, sort_keys=True)
    )


def test_structured_tool_json_is_not_character_truncated():
    payload = json.dumps({"value": "x" * 5000})
    output = SimpleNamespace(content_type="json", content=payload)
    assert AgentRouter._bounded_tool_observation(output) == payload
    text_output = SimpleNamespace(content_type="text", content="word " * 1000)
    truncated = AgentRouter._bounded_tool_observation(text_output)
    assert len(truncated) <= 1600
    assert truncated.endswith("...")


def test_prompt_identity_is_stable_and_code_owned():
    first = build_prompt_identity("planner", "1", "planner instruction")
    second = build_prompt_identity("planner", "1", "planner instruction")
    changed = build_prompt_identity("planner", "1", "different planner instruction")
    assert first == second
    assert first.prompt_id == "planner"
    assert first.prompt_version == "1"
    assert first.prompt_digest != changed.prompt_digest
    assert len(first.prompt_digest) == 64


def _structured_stub(outputs):
    router = object.__new__(AgentRouter)
    router.context_builder = ContextBuilder()
    router.model_context_window = 4096
    router.max_tokens = 256
    router.structured_invocation_repair_enabled = True
    calls = {"count": 0}

    def invoke(**_kwargs):
        calls["count"] += 1
        return SimpleNamespace(output=outputs.pop(0))

    router._invoke_model_contract = invoke
    return router, calls


def test_structured_valid_output_succeeds_without_repair():
    router, calls = _structured_stub([direct_json()])
    assert router.complete_planning_decision("question", run_context=None)
    assert calls["count"] == 1


def test_structured_malformed_output_repairs_once_then_succeeds():
    router, calls = _structured_stub(["not-json", direct_json()])
    assert router.complete_planning_decision("question", run_context=None)
    assert calls["count"] == 2


def test_structured_malformed_twice_typed_fail():
    router, calls = _structured_stub(["not-json", "still-not-json"])
    with pytest.raises(PlanningError):
        router.complete_planning_decision("question", run_context=None)
    assert calls["count"] == 2


def test_benign_context_includes_system_request_rag_memory_and_history():
    rag_text = "rag content"
    rag_hash = hashlib.sha256(rag_text.encode("utf-8")).hexdigest()
    result = ContextBuilder(FakeEstimator()).build(
        ContextBuildRequest(
            "run",
            "agent",
            (
                item("sys", ContextSourceType.SYSTEM_INSTRUCTION, ContextTrustLevel.TRUSTED_INSTRUCTION, "system"),
                item("user", ContextSourceType.CURRENT_USER_REQUEST, ContextTrustLevel.USER_CONTENT, "request"),
                item("mem", ContextSourceType.MEMORY_RETRIEVAL, ContextTrustLevel.USER_CONTENT, "memory", 700),
                item(
                    "rag",
                    ContextSourceType.RAG_DOCUMENT,
                    ContextTrustLevel.UNTRUSTED_EXTERNAL,
                    rag_text,
                    600,
                    source_ref="source.md",
                    citation_id="R1",
                    preserve_content=True,
                    payload_content_hash=rag_hash,
                ),
            ),
            max_input_tokens=1000,
            reserved_output_tokens=10,
            history_groups=(
                ConversationTurnGroup("g1", ({"role": "user", "content": "old"}, {"role": "assistant", "content": "answer"}), 2),
            ),
        )
    )
    included_ids = {x.item_id for x in result.included_items}
    assert {"sys", "user", "mem", "rag"}.issubset(included_ids)
    assert [m["content"] for m in result.included_history_messages()] == ["old", "answer"]
    assert all(record.decision in {"keep", "truncate", "drop"} for record in result.selection_records)
    assert all("/" not in record.item_or_group_id for record in result.selection_records)
    assert all(
        not record.safe_provenance_id_or_digest
        or record.safe_provenance_id_or_digest.startswith("provenance:sha256:")
        for record in result.selection_records
    )


def test_model_capability_and_provider_evidence_are_explicit():
    profile = ModelProfile(
        ModelProfileId.REMOTE_ADVANCED,
        8192,
        512,
        True,
        True,
        True,
        True,
        2,
        2,
        supports_native_tool_calling=True,
        supports_provider_structured_output=True,
        provider_kind="deepseek",
        model_identity="deepseek-chat",
    )
    assert profile.supports_native_tool_calling is True
    assert profile.supports_provider_structured_output is True
    assert profile.provider_kind == "deepseek"
    assert profile.model_identity == "deepseek-chat"

    result = ModelInvocationResult(
        "output",
        None,
        None,
        ModelProfileId.REMOTE_ADVANCED,
        (),
        False,
        provider_kind=profile.provider_kind,
        model_identity=profile.model_identity,
        prompt_id="planner",
        prompt_version="1",
        prompt_digest="a" * 64,
        estimated_input_tokens=10,
        reserved_output_tokens=5,
        context_budget_utilization=0.5,
        selected_context_items=2,
        dropped_context_items=1,
        structured_repair_count=1,
    )
    assert result.provider_kind == "deepseek"
    assert result.model_identity == "deepseek-chat"
    assert result.prompt_id == "planner"
    assert result.prompt_digest == "a" * 64
    assert result.structured_repair_count == 1


def test_context_prompt_and_provider_evidence_are_accepted_by_trace_allowlist():
    recorder = InMemorySpanRecorder()
    handle = recorder.start_span(
        trace_id="trace-wp11",
        run_id="run-wp11",
        component="model_invocation",
        operation="invoke",
    )
    set_span_attributes(
        handle,
        prompt_id="planner",
        prompt_version="1",
        prompt_digest="a" * 64,
        provider_kind="deepseek",
        model_identity="deepseek-chat",
        estimated_input_tokens=10,
        reserved_output_tokens=5,
        context_budget_utilization=0.5,
        selected_context_items=2,
        dropped_context_items=1,
        structured_repair_count=1,
    )
    record = handle.end_ok()

    assert record is not None
    assert record.attributes["prompt_id"] == "planner"
    assert record.attributes["provider_kind"] == "deepseek"
    assert record.attributes["selected_context_items"] == 2
    assert record.attributes["structured_repair_count"] == 1


def test_agent_router_recent_history_query_and_reorder():
    seen = {}

    class FakeMemory:
        def get_chat_history(self, **kwargs):
            seen.update(kwargs)
            return [
                {"id": 4, "role": "assistant", "content": "a2"},
                {"id": 3, "role": "user", "content": "u2"},
                {"id": 2, "role": "assistant", "content": "a1"},
                {"id": 1, "role": "user", "content": "u1"},
            ]

    router = object.__new__(AgentRouter)
    router.memory_manager = FakeMemory()
    rows = router._get_recent_history("core_router", limit=4, memory_scope="direct")
    assert seen["ascending"] is False
    assert seen["limit"] == 4
    assert [row["content"] for row in rows] == ["u1", "a1", "u2", "a2"]


def test_history_window_drops_orphan_leading_assistant_before_grouping():
    router = object.__new__(AgentRouter)
    router.context_builder = ContextBuilder(FakeEstimator())

    groups = router._group_history(
        [
            {"role": "assistant", "content": "orphaned-by-window"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
        ]
    )

    assert len(groups) == 1
    assert [message["content"] for message in groups[0].messages] == ["u2", "a2"]
