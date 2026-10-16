"""Loop 的版本、有限策略与严格模型决策合同。"""

from dataclasses import asdict, dataclass
import json
from typing import Mapping

from core.runtime.execution_aggregate import canonical_payload_digest

LOOP_POLICY = "durable-agent-loop.v1"


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
            if response is None or getattr(response, "finish_reason", None) not in {"stop", "tool_calls", "end_turn"} or (getattr(response, "tool_call_count", None) or 0) > 1:
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
