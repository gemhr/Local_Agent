"""仅从冻结 seed 和已确认 Observation 重建模型输入。"""

from copy import deepcopy
from dataclasses import dataclass, fields, is_dataclass
import json
from collections.abc import Mapping

from core.runtime.execution_aggregate import canonical_payload_digest
from .contracts import LoopStopped


def _pin_value(value):
    """Agent definition 中的冻结集合以稳定顺序进入配置身份。"""
    from core.runtime.snapshot_serialization import to_primitive
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _pin_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {key: _pin_value(item) for key, item in value.items()}
    if isinstance(value, (frozenset, set)):
        return sorted(_pin_value(item) for item in value)
    if isinstance(value, (list, tuple)):
        return [_pin_value(item) for item in value]
    return to_primitive(value)


def pinned_loop_config(router, agent_id, run_context):
    """冻结非 secret 内容身份；名字相同但内容变化也会拒绝恢复。"""
    from core.runtime.snapshot_serialization import to_primitive
    definition = router.agent_registry.resolve(agent_id).definition
    registrations = tuple(run_context.tool_resolution_snapshot.registrations())
    return {
        "agent": _pin_value(definition),
        "profiles": to_primitive(router.model_profiles),
        "model_adapters": [{"profile_id": profile.profile_id.value,
            "adapter": type(router.model_adapter_resolver.resolve(profile.profile_id)).__module__ + "." + type(router.model_adapter_resolver.resolve(profile.profile_id)).__qualname__,
            "engine": type(getattr(router.model_adapter_resolver.resolve(profile.profile_id), "_engine", None)).__module__ + "." + type(getattr(router.model_adapter_resolver.resolve(profile.profile_id), "_engine", None)).__qualname__}
            for profile in router.model_profiles],
        "selection_policy": {"context_window_safety_ratio": router.model_selection_policy.context_window_safety_ratio,
                             "multi_step_threshold": router.model_selection_policy.multi_step_threshold},
        "routing_policy": {"allow_escalation": router.model_routing_policy.allow_escalation,
                           "allow_downgrade": router.model_routing_policy.allow_downgrade,
                           "require_confirmation_for_downgrade": router.model_routing_policy.require_confirmation_for_downgrade,
                           "context_window_safety_ratio": router.model_routing_policy._selection_policy.context_window_safety_ratio},
        "generation_options": {"tool_choice": "auto", "max_tokens": router.max_tokens,
                               "provider_structured_output": [p.supports_provider_structured_output for p in router.model_profiles]},
        "tools": [{"schema": item.native_function_definition(),
                   "adapter": type(item.adapter).__module__ + "." + type(item.adapter).__qualname__,
                   "spec": to_primitive(item.adapter.spec)} for item in registrations],
        "max_tokens": router.max_tokens,
        "context_window": router.model_context_window,
        "security_prompt": router._build_system_prompt(agent_id, business_definition=definition),
        "estimator": type(router.context_builder.estimator).__module__ + "." + type(router.context_builder.estimator).__qualname__,
    }


def prepare_loop_seed(router, agent_id, instruction, run_context, *, event_emitter, memory_context_bundle=None):
    definition = router.agent_registry.resolve(agent_id).definition
    messages = router._prepare_answer_messages(
        agent_id, instruction, run_context=run_context, event_emitter=event_emitter,
        memory_context_bundle=memory_context_bundle, native_selection=True,
        business_definition=definition,
    )
    config = pinned_loop_config(router, agent_id, run_context)
    # 同一 Frame 不允许 native/structured 间 fallback；混合能力采用通用严格 JSON。
    native = all(item.supports_native_tool_calling for item in router.model_profiles)
    mode = "native" if native else "structured"
    if not native:
        messages[0]["content"] += (
            '\n每轮只返回一个 JSON 对象，禁止额外字段：'
            '{"kind":"FINAL","content":"..."} 或 '
            '{"kind":"TOOL_CALL","tool_name":"...","arguments":{...}} 或 '
            '{"kind":"REFUSAL","reason_code":"SAFETY|UNSUPPORTED|PROVIDER_REFUSAL","content":"..."}。'
            '\n可用工具 schema：' + json.dumps([item["schema"] for item in config["tools"]], ensure_ascii=False)
        )
    return {"messages": messages, "protocol_mode": mode, "goal_digest": canonical_payload_digest(instruction)}, config


@dataclass(frozen=True)
class LoopContextInputV1:
    seed: dict
    ordered_accepted_decisions: tuple
    committed_observations: tuple
    cursor: int

    def build(self):
        messages = deepcopy(self.seed["messages"])
        observations = {item.iteration_no: item for item in self.committed_observations}
        seen = set()
        for item in self.ordered_accepted_decisions:
            if item.decision_kind != "TOOL_CALL":
                continue
            observed = observations.get(item.iteration_no)
            if observed is None:
                continue
            assistant = deepcopy(item.assistant_record)
            call_id = assistant["tool_calls"][0]["id"]
            if call_id in seen:
                raise LoopStopped("LOOP_DECISION_INVALID")
            seen.add(call_id)
            observation = observed.observation
            if canonical_payload_digest(observation) != observed.observation_digest:
                raise LoopStopped("LOOP_RESULT_UNAVAILABLE")
            if self.seed["protocol_mode"] == "native":
                messages.extend([assistant, {"role": "tool", "tool_call_id": call_id, "content": observation["content"]}])
            else:
                messages.extend([
                    {"role": "assistant", "content": json.dumps({"kind": "TOOL_CALL", "action_id": item.action_id,
                     "tool_name": assistant["tool_calls"][0]["function"]["name"],
                     "arguments": json.loads(assistant["tool_calls"][0]["function"]["arguments"])}, ensure_ascii=False)},
                    {"role": "user", "content": json.dumps({"action_id": item.action_id, "observation": observation["content"]}, ensure_ascii=False)},
                ])
        if len(observations) != self.cursor:
            raise LoopStopped("LOOP_RESULT_UNAVAILABLE")
        return messages, canonical_payload_digest(messages)
