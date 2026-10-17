"""Loop 的版本、有限策略与严格模型决策合同。"""

from dataclasses import asdict, dataclass
import json
from typing import Mapping

from core.runtime.execution_aggregate import canonical_payload_digest

LOOP_POLICY = "durable-agent-loop.v1"
LOOP_POLICY_V2 = "durable-agent-loop.v2"
HANDOFF_FUNCTION = "__agent_handoff_v1"


class LoopStopped(RuntimeError):
    def __init__(self, stop_code: str):
        self.stop_code = stop_code
        super().__init__(stop_code)


class LoopSuspended(RuntimeError):
    """等待原审批/工具 Owner 推进；不得提交 Step 或 Run 终态。"""

    def __init__(self, reason: str = "WAIT_TOOL", *, next_recovery_at=None):
        self.reason = reason
        self.next_recovery_at = next_recovery_at
        super().__init__(reason)


@dataclass(frozen=True)
class LoopPolicyV1:
    max_iterations: int = 16
    max_model_calls: int = 32
    max_tool_calls: int = 16
    max_retries: int = 2
    max_elapsed_seconds: int = 300

    def __post_init__(self):
        for key, ceiling in (("max_iterations", 64), ("max_model_calls", 32),
                             ("max_tool_calls", 16), ("max_retries", 2),
                             ("max_elapsed_seconds", 300)):
            value = getattr(self, key)
            if type(value) is not int or not (0 if key == "max_retries" else 1) <= value <= ceiling:
                raise ValueError(f"invalid loop limit: {key}")

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class LoopPolicyV2(LoopPolicyV1):
    max_batch_size: int = 4
    max_batch_concurrency: int = 2
    failure_mode: str = "FAIL_FAST"
    max_child_depth: int = 1
    max_delegations: int = 2
    max_active_children: int = 1
    total_tokens: int = 65536
    cost_units: int = 65536

    def __post_init__(self):
        super().__post_init__()
        for key, ceiling in (("max_batch_size", 4), ("max_batch_concurrency", 2),
                             ("max_child_depth", 1), ("max_delegations", 2), ("max_active_children", 1),
                             ("total_tokens", 1000000), ("cost_units", 1000000)):
            if type(getattr(self, key)) is not int or not 1 <= getattr(self, key) <= ceiling:
                raise ValueError(f"invalid loop limit: {key}")
        if self.failure_mode not in {"FAIL_FAST", "BEST_EFFORT"}:
            raise ValueError("invalid batch failure mode")


@dataclass(frozen=True)
class LoopFrame:
    frame_id: str
    run_id: str
    step_id: str
    phase: str = "NEED_DECISION"
    current_iteration: int = 0
    committed_observation_cursor: int = 0
    resume_generation: int = 0
    row_version: int = 1


@dataclass(frozen=True)
class LoopIteration:
    frame_id: str
    iteration_no: int
    context_input_digest: str
    context_cursor: int
    state: str = "OPEN"
    decision_id: str | None = None
    action_id: str | None = None


def strict_object(raw: str) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON")))
    if not isinstance(value, dict):
        raise ValueError("object required")
    return value


@dataclass(frozen=True)
class LoopDecisionV1:
    kind: str
    content: str | None = None
    tool_name: str | None = None
    arguments: Mapping | None = None
    provider_tool_call_id: str | None = None
    reason_code: str | None = None

    @property
    def digest(self):
        return canonical_payload_digest(asdict(self))

    @classmethod
    def parse(cls, response, *, protocol_mode: str, synthetic_id: str):
        try:
            if response is None or getattr(response, "native_tool_calls", None) or getattr(response, "finish_reason", None) not in {"stop", "tool_calls", "end_turn"} or (getattr(response, "tool_call_count", None) or 0) > 1:
                raise ValueError("incomplete response")
            call = response.native_tool_call
            refusal = getattr(response, "refusal", None)
            count = getattr(response, "tool_call_count", None)
            if count is not None and (type(count) is not int or count != (1 if call is not None else 0)):
                raise ValueError("tool count mismatch")
            if protocol_mode == "native":
                if call is not None:
                    if refusal or response.output.strip():
                        raise ValueError("contradictory decision")
                    args = strict_object(call.arguments_json)
                    expected = {"id": call.provider_tool_call_id, "type": "function",
                                "function": {"name": call.tool_name, "arguments": call.arguments_json}}
                    assistant = response.assistant_message
                    if not call.provider_tool_call_id or assistant is None or assistant.get("role") != "assistant" or assistant.get("tool_calls") != [expected]:
                        raise ValueError("native binding mismatch")
                    return cls("TOOL_CALL", tool_name=call.tool_name, arguments=args,
                               provider_tool_call_id=call.provider_tool_call_id)
                if (response.finish_reason == "tool_calls"
                        or (response.assistant_message and response.assistant_message.get("tool_calls"))):
                    raise ValueError("missing native call")
                if refusal:
                    if response.output.strip() or not isinstance(refusal, str) or not refusal.strip():
                        raise ValueError("contradictory refusal")
                    return cls("REFUSAL", content=str(refusal), reason_code="PROVIDER_REFUSAL")
                if not response.output.strip():
                    raise ValueError("empty final")
                return cls("FINAL", content=response.output)
            if protocol_mode != "structured" or call is not None or refusal:
                raise ValueError("protocol mismatch")
            value = strict_object(response.output)
            kind = value.get("kind")
            fields = {"FINAL": {"kind", "content"}, "TOOL_CALL": {"kind", "tool_name", "arguments"},
                      "REFUSAL": {"kind", "content", "reason_code"}}
            if kind not in fields or set(value) != fields[kind]:
                raise ValueError("invalid tagged decision")
            if kind == "TOOL_CALL":
                if not isinstance(value["arguments"], dict) or not isinstance(value["tool_name"], str) or not value["tool_name"]:
                    raise ValueError("invalid tool decision")
                return cls(kind, tool_name=value["tool_name"], arguments=value["arguments"], provider_tool_call_id=synthetic_id)
            if not isinstance(value["content"], str) or not value["content"].strip():
                raise ValueError("empty content")
            if kind == "REFUSAL" and value["reason_code"] not in {"PROVIDER_REFUSAL", "SAFETY", "UNSUPPORTED"}:
                raise ValueError("invalid refusal code")
            return cls(kind, content=value["content"], reason_code=value.get("reason_code"))
        except (TypeError, ValueError, KeyError, AttributeError):
            raise LoopStopped("LOOP_DECISION_INVALID") from None


@dataclass(frozen=True)
class LoopDecisionV2:
    kind: str
    content: str | None = None
    calls: tuple[dict, ...] = ()
    target_agent_id: str | None = None
    task: str | None = None
    provider_tool_call_id: str | None = None
    reason_code: str | None = None

    @property
    def digest(self):
        return canonical_payload_digest(asdict(self))

    @classmethod
    def parse(cls, response, *, protocol_mode, synthetic_id, handoff_targets=()):
        try:
            if response.finish_reason not in {"stop", "tool_calls", "end_turn"}:
                raise ValueError("incomplete response")
            calls = tuple(getattr(response, "native_tool_calls", None) or ())
            if response.native_tool_call is not None:
                raise ValueError("v2 single representation forbidden")
            count = response.tool_call_count
            if type(count) is not int or count != len(calls):
                raise ValueError("tool count mismatch")
            if protocol_mode == "native":
                if calls:
                    if not 1 <= len(calls) <= 4 or response.output.strip() or response.refusal or response.finish_reason != "tool_calls":
                        raise ValueError("contradictory batch")
                    expected = [{"id": c.provider_tool_call_id, "type": "function",
                        "function": {"name": c.tool_name, "arguments": c.arguments_json}} for c in calls]
                    assistant = response.assistant_message
                    if (not assistant or set(assistant) - {"role", "content", "tool_calls"}
                            or assistant.get("role") != "assistant" or assistant.get("content") not in {None, ""}
                            or assistant.get("tool_calls") != expected):
                        raise ValueError("native binding mismatch")
                    ids = [c.provider_tool_call_id for c in calls]
                    if len(set(ids)) != len(ids) or any(not isinstance(i, str) or not i.strip() or len(i) > 255 for i in ids):
                        raise ValueError("invalid call identity")
                    normalized = tuple({"tool_name": c.tool_name, "arguments": strict_object(c.arguments_json),
                        "call_id": c.provider_tool_call_id} for c in calls)
                    if any(c["tool_name"] == HANDOFF_FUNCTION for c in normalized):
                        if len(normalized) != 1:
                            raise ValueError("mixed handoff")
                        call = normalized[0]
                        value = {"kind": "HANDOFF", **call["arguments"]}
                        if set(call["arguments"]) != {"target_agent_id", "task"}:
                            raise ValueError("invalid handoff")
                        call_id = call["call_id"]
                    else:
                        value = {"kind": "TOOL_BATCH", "calls": normalized}
                        call_id = None
                else:
                    if response.finish_reason == "tool_calls" or (response.assistant_message and response.assistant_message.get("tool_calls")):
                        raise ValueError("missing native batch")
                    value = {"kind": "REFUSAL", "content": response.refusal, "reason_code": "PROVIDER_REFUSAL"} if response.refusal else {"kind": "FINAL", "content": response.output}
                    if response.refusal and response.output.strip():
                        raise ValueError("contradictory refusal")
                    call_id = None
            elif protocol_mode == "structured" and not calls and not response.refusal and response.finish_reason != "tool_calls":
                value = strict_object(response.output)
                call_id = synthetic_id
            else:
                raise ValueError("protocol mismatch")
            kind = value.get("kind")
            fields = {"FINAL": {"kind", "content"}, "REFUSAL": {"kind", "content", "reason_code"},
                "TOOL_BATCH": {"kind", "calls"}, "HANDOFF": {"kind", "target_agent_id", "task"}}
            if kind not in fields or set(value) != fields[kind]:
                raise ValueError("invalid tagged decision")
            if kind == "TOOL_BATCH":
                items = value["calls"]
                if not isinstance(items, (list, tuple)) or not 1 <= len(items) <= 4:
                    raise ValueError("invalid batch size")
                normalized = []
                for ordinal, item in enumerate(items):
                    required = {"tool_name", "arguments", "call_id"} if protocol_mode == "native" else {"tool_name", "arguments"}
                    if (not isinstance(item, dict) or set(item) != required or not isinstance(item["tool_name"], str)
                            or not item["tool_name"].strip() or item["tool_name"] == HANDOFF_FUNCTION or not isinstance(item["arguments"], dict)):
                        raise ValueError("invalid batch call")
                    normalized.append({**item, "call_id": item["call_id"] if protocol_mode == "native" else f"{synthetic_id}_{ordinal}"})
                return cls(kind, calls=tuple(normalized))
            if kind == "HANDOFF":
                if (value["target_agent_id"] not in handoff_targets or not isinstance(value["task"], str)
                        or not value["task"].strip() or len(value["task"].encode("utf-8")) > 16384):
                    raise ValueError("handoff denied")
                return cls(kind, target_agent_id=value["target_agent_id"], task=value["task"], provider_tool_call_id=call_id)
            if not isinstance(value["content"], str) or not value["content"].strip() or len(value["content"].encode("utf-8")) > 65536:
                raise ValueError("invalid result")
            if kind == "REFUSAL" and value["reason_code"] not in {"SAFETY", "UNSUPPORTED", "PROVIDER_REFUSAL"}:
                raise ValueError("invalid refusal")
            return cls(kind, content=value["content"], reason_code=value.get("reason_code"))
        except (TypeError, ValueError, KeyError, AttributeError):
            raise LoopStopped("LOOP_DECISION_INVALID") from None
